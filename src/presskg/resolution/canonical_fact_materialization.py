
"""PressKG Phase 5C.2: Canonical Fact Materialization.

Creates one CANONICAL_FACT relationship per validated fact,
preserving direction, predicate and source provenance.

Requires the canonical entity projection to be materialized.

Modes:
    --check   read-only preflight (default)
    --apply   explicit transactional writes
    --verify  read-only post-write verification

Self-loops are quarantined, never silently corrected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os

from collections import Counter
from pathlib import Path

from neo4j import GraphDatabase, READ_ACCESS


VERSION = "canonical-fact-projection-v1"
ENTITY_VERSION = "canonical-projection-v1"
CONFIRM = "APPLY_CANONICAL_FACTS"

CONSTRAINT_NAME = "canonical_fact_projection_key"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    h = hashlib.sha256()

    with Path(path).open("rb") as f:
        for block in iter(
            lambda: f.read(1024 * 1024), b""
        ):
            h.update(block)

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
        for record in records:
            f.write(json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
            ) + "\n")


def stable_key(batch, fact_id):
    raw = f"{VERSION}\0{batch}\0{fact_id}"

    return "cf-" + hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:32]


def load_plan(projection_dir):
    root = Path(projection_dir)
    summary = read_json(root / "summary.json")

    require(
        summary["version"] == VERSION,
        "Unsupported fact projection version",
    )

    require(
        summary["graph_writes"] == 0,
        "Source projection was not read-only",
    )

    require(
        summary["rejected_facts"] == 0,
        "Dry-run contains rejected facts",
    )

    for filename, expected_hash in (
        summary["checksums"].items()
    ):
        require(
            sha256(root / filename) == expected_hash,
            f"Checksum mismatch: {filename}",
        )

    require(
        summary["projection_ready_facts"]
        == summary["facts_with_both_safe_roles"],
        "Incomplete projection coverage",
    )

    plan = []
    quarantine = []

    seen_facts = set()
    seen_keys = set()

    for row in read_jsonl(
        root / "canonical_fact_plan.jsonl"
    ):
        fid = row["fact_id"]
        key = row["projection_key"]

        require(
            fid not in seen_facts,
            f"Duplicate fact: {fid}",
        )

        require(
            key not in seen_keys,
            f"Duplicate projection key: {key}",
        )

        seen_facts.add(fid)
        seen_keys.add(key)

        require(
            row["operation"]
            == "PROPOSE_CANONICAL_FACT"
            and row["execute"] is False
            and row["projection_version"] == VERSION
            and row["source_batch_key"]
            == summary["source_batch_key"]
            and key == stable_key(
                summary["source_batch_key"],
                fid,
            ),
            f"Invalid fact projection lineage: {fid}",
        )

        require(
            row["subject_mention_id"]
            == f"{fid}:subject"
            and row["object_mention_id"]
            == f"{fid}:object",
            f"Invalid fact role IDs: {fid}",
        )

        require(
            isinstance(row["predicate"], str)
            and bool(row["predicate"].strip()),
            f"Missing predicate: {fid}",
        )

        if row["subject_key"] == row["object_key"]:
            quarantine.append({
                **row,
                "quarantine_reason":
                    "CANONICAL_SELF_LOOP",
            })
            continue

        plan.append(row)

    require(
        len(plan) + len(quarantine)
        == summary["projection_ready_facts"],
        "Fact plan coverage mismatch",
    )

    return summary, plan, quarantine


def batches(rows, size):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


# ==========================================================
# Live Neo4j validation
# ==========================================================

CHECK_QUERY = """
UNWIND $rows AS x

OPTIONAL MATCH (f:ValidatedFact {id: x.fact_id})
WITH x, collect(DISTINCT f) AS fs

OPTIONAL MATCH (
    sm:ValidatedMention {id: x.subject_mention_id}
)
WITH x, fs, collect(DISTINCT sm) AS sms

OPTIONAL MATCH (
    om:ValidatedMention {id: x.object_mention_id}
)
WITH x, fs, sms, collect(DISTINCT om) AS oms

OPTIONAL MATCH (
    s:CanonicalEntity {projection_key: x.subject_key}
)
WITH x, fs, sms, oms,
     collect(DISTINCT s) AS ss

OPTIONAL MATCH (
    o:CanonicalEntity {projection_key: x.object_key}
)
WITH x, fs, sms, oms, ss,
     collect(DISTINCT o) AS os

OPTIONAL MATCH ()-[
    existing:CANONICAL_FACT {
        projection_key: x.projection_key
    }
]->()
WITH x, fs, sms, oms, ss, os,
     collect(DISTINCT existing) AS existing_rels

