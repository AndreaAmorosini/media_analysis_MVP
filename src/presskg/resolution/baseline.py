
"""Read-only Neo4j baseline audit for PressKG entity resolution."""

from __future__ import annotations

import argparse
import hashlib
import json

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from neo4j import GraphDatabase

from presskg.settings import CFG


VERSION = "resolution-baseline-v1"
FACT_VIEW_VERSION = "validated-fact-view-v1"

OLD_LABELS = [
    "ValidatedCanonicalEntity",
    "EntityResolutionBatch",
    "ComponentEntityResolutionBatch",
    "CompoundVerificationBatch",
    "FactConsistencyHold",
    "CanonicalReconciliationBatch",
    "CanonicalReconciliationV2Batch",
    "CanonicalFactProjectionBatch",
    "ProjectedValidatedFact",
    "LocalToGlobalAutoBatch",
    "L2GAutoGlobalEntity",
]


FACT_QUERY = """
MATCH (b:ValidatedFactBatch {id:$key})-[:HAS_FACT]->(f:ValidatedFact)

OPTIONAL MATCH (a:Article {id:f.article_id})
OPTIONAL MATCH (c:Claim {id:f.claim_id})
OPTIONAL MATCH (v:ClaimValidation {id:f.validation_id})

RETURN
    f.id AS id,
    f.claim_id AS claim_id,
    f.article_id AS article_id,
    f.validation_id AS validation_id,
    f.representation AS representation,
    f.subject_name AS subject_name,
    f.object_name AS object_name,
    f.subject_kind AS subject_kind,
    f.object_kind AS object_kind,
    f.predicate AS predicate,
    f.batch_id AS batch_id,
    f.original_quote AS original_quote,
    f.supporting_span AS supporting_span,

    a IS NOT NULL AS article_found,
    c IS NOT NULL AS claim_found,
    v IS NOT NULL AS validation_found,

    c.article_id AS claim_article_id,
    v.claim_id AS validation_claim_id,
    v.batch_id AS validation_batch_id,

    CASE
        WHEN a.body IS NOT NULL
         AND f.original_quote IS NOT NULL
        THEN a.body CONTAINS f.original_quote
        ELSE false
    END AS original_quote_in_body,

    CASE
        WHEN a.body IS NOT NULL
         AND f.supporting_span IS NOT NULL
        THEN a.body CONTAINS f.supporting_span
        ELSE false
    END AS supporting_span_in_body,

    [
        (f)-[:SUBJECT_MENTION]->(sm:ValidatedMention) |
        {
            id: sm.id,
            fact_id: sm.fact_id,
            role: sm.role,
            name: sm.name,
            kind: sm.kind
        }
    ] AS subjects,

    [
        (f)-[:OBJECT_MENTION]->(om:ValidatedMention) |
        {
            id: om.id,
            fact_id: om.fact_id,
            role: om.role,
            name: om.name,
            kind: om.kind
        }
    ] AS objects,

    EXISTS {
        MATCH (c)-[:HAS_VALIDATED_FACT]->(f)
    } AS claim_link,

    EXISTS {
        MATCH (v)-[:VALIDATED_AS]->(f)
    } AS validation_link,

    EXISTS {
        MATCH (a)-[:HAS_VALIDATED_FACT]->(f)
    } AS article_link

ORDER BY f.id
"""


def inspect_fact(
    row: dict,
    batch_id: str,
) -> list[str]:
    """Pure, independently testable invariant checks."""

    issues = []

    for key in ("article", "claim", "validation"):
        if not row[f"{key}_found"]:
            issues.append(f"missing_{key}")

        elif not row[f"{key}_link"]:
            issues.append(f"missing_{key}_link")

    if (
        row["claim_found"]
        and row["claim_article_id"] != row["article_id"]
    ):
        issues.append("claim_article_mismatch")

    if row["validation_found"]:

        if row["validation_claim_id"] != row["claim_id"]:
            issues.append("validation_claim_mismatch")

        if row["validation_batch_id"] != batch_id:
            issues.append("validation_batch_mismatch")

    if row["batch_id"] != batch_id:
        issues.append("fact_batch_mismatch")

    if not row["original_quote_in_body"]:
        issues.append("original_quote_not_literal")

    # Supporting spans of corrected facts may be normalized
    # from nearby article context.
    # Absence of verbatim matching is therefore a warning.
    if not row["supporting_span_in_body"]:
        issues.append("supporting_span_not_literal")

    for role, key in (
        ("subject", "subjects"),
        ("object", "objects"),
    ):
        mentions = row[key]

        if len(mentions) != 1:
            issues.append(
                f"{role}_mention_count_{len(mentions)}"
            )
            continue

        mention = mentions[0]

        if mention["id"] != f"{row['id']}:{role}":
            issues.append(
                f"{role}_mention_id_mismatch"
            )

        for prop, expected in (
            ("fact_id", row["id"]),
            ("role", role),
            ("name", row[f"{role}_name"]),
            ("kind", row[f"{role}_kind"]),
        ):
            if mention[prop] != expected:
                issues.append(
                    f"{role}_{prop}_mismatch"
                )

    return issues


