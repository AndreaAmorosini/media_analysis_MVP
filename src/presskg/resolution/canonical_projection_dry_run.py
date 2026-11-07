
"""PressKG Phase 5A: Canonical Projection Dry-run.

Validates the Phase 4F.1 manifest against Neo4j and
generates an auditable plan. NEVER writes to Neo4j.

Default schema assumptions are configurable via CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re

from collections import Counter, defaultdict
from pathlib import Path

from neo4j import GraphDatabase, READ_ACCESS


VERSION = "canonical-projection-dry-run-v1"
APPROVED = "APPROVED_FOR_PROJECTION"

ROLE_HEADS = {
    "sindaco", "vicesindaco", "assessore",
    "presidente", "vicepresidente",
    "ministro", "prefetto", "questore",
    "commissario", "direttore",
    "responsabile", "amministratore",
    "segretario", "governatore",
}

SCHEMA_NAME_RE = re.compile(
    r"^[A-Za-z_][A-Za-z_0-9]*$"
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    h = hashlib.sha256()

    with Path(path).open("rb") as f:
        for chunk in iter(
            lambda: f.read(1024 * 1024), b""
        ):
            h.update(chunk)

    return h.hexdigest()


def read_json(path):
    return json.loads(
        Path(path).read_text(encoding="utf-8")
    )


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as f:
        for number, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{number}: {exc}"
                ) from exc


def write_jsonl(path, records):
    with Path(path).open(
        "w", encoding="utf-8"
    ) as f:
        for row in records:
            f.write(json.dumps(
                row, ensure_ascii=False, sort_keys=True
            ) + "\n")


def cypher_identifier(name):
    require(
        bool(SCHEMA_NAME_RE.fullmatch(name)),
        f"Invalid Cypher identifier: {name!r}",
    )
    return f"`{name}`"


def load_manifest(directory):
    directory = Path(directory)
    summary = read_json(directory / "summary.json")

    require(
        summary["version"]
        == "projection-readiness-refinement-v1",
        "Unsupported manifest version",
    )

    require(
        summary["graph_writes"] == 0
        and summary["identity_merges"] == 0,
        "Upstream graph mutation",
    )

    for filename, expected in summary["checksums"].items():
        require(
            sha256(directory / filename) == expected,
            f"Checksum mismatch: {filename}",
        )

    manifest = {}

    for row in read_jsonl(
        directory / "refined_manifest.jsonl"
    ):
        iid = row["proposed_identity_id"]

        require(
            iid not in manifest,
            f"Duplicate identity: {iid}",
        )

        manifest[iid] = row

    assignments = []
    seen_profiles = set()
    seen_mentions = set()
    seen_fact_roles = set()
    approved_by_identity = defaultdict(set)

    for row in read_jsonl(
        directory / "refined_assignments.jsonl"
    ):
        pid = row["profile_id"]

        require(
            pid not in seen_profiles,
            f"Duplicate profile: {pid}",
        )
        seen_profiles.add(pid)

        status = row["readiness_status"]
        iid = row["proposed_identity_id"]

        if iid is not None:
            require(
                iid in manifest,
                f"Unknown identity: {iid}",
            )

        if status != APPROVED:
            continue

        require(
            iid is not None
            and row["profile_scope"]
            == "mention_occurrence"
            and row["role"] in {"subject", "object"}
            and row["mention_id"]
            == f'{row["fact_id"]}:{row["role"]}'
            and row["canonical_entity_id"] is None
            and not row["graph_write_approved"],
            f"Invalid approved assignment: {pid}",
        )

        mention = row["mention_id"]
        fact_role = (
            row["fact_id"], row["role"]
        )

        require(
            mention not in seen_mentions,
            f"Duplicate mention: {mention}",
        )
        require(
            fact_role not in seen_fact_roles,
            f"Duplicate fact role: {fact_role}",
        )

        seen_mentions.add(mention)
        seen_fact_roles.add(fact_role)

        approved_by_identity[iid].add(pid)
        assignments.append(row)

    require(
        len(seen_profiles) == summary["profiles_total"],
        "Profile count mismatch",
    )
    require(
        len(manifest) == summary["identities_total"],
        "Identity count mismatch",
    )
    require(
        len(assignments)
        == summary["refined_status_counts"][APPROVED],
        "Approved assignment count mismatch",
    )

    for iid, item in manifest.items():
        require(
            set(item["approved_profile_ids"])
            == approved_by_identity[iid],
            f"Approved profiles mismatch: {iid}",
        )

        require(
            item["approved_occurrences"]
            == len(approved_by_identity[iid]),
            f"Occurrence count mismatch: {iid}",
        )

        if approved_by_identity[iid]:
            require(
                item["readiness_status"] == APPROVED,
                f"Unapproved identity with approved mentions: {iid}",
            )

    return summary, manifest, assignments


def build_query(args):
    m_label = cypher_identifier(args.mention_label)
    f_label = cypher_identifier(args.fact_label)
    a_label = cypher_identifier(args.article_label)

    mid = cypher_identifier(args.mention_id_prop)
    fid = cypher_identifier(args.fact_id_prop)
    aid = cypher_identifier(args.article_id_prop)

    return f"""
    UNWIND $rows AS x

    OPTIONAL MATCH (m:{m_label} {{{mid}: x.mention_id}})
    WITH x, collect(DISTINCT m) AS ms

    OPTIONAL MATCH (f:{f_label} {{{fid}: x.fact_id}})
    WITH x, ms, collect(DISTINCT f) AS fs

    WITH x, ms, fs,
         ms[0] AS m, fs[0] AS f

    RETURN
        x.profile_id AS profile_id,
        size(ms) AS mention_count,
        size(fs) AS fact_count,

        CASE
          WHEN size(ms) = 1 AND size(fs) = 1
          THEN EXISTS {{
            MATCH (m)--(f)
          }}
          ELSE false
        END AS fact_linked,

        m.article_id AS mention_article_id,
        f.article_id AS fact_article_id,
        m.fact_id AS mention_fact_id,
        m.role AS mention_role,

        CASE
          WHEN size(ms) = 1 AND size(fs) = 1
          THEN (
            EXISTS {{
              MATCH (m)--(a:{a_label})
              WHERE a.{aid} = x.article_id
            }}
            OR EXISTS {{
              MATCH (f)--(a:{a_label})
              WHERE a.{aid} = x.article_id
            }}
          )
          ELSE false
        END AS article_linked
    """


def query_batch(tx, query, batch):
    parameters = [
        {
            "profile_id": row["profile_id"],
            "mention_id": row["mention_id"],
            "fact_id": row["fact_id"],
            "article_id": row["article_id"],
        }
        for row in batch
    ]

    return [
        dict(record)
        for record in tx.run(
            query, rows=parameters
        )
    ]


def classify_graph_match(source, observed):
    """Fail closed when graph evidence is missing."""

    reasons = []

    if observed["mention_count"] != 1:
        reasons.append(
            "mention_missing"
            if observed["mention_count"] == 0
            else "duplicate_mention_id"
        )

    if observed["fact_count"] != 1:
        reasons.append(
            "fact_missing"
            if observed["fact_count"] == 0
            else "duplicate_fact_id"
        )

    if reasons:
        return "INVALID", reasons

    if not observed["fact_linked"]:
        reasons.append("mention_fact_link_unverified")

    if (
        observed["mention_fact_id"] is not None
        and observed["mention_fact_id"]
        != source["fact_id"]
    ):
        reasons.append("mention_fact_id_mismatch")

    if (
        observed["mention_role"] is not None
        and str(observed["mention_role"]).lower()
        != source["role"]
    ):
        reasons.append("mention_role_mismatch")

    article_properties = [
        value
        for value in (
            observed["mention_article_id"],
            observed["fact_article_id"],
        )
        if value is not None
    ]

    if any(
        value != source["article_id"]
        for value in article_properties
    ):
        reasons.append("article_id_mismatch")

    if not (
        observed["article_linked"]
        or article_properties
    ):
        reasons.append("article_provenance_unverified")

    if reasons:
        return "REVIEW", sorted(set(reasons))

    return "READY", []


def semantic_warnings(row):
    """Advisory risks, never automatic identity merges."""

    text = str(row["surface"] or "").strip()
    words = re.findall(r"\w+", text.casefold())

    warnings = []

    if (
        row["source_kind"] == "Person"
        and len(words) >= 3
        and words[0] in ROLE_HEADS
        and words[1] in {"di", "del", "della", "da"}
    ):
        warnings.append("role_description_as_person")

    if (
        row["source_kind"] == "Project"
        and len(words) >= 5
    ):
        warnings.append("descriptive_project_name")

    return warnings



def create_projection_plan(manifest, verified_rows):
    """Plan only identities with complete graph verification."""

    ready = defaultdict(set)

    for row in verified_rows:
        if row["graph_status"] == "READY":
            ready[
                row["proposed_identity_id"]
            ].add(row["profile_id"])

    result = []
    incomplete = []

    for iid, item in sorted(manifest.items()):
        expected = set(
            item["approved_profile_ids"]
        )

        if not expected:
            continue

        verified = ready[iid]

        if verified != expected:
            incomplete.append({
                "proposed_identity_id": iid,
                "expected_occurrences": len(expected),
                "verified_occurrences": len(verified),
                "unverified_profile_ids": sorted(
                    expected - verified
                ),
            })
            continue

        result.append({
            "operation": "PROPOSE_CANONICAL_ENTITY",
            "proposed_identity_id": iid,
            "semantic_facet_counts":
                item["semantic_facet_counts"],
            "surface_counts": item["surface_counts"],
            "validated_profile_ids":
                sorted(verified),
            "validated_occurrences": len(verified),
            "source_batch_key": None,
            "execute": False,
        })

    return result, incomplete

def run(args):
    (
        source_summary,
        manifest,
        assignments,
    ) = load_manifest(args.manifest_dir)

    require(
        args.batch_size >= 1,
        "Batch size must be positive",
    )

    uri = args.uri or os.environ.get(
        "NEO4J_URI", "bolt://localhost:7687"
    )
    user = args.user or os.environ.get(
        "NEO4J_USER", "neo4j"
    )
    password = (
        args.password
        or os.environ.get("NEO4J_PASSWORD")
    )

    require(
        password is not None,
        "NEO4J_PASSWORD is required",
    )

    out = Path(args.out_dir)
    require(
        not out.exists(),
        "Output directory already exists",
    )

    query = build_query(args)
    verified = []
    statuses = Counter()
    warnings = Counter()
    reasons = Counter()

    driver = GraphDatabase.driver(
        uri, auth=(user, password)
    )

    try:
        driver.verify_connectivity()

        with driver.session(
            database=args.database,
            default_access_mode=READ_ACCESS,
        ) as session:
            for start in range(
                0, len(assignments), args.batch_size
            ):
                batch = assignments[
                    start:start + args.batch_size
                ]

                graph_results = session.execute_read(
                    query_batch, query, batch
                )

                lookup = {
                    row["profile_id"]: row
                    for row in graph_results
                }

                require(
                    len(lookup) == len(batch),
                    "Incomplete Neo4j batch result",
                )

                for row in batch:
                    observed = lookup[row["profile_id"]]
                    status, problems = (
                        classify_graph_match(
                            row, observed
                        )
                    )

                    semantic = semantic_warnings(row)

                    # Semantic warnings are retained for audit.
                    # Explicit role-as-person risks are not ready.
                    if (
                        status == "READY"
                        and "role_description_as_person"
                        in semantic
                    ):
                        status = "REVIEW"
                        problems = [
                            "role_description_as_person"
                        ]

                    statuses[status] += 1
                    reasons.update(problems)
                    warnings.update(semantic)

                    verified.append({
                        "profile_id": row["profile_id"],
                        "proposed_identity_id":
                            row["proposed_identity_id"],
                        "mention_id": row["mention_id"],
                        "fact_id": row["fact_id"],
                        "article_id": row["article_id"],
                        "role": row["role"],
                        "surface": row["surface"],
                        "source_kind": row["source_kind"],
                        "graph_status": status,
                        "graph_reasons": problems,
                        "semantic_warnings": semantic,
                        "graph_evidence": observed,
                        "execute": False,
                    })
    finally:
        driver.close()

    plan, incomplete = create_projection_plan(
        manifest, verified
    )

    for row in plan:
        row["source_batch_key"] = source_summary[
            "source_batch_key"
        ]

    require(
        sum(statuses.values()) == len(assignments),
        "Missing graph verification results",
    )

    planned_ids = {
        pid
        for item in plan
        for pid in item["validated_profile_ids"]
    }

    require(
        len(planned_ids)
        <= statuses["READY"],
        "Projection plan exceeds verified coverage",
    )

    require(
        len(planned_ids) == sum(
            item["validated_occurrences"]
            for item in plan
        ),
        "Duplicate profile across canonical identities",
    )
    out.mkdir(parents=True)

    verification_path = (
        out / "graph_verification.jsonl"
    )
    plan_path = (
        out / "projection_plan.jsonl"
    )
    risk_path = (
        out / "projection_risks.jsonl"
    )
    incomplete_path = (
        out / "incomplete_identities.jsonl"
    )

    write_jsonl(
        verification_path, verified
    )
    write_jsonl(plan_path, plan)
    write_jsonl(
        risk_path,
        (
            item for item in verified
            if (
                item["graph_status"] != "READY"
                or item["semantic_warnings"]
            )
        ),
    )
    write_jsonl(incomplete_path, incomplete)

    summary = {
        "version": VERSION,
        "source_batch_key":
            source_summary["source_batch_key"],
        "source_manifest_sha256": sha256(
            Path(args.manifest_dir)
            / "refined_manifest.jsonl"
        ),
        "approved_input_occurrences":
            len(assignments),
        "approved_input_identities":
            source_summary[
                "identity_status_counts"
            ][APPROVED],
        "graph_verification_status_counts":
            dict(sorted(statuses.items())),
        "graph_verification_reason_counts":
            dict(sorted(reasons.items())),
        "semantic_warning_counts":
            dict(sorted(warnings.items())),
        "planned_canonical_entities":
            len(plan),
        "planned_mention_links":
            len(planned_ids),
        "missing_or_unverified_occurrences":
            len(assignments)
            - len(planned_ids),
        "incomplete_identities": len(incomplete),
        "verified_but_not_planned_occurrences":
            statuses["READY"] - len(planned_ids),
        "graph_writes": 0,
        "identity_merges": 0,
        "canonical_entities_created": 0,
        "llm_calls": 0,
        "checksums": {
            path.name: sha256(path)
            for path in (
                verification_path,
                plan_path,
                risk_path,
                incomplete_path
            )
        },
    }

    (out / "summary.json").write_text(
        json.dumps(
            summary, ensure_ascii=False,
            indent=2, sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(
        summary, ensure_ascii=False, indent=2
    ))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--manifest-dir",
        default=(
            "output/resolution/"
            "projection-readiness-refinement-v1"
        ),
    )
    parser.add_argument(
        "--out-dir",
        default=(
            "output/resolution/"
            "canonical-projection-dry-run-v1"
        ),
    )

    parser.add_argument("--uri")
    parser.add_argument("--user")
    parser.add_argument("--password")
    parser.add_argument("--database", default="neo4j")
    parser.add_argument(
        "--batch-size", type=int, default=250
    )

    parser.add_argument(
        "--mention-label",
        default="ValidatedMention",
    )
    parser.add_argument(
        "--fact-label",
        default="ValidatedFact",
    )
    parser.add_argument(
        "--article-label",
        default="Article",
    )
    parser.add_argument(
        "--mention-id-prop", default="id"
    )
    parser.add_argument(
        "--fact-id-prop", default="id"
    )
    parser.add_argument(
        "--article-id-prop", default="id"
    )

    run(parser.parse_args())


if __name__ == "__main__":
    main()