WITH x, fs, sms, oms, ss, os, existing_rels,
     fs[0] AS f,
     sms[0] AS sm,
     oms[0] AS om,
     ss[0] AS s,
     os[0] AS o

RETURN
    x.fact_id AS fact_id,

    size(fs) AS fact_count,
    size(sms) AS subject_mention_count,
    size(oms) AS object_mention_count,
    size(ss) AS subject_entity_count,
    size(os) AS object_entity_count,

    f.predicate AS predicate,
    f.article_id AS fact_article_id,

    sm.fact_id AS subject_fact_id,
    om.fact_id AS object_fact_id,
    sm.role AS subject_role,
    om.role AS object_role,

    CASE
      WHEN size(fs) = 1 AND size(sms) = 1
      THEN EXISTS { MATCH (sm)--(f) }
      ELSE false
    END AS subject_fact_linked,

    CASE
      WHEN size(fs) = 1 AND size(oms) = 1
      THEN EXISTS { MATCH (om)--(f) }
      ELSE false
    END AS object_fact_linked,

    CASE
      WHEN size(sms) = 1 AND size(ss) = 1
      THEN EXISTS {
        MATCH (sm)-[:RESOLVED_AS]->(s)
      }
      ELSE false
    END AS subject_resolved,

    CASE
      WHEN size(oms) = 1 AND size(os) = 1
      THEN EXISTS {
        MATCH (om)-[:RESOLVED_AS]->(o)
      }
      ELSE false
    END AS object_resolved,

    s.source_batch_key AS subject_batch,
    o.source_batch_key AS object_batch,
    s.projection_version AS subject_version,
    o.projection_version AS object_version,

    [
      r IN existing_rels |
      {
        subject_key: startNode(r).projection_key,
        object_key: endNode(r).projection_key,
        fact_id: r.fact_id,
        article_id: r.article_id,
        predicate: r.predicate,
        source_batch_key: r.source_batch_key,
        projection_version: r.projection_version
      }
    ] AS existing
"""


def validate_observation(expected, observed,
                         require_materialized=False):
    errors = []

    for field in (
        "fact_count",
        "subject_mention_count",
        "object_mention_count",
        "subject_entity_count",
        "object_entity_count",
    ):
        if observed[field] != 1:
            errors.append(
                f"INVALID_{field.upper()}"
            )

    for field in (
        "subject_fact_linked",
        "object_fact_linked",
        "subject_resolved",
        "object_resolved",
    ):
        if observed[field] is not True:
            errors.append(
                f"{field.upper()}_MISSING"
            )

    if observed["predicate"] != expected["predicate"]:
        errors.append("PREDICATE_MISMATCH")

    if (
        observed["fact_article_id"] is not None
        and observed["fact_article_id"]
        != expected["article_id"]
    ):
        errors.append("ARTICLE_MISMATCH")

    for role in ("subject", "object"):
        fact_id = observed[f"{role}_fact_id"]
        mention_role = observed[f"{role}_role"]

        if (
            fact_id is not None
            and fact_id != expected["fact_id"]
        ):
            errors.append(
                f"{role.upper()}_FACT_ID_MISMATCH"
            )

        if (
            mention_role is not None
            and str(mention_role).lower() != role
        ):
            errors.append(
                f"{role.upper()}_ROLE_MISMATCH"
            )

        if (
            observed[f"{role}_batch"]
            != expected["source_batch_key"]
        ):
            errors.append(
                f"{role.upper()}_BATCH_MISMATCH"
            )

        if (
            observed[f"{role}_version"]
            != ENTITY_VERSION
        ):
            errors.append(
                f"{role.upper()}_VERSION_MISMATCH"
            )

    existing = observed["existing"]

    if len(existing) > 1:
        errors.append(
            "DUPLICATE_FACT_PROJECTION_KEY"
        )

    expected_rel = {
        "subject_key": expected["subject_key"],
        "object_key": expected["object_key"],
        "fact_id": expected["fact_id"],
        "article_id": expected["article_id"],
        "predicate": expected["predicate"],
        "source_batch_key":
            expected["source_batch_key"],
        "projection_version": VERSION,
    }

    for relation in existing:
        if relation != expected_rel:
            errors.append(
                "EXISTING_FACT_RELATION_CONFLICT"
            )

    if require_materialized and len(existing) != 1:
        errors.append(
            "EXPECTED_FACT_RELATION_MISSING"
        )

    return sorted(set(errors))


def check_batch(tx, rows, require_materialized=False):
    results = [
        dict(record)
        for record in tx.run(
            CHECK_QUERY, rows=rows
        )
    ]

    require(
        len(results) == len(rows),
        "Incomplete graph verification response",
    )

    lookup = {}

    for record in results:
        fid = record["fact_id"]

        require(
            fid not in lookup,
            f"Duplicate verification result: {fid}",
        )

        lookup[fid] = record

    problems = []

    for row in rows:
        fid = row["fact_id"]

        require(
            fid in lookup,
            f"Missing verification: {fid}",
        )

        errors = validate_observation(
            row, lookup[fid],
            require_materialized,
        )

        if errors:
            problems.append({
                "fact_id": fid,
                "projection_key":
                    row["projection_key"],
                "errors": errors,
            })

    return problems


# ==========================================================
# Transactional materialization
# ==========================================================

CREATE_QUERY = """
UNWIND $rows AS x