def query_one(session, query, **params):
    result = session.run(
        query,
        **params,
    ).single()

    return dict(result) if result else {}


def run(args):
    batch_key = (
        f"{FACT_VIEW_VERSION}:{args.batch_id}"
    )

    driver = GraphDatabase.driver(
        CFG.neo4j_uri,
        auth=(
            CFG.neo4j_user,
            CFG.neo4j_password,
        ),
    )

    try:
        with driver.session(
            database=CFG.neo4j_database
        ) as session:

            # ----------------------------------------
            # 1. Verify materialization batch
            # ----------------------------------------

            batch = query_one(
                session,
                """
                MATCH (
                    b:ValidatedFactBatch {
                        id:$key
                    }
                )
                RETURN properties(b) AS properties
                """,
                key=batch_key,
            ).get("properties")

            if not batch:
                raise RuntimeError(
                    "ValidatedFactBatch not found: "
                    f"{batch_key}"
                )

            if batch.get("status") != "ready":
                raise RuntimeError(
                    "Fact batch not ready: "
                    f"{batch.get('status')}"
                )

            if batch.get("batch_id") != args.batch_id:
                raise RuntimeError(
                    "Fact batch ID mismatch"
                )

            # ----------------------------------------
            # 2. Claim and article inventory
            # ----------------------------------------

            totals = query_one(
                session,
                """
                MATCH (
                    v:ClaimValidation {
                        batch_id:$batch_id
                    }
                )

                WITH
                    count(v) AS validation_count,
                    count(DISTINCT v.claim_id)
                        AS validated_claims,
                    sum(
                        CASE
                            WHEN v.status IN [
                                'accepted',
                                'corrected_verified'
                            ]
                            THEN 1
                            ELSE 0
                        END
                    ) AS eligible

                MATCH (c:Claim)

                WITH
                    validation_count,
                    validated_claims,
                    eligible,
                    count(c) AS claims

                MATCH (a:Article)

                RETURN
                    validation_count,
                    validated_claims,
                    eligible,
                    claims,

                    count(a) AS articles,

                    sum(
                        CASE
                            WHEN a.title IS NULL
                              OR trim(a.title) = ''
                            THEN 1
                            ELSE 0
                        END
                    ) AS articles_without_title,

                    sum(
                        CASE
                            WHEN a.body IS NULL
                              OR trim(a.body) = ''
                            THEN 1
                            ELSE 0
                        END
                    ) AS articles_without_body,

                    sum(
                        CASE
                            WHEN a.publication_date IS NULL
                            THEN 1
                            ELSE 0
                        END
                    ) AS articles_without_date,

                    count(DISTINCT a.content_hash)
                        AS distinct_article_content_hashes
                """,
                batch_id=args.batch_id,
            )

            # ----------------------------------------
            # 3. Global fact view counts
            # ----------------------------------------

            global_counts = query_one(
                session,
                """
                OPTIONAL MATCH (f:ValidatedFact)

                WITH count(f) AS facts

                OPTIONAL MATCH (m:ValidatedMention)

                WITH
                    facts,
                    count(m) AS mentions

                OPTIONAL MATCH (b:ValidatedFactBatch)

                RETURN
                    facts,
                    mentions,
                    count(b) AS batches
                """,
            )

            # ----------------------------------------
            # 4. Detect legacy resolution nodes
            # ----------------------------------------

            old_counts = {}

            for label in OLD_LABELS:

                # Labels are constants maintained in this
                # module, never untrusted user input.

                result = query_one(
                    session,
                    f"""
                    MATCH (n:`{label}`)
                    RETURN count(n) AS n
                    """,
                )

                old_counts[label] = result.get(
                    "n",
                    0,
                )

            # ----------------------------------------
            # 5. Inspect every validated fact
            # ----------------------------------------

            issues = []

            kinds = Counter()
            predicates = Counter()
            representations = Counter()
            flags = Counter()

            seen_ids = set()
            seen_claims = set()

            records = 0

            for record in session.run(
                FACT_QUERY,
                key=batch_key,
            ):
                row = dict(record)
                records += 1

                kinds[
                    ("subject", row["subject_kind"])
                ] += 1

                kinds[
                    ("object", row["object_kind"])
                ] += 1

                predicates[
                    row["predicate"]
                ] += 1

                representations[
                    row["representation"]
                ] += 1

                found = inspect_fact(
                    row,
                    args.batch_id,
                )

                if row["id"] in seen_ids:
                    found.append(
                        "duplicate_fact_id_in_batch"
                    )

                if row["claim_id"] in seen_claims:
                    found.append(
                        "duplicate_claim_id_in_batch"
                    )

                seen_ids.add(row["id"])
                seen_claims.add(row["claim_id"])

                for issue in found:
                    flags[issue] += 1

                    issues.append({
                        "fact_id": row["id"],
                        "claim_id": row["claim_id"],
                        "issue": issue,
                    })

            # ----------------------------------------
            # 6. Evaluate baseline invariants
            # ----------------------------------------

            hard_flags = {
                key: count
                for key, count in flags.items()
                if key != "supporting_span_not_literal"
            }

            invariants = {
                "complete_validations":
                    totals["validation_count"]
                    == totals["claims"]
                    == totals["validated_claims"],

                "eligible_matches_materialization":
                    totals["eligible"] == records,

                "batch_expected_matches":
                    batch.get("expected_facts")
                    == records,

                "all_facts_in_selected_batch":
                    records == global_counts["facts"],

                "two_mentions_per_fact":
                    global_counts["mentions"]
                    == 2 * records,

                "single_batch":
                    global_counts["batches"] == 1,

                "no_broken_fact_links":
                    not hard_flags,

                "no_legacy_nodes":
                    not any(old_counts.values()),
            }

            # ----------------------------------------
            # 7. Build summary
            # ----------------------------------------

            report = {
                "version": VERSION,
                "generated_at":
                    datetime.now(
                        timezone.utc
                    ).isoformat(),

                "batch_key": batch_key,
                "batch_properties": batch,
                "totals": totals,
                "global_counts": global_counts,
                "scanned_facts": records,

                "mention_kind_counts": [
                    {
                        "role": role,
                        "kind": kind,
                        "count": count,
                    }
                    for (role, kind), count
                    in sorted(
                        kinds.items(),
                        key=lambda x: (
                            x[0][0],
                            str(x[0][1]),
                        ),
                    )
                ],

                "predicate_counts":
                    dict(sorted(
                        predicates.items(),
                        key=lambda x: str(x[0]),
                    )),

                "representation_counts":
                    dict(sorted(
                        representations.items(),
                        key=lambda x: str(x[0]),
                    )),

                "issue_counts":
                    dict(sorted(flags.items())),

                "legacy_node_counts":
                    old_counts,

                "invariants":
                    invariants,

                "status":
                    "PASS"
                    if all(invariants.values())
                    else "FAIL",

                "graph_writes": 0,
            }

            # Stable fingerprint excluding run timestamp.
            source_hash = hashlib.sha256(
                json.dumps(
                    {
                        key: value
                        for key, value in report.items()
                        if key != "generated_at"
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()

            report["baseline_fingerprint"] = (
                source_hash
            )

    finally:
        driver.close()

    # ----------------------------------------
    # 8. Write reports
    # ----------------------------------------

    output = Path(args.out_dir)

    output.mkdir(
        parents=True,
        exist_ok=True,
    )

    (
        output / "baseline_summary.json"
    ).write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ) + "\n",
        encoding="utf-8",
    )

    with (
        output / "baseline_issues.jsonl"
    ).open(
        "w",
        encoding="utf-8",
    ) as fh:

        for issue in issues:
            fh.write(
                json.dumps(
                    issue,
                    ensure_ascii=False,
                    sort_keys=True,
                ) + "\n"
            )

    print(
        json.dumps(
            {
                "status": report["status"],
                "facts": records,
                "issues": len(issues),
                "invariants": invariants,
                "out_dir": str(output),
            },
            indent=2,
        )
    )

    if report["status"] != "PASS":
        raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--batch-id",
        default="qwen38-gemma31-gemma12-v1",
    )

    parser.add_argument(
        "--out-dir",
        default="output/resolution/baseline-v1",
    )

    run(parser.parse_args())


if __name__ == "__main__":
    main()