MATCH (
    s:CanonicalEntity {projection_key: x.subject_key}
)
MATCH (
    o:CanonicalEntity {projection_key: x.object_key}
)

MERGE (s)-[
    r:CANONICAL_FACT {
        projection_key: x.projection_key
    }
]->(o)

ON CREATE SET
    r.fact_id = x.fact_id,
    r.article_id = x.article_id,
    r.predicate = x.predicate,
    r.source_batch_key = x.source_batch_key,
    r.projection_version = $version

RETURN
    x.fact_id AS fact_id,
    r.projection_key AS projection_key,
    r.fact_id AS stored_fact_id,
    r.article_id AS stored_article_id,
    r.predicate AS stored_predicate,
    r.source_batch_key AS stored_batch,
    r.projection_version AS stored_version,
    startNode(r).projection_key AS stored_subject,
    endNode(r).projection_key AS stored_object
"""


def materialize_batch(tx, rows):
    problems = check_batch(tx, rows)

    require(
        not problems,
        "Live preflight failed: "
        + json.dumps(
            problems[:5], ensure_ascii=False
        ),
    )

    results = [
        dict(record)
        for record in tx.run(
            CREATE_QUERY,
            rows=rows,
            version=VERSION,
        )
    ]

    require(
        len(results) == len(rows),
        "Incomplete materialization batch",
    )

    stored = {}

    for result in results:
        fid = result["fact_id"]

        require(
            fid not in stored,
            f"Duplicate materialized fact: {fid}",
        )

        stored[fid] = result

    for row in rows:
        result = stored.get(row["fact_id"])

        require(
            result is not None,
            f"Missing materialized fact: {row['fact_id']}",
        )

        expected = {
            "projection_key": row["projection_key"],
            "stored_fact_id": row["fact_id"],
            "stored_article_id": row["article_id"],
            "stored_predicate": row["predicate"],
            "stored_batch": row["source_batch_key"],
            "stored_version": VERSION,
            "stored_subject": row["subject_key"],
            "stored_object": row["object_key"],
        }

        for field, value in expected.items():
            require(
                result[field] == value,
                f"Materialization conflict: "
                f"{row['fact_id']} / {field}",
            )

    # A failed check raises and rolls back this entire batch.
    problems = check_batch(
        tx, rows, require_materialized=True
    )

    require(
        not problems,
        "Post-write batch verification failed: "
        + json.dumps(problems[:5]),
    )

    return len(rows)


# ==========================================================
# Schema and batch verification
# ==========================================================

def check_constraint(session):
    rows = session.run("""
        SHOW CONSTRAINTS
        YIELD type, labelsOrTypes, properties
        RETURN type, labelsOrTypes, properties
    """)

    return any(
        "CANONICAL_FACT" in row["labelsOrTypes"]
        and row["properties"] == ["projection_key"]
        and row["type"] in {
            "RELATIONSHIP_PROPERTY_UNIQUENESS",
            "RELATIONSHIP_KEY",
            "RELATIONSHIP_KEY_CONSTRAINT",
        }
        for row in rows
    )


def validate_live(session, plan, batch_size,
                  require_materialized=False):
    problems = []
    checked = 0

    for batch in batches(plan, batch_size):
        problems.extend(
            session.execute_read(
                check_batch,
                batch,
                require_materialized,
            )
        )
        checked += len(batch)

    return checked, problems


def count_materialized(session, batch):
    result = session.run("""
        MATCH ()-[r:CANONICAL_FACT]->()
        WHERE r.projection_version = $version
          AND r.source_batch_key = $batch
        RETURN count(r) AS relationships
    """, version=VERSION, batch=batch).single()

    return result["relationships"]


# ==========================================================
# CLI
# ==========================================================

def run(args):
    summary, plan, quarantine = load_plan(
        args.projection_dir
    )
    
    gate_dir = Path(args.gate_dir)
    gate_summary = read_json(
        gate_dir / "summary.json"
    )

    require(
        gate_summary["version"]
        == "materialization-safety-gate-v1",
        "Unexpected safety gate version",
    )

    require(
        gate_summary["source_batch_key"]
        == summary["source_batch_key"],
        "Safety gate batch mismatch",
    )

    require(
        sha256(
            gate_dir / "safe_projection_plan.jsonl"
        )
        == summary["source_safety_gate_sha256"],
        "Fact plan is not based on current safety gate",
    )

    require(
        gate_summary["safe_mention_links"]
        == summary["safe_mentions"],
        "SAFE occurrence count mismatch",
    )

    require(
        args.batch_size > 0,
        "Batch size must be positive",
    )
    require(
        not (args.apply and args.verify),
        "Choose either --apply or --verify",
    )

    if args.apply:
        require(
            args.confirm == CONFIRM,
            "Explicit confirmation required",
        )

    password = os.environ.get(
        "NEO4J_PASSWORD"
    )
    require(
        password,
        "NEO4J_PASSWORD is required",
    )

    mode = (
        "APPLY" if args.apply
        else "VERIFY" if args.verify
        else "CHECK"
    )

    report = {
        "version": VERSION,
        "mode": mode,
        "source_batch_key":
            summary["source_batch_key"],
        "input_facts":
            summary["projection_ready_facts"],
        "quarantined_self_loops":
            len(quarantine),
        "expected_relationships": len(plan),
        "graph_writes": 0,
    }

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    quarantine_path = (
        out / "quarantined_self_loops.jsonl"
    )
    write_jsonl(quarantine_path, quarantine)

    driver = GraphDatabase.driver(
        os.environ.get(
            "NEO4J_URI",
            "bolt://localhost:7687",
        ),
        auth=(
            os.environ.get(
                "NEO4J_USER", "neo4j"
            ),
            password,
        ),
    )

    try:
        driver.verify_connectivity()

        with driver.session(
            database=args.database,
            default_access_mode=READ_ACCESS,
        ) as session:
            constraint = check_constraint(session)

            report["uniqueness_constraint_present"] = (
                constraint
            )

            checked, problems = validate_live(
                session,
                plan,
                args.batch_size,
                require_materialized=args.verify,
            )

            report["preflight_checked"] = checked
            report["preflight_errors"] = len(problems)
            report["preflight_examples"] = problems[:20]

            require(
                checked == len(plan),
                "Incomplete live preflight",
            )

            require(
                not problems,
                "Live validation failed",
            )

        if args.apply:
            require(
                constraint,
                "CANONICAL_FACT uniqueness constraint missing",
            )

            processed = 0

            with driver.session(
                database=args.database
            ) as session:
                for batch in batches(
                    plan, args.batch_size
                ):
                    processed += session.execute_write(
                        materialize_batch, batch
                    )

            report["processed_relationships"] = (
                processed
            )

            # Processed includes relationships that already
            # existed. Do not misreport it as new writes.
            report["graph_writes"] = "IDEMPOTENT_APPLY"

        with driver.session(
            database=args.database,
            default_access_mode=READ_ACCESS,
        ) as session:
            actual = count_materialized(
                session,
                summary["source_batch_key"],
            )

            if args.apply or args.verify:
                checked, problems = validate_live(
                    session,
                    plan,
                    args.batch_size,
                    require_materialized=True,
                )

                report["postcheck_checked"] = checked
                report["postcheck_errors"] = len(problems)

                require(
                    not problems,
                    "Post-materialization verification failed",
                )

        report["actual_relationships"] = actual

        report["complete"] = (
            actual == len(plan)
        )

        if args.apply or args.verify:
            require(
                report["complete"],
                "Materialization count mismatch",
            )

    finally:
        driver.close()

        report_path = (
            out / f"{mode.lower()}_summary.json"
        )

        report_path.write_text(
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )

        print(json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        ))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--projection-dir",
        default=(
            "output/resolution/"
            "canonical-fact-projection-v1"
        ),
    )
    parser.add_argument(
        "--out-dir",
        default=(
            "output/resolution/"
            "canonical-fact-materialization-v1"
        ),
    )
    parser.add_argument(
        "--gate-dir",
        default=(
            "output/resolution/"
            "materialization-safety-gate-v1"
        ),
    )

    parser.add_argument(
        "--database", default="neo4j"
    )
    parser.add_argument(
        "--batch-size",
        type=int, default=100
    )
    parser.add_argument(
        "--apply", action="store_true"
    )
    parser.add_argument(
        "--verify", action="store_true"
    )
    parser.add_argument("--confirm")

    run(parser.parse_args())


if __name__ == "__main__":
    main()
