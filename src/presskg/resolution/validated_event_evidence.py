
"""PressKG 7B.3: Validated Event Evidence Linking.

Links CanonicalEvent to ValidatedFact using explicit Claim
participation and existing Claim HAS_VALIDATED_FACT edges.

No LLM, no checksum checks, no inferred entity resolution.
Dry-run by default. --apply writes; --rollback deletes only
this projection's SUPPORTED_BY relationships.
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
from presskg.settings import CFG


VERSION = "validated-event-evidence-v1"
EVENT_VERSION = "canonical-event-projection-v1"


SOURCE_QUERY = """
MATCH (c:Claim)-[role:SUBJECT|OBJECT]->(m:EventMention)
MATCH (c)-[:HAS_VALIDATED_FACT]->(f:ValidatedFact)
MATCH (m)-[:EVENT_RESOLVED_AS]->(e:CanonicalEvent)
MATCH (a:Article)-[:HAS_EVENT]->(m)
WHERE e.projection_version = $event_version
  AND m.article_id = a.id
  AND e.article_id = a.id
  AND f.article_id = a.id
RETURN
    e.event_key AS event_key,
    e.mention_id AS canonical_mention_id,
    e.name AS canonical_name,
    m.id AS mention_id,
    m.name AS mention_name,
    a.id AS article_id,
    c.id AS claim_id,
    f.id AS fact_id,
    f.claim_id AS fact_claim_id,
    f.subject_name AS subject_name,
    f.object_name AS object_name,
    f.eligible_for_propagation AS eligible_for_propagation,
    type(role) AS event_role
"""


EVENT_COUNTS_QUERY = """
MATCH (e:CanonicalEvent)
WHERE e.projection_version = $version
RETURN count(e) AS total
"""


EXISTING_QUERY = """
MATCH (e:CanonicalEvent)-[r:SUPPORTED_BY]->
      (f:ValidatedFact)
WHERE r.projection_version = $version
RETURN
    r.evidence_key AS evidence_key,
    e.event_key AS event_key,
    f.id AS fact_id,
    r.event_role AS event_role,
    r.article_id AS article_id,
    r.claim_ids AS claim_ids
"""


APPLY_QUERY = """
UNWIND $rows AS x

MATCH (e:CanonicalEvent {
    event_key: x.event_key,
    projection_version: $event_version
})
MATCH (f:ValidatedFact {
    id: x.fact_id,
    article_id: x.article_id
})
MATCH (m:EventMention {
    id: x.mention_id,
    article_id: x.article_id
})-[:EVENT_RESOLVED_AS]->(e)

MERGE (e)-[r:SUPPORTED_BY {
    evidence_key: x.evidence_key
}]->(f)

ON CREATE SET
    r.projection_version = $version,
    r.event_role = x.event_role,
    r.article_id = x.article_id,
    r.claim_ids = x.claim_ids,
    r.evidence_scope = 'VALIDATED_CLAIM_PARTICIPATION'

RETURN count(r) AS matched
"""


ROLLBACK_QUERY = """
MATCH ()-[r:SUPPORTED_BY]->()
WHERE r.projection_version = $version
WITH collect(r) AS rels
FOREACH (r IN rels | DELETE r)
RETURN size(rels) AS deleted
"""


def require(condition, message):
    if not condition:
        raise ValueError(message)


def normalize(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip().casefold()


def stable_hash(parts):
    encoded = json.dumps(
        parts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    tmp.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def read_sources(tx):
    return [
        dict(record)
        for record in tx.run(
            SOURCE_QUERY,
            event_version=EVENT_VERSION,
        )
    ]


def read_existing(tx):
    return [
        dict(record)
        for record in tx.run(
            EXISTING_QUERY,
            version=VERSION,
        )
    ]


def count_events(tx):
    return tx.run(
        EVENT_COUNTS_QUERY,
        version=EVENT_VERSION,
    ).single()["total"]


def build_plan(sources):
    """
    Accept only explicitly linked, correctly directed,
    same-article validated facts.

    A validated correction which changes the participant
    name is not projected back onto the original mention.
    """
    grouped = defaultdict(lambda: {
        "claims": set(),
        "mention_ids": set(),
    })

    stats = Counter()
    seen_source_rows = set()

    for item in sources:
        stats["source_rows"] += 1

        event_key = item["event_key"]
        fact_id = item["fact_id"]
        claim_id = item["claim_id"]
        mention_id = item["mention_id"]
        article_id = item["article_id"]
        role = item["event_role"]

        require(
            all(
                isinstance(x, str) and x
                for x in (
                    event_key,
                    fact_id,
                    claim_id,
                    mention_id,
                    article_id,
                )
            ),
            "Missing event, fact, claim or article identity",
        )

        require(
            role in ("SUBJECT", "OBJECT"),
            f"Unexpected claim role: {role}",
        )

        require(
            item["canonical_mention_id"] == mention_id,
            f"Event identity mismatch: {mention_id}",
        )

        source_identity = (
            event_key,
            fact_id,
            claim_id,
            mention_id,
            role,
        )

        if source_identity in seen_source_rows:
            stats["duplicate_paths"] += 1
            continue

        seen_source_rows.add(source_identity)

        mention_name = normalize(item["mention_name"])
        canonical_name = normalize(item["canonical_name"])

        if (
            not mention_name
            or mention_name != canonical_name
        ):
            stats["event_name_mismatch"] += 1
            continue

        participant_name = normalize(
            item["subject_name"]
            if role == "SUBJECT"
            else item["object_name"]
        )

        if participant_name != mention_name:
            stats["validated_participant_mismatch"] += 1
            continue

        key = (event_key, fact_id, article_id, role)
        grouped[key]["claims"].add(claim_id)
        grouped[key]["mention_ids"].add(mention_id)

        stats["accepted_source_rows"] += 1

        if item["eligible_for_propagation"] is True:
            stats["eligible_source_rows"] += 1

    rows = []

    for (event_key, fact_id, article_id, role), group in sorted(
        grouped.items()
    ):
        require(
            len(group["mention_ids"]) == 1,
            "Multiple EventMention nodes mapped "
            "to one mention-local CanonicalEvent",
        )

        mention_id = next(iter(group["mention_ids"]))

        evidence_key = stable_hash({
            "version": VERSION,
            "event_key": event_key,
            "fact_id": fact_id,
            "role": role,
        })

        rows.append({
            "evidence_key": evidence_key,
            "event_key": event_key,
            "fact_id": fact_id,
            "mention_id": mention_id,
            "article_id": article_id,
            "event_role": role,
            "claim_ids": sorted(group["claims"]),
        })

    require(
        len(set(r["evidence_key"] for r in rows))
        == len(rows),
        "Evidence key collision",
    )

    stats["projectable_links"] = len(rows)
    stats["linked_events"] = len({
        r["event_key"] for r in rows
    })
    stats["linked_validated_facts"] = len({
        r["fact_id"] for r in rows
    })

    return rows, dict(stats)


def validate_existing(existing, rows):
    planned = {
        row["evidence_key"]: row
        for row in rows
    }

    seen = set()

    for item in existing:
        key = item["evidence_key"]

        require(
            isinstance(key, str) and key,
            "Existing SUPPORTED_BY without evidence_key",
        )

        require(
            key not in seen,
            f"Duplicate existing evidence link: {key}",
        )
        seen.add(key)

        require(
            key in planned,
            f"Unexpected existing evidence link: {key}",
        )

        expected = planned[key]

        for field in (
            "event_key",
            "fact_id",
            "event_role",
            "article_id",
            "claim_ids",
        ):
            require(
                item[field] == expected[field],
                f"Existing evidence conflict: {key}/{field}",
            )

    return len(existing)


def apply_transaction(tx, expected_rows, batch_size):
    """
    Read and recheck all source paths inside the write
    transaction before materializing the evidence links.
    """
    current_rows, _ = build_plan(read_sources(tx))

    require(
        current_rows == expected_rows,
        "Source graph changed since preflight",
    )

    validate_existing(
        read_existing(tx),
        expected_rows,
    )

    written = 0

    for start in range(0, len(expected_rows), batch_size):
        batch = expected_rows[start:start + batch_size]

        count = tx.run(
            APPLY_QUERY,
            rows=batch,
            event_version=EVENT_VERSION,
            version=VERSION,
        ).single()["matched"]

        require(
            count == len(batch),
            "Evidence link write cardinality mismatch",
        )

        written += count

    final_existing = read_existing(tx)

    require(
        validate_existing(
            final_existing,
            expected_rows,
        ) == len(expected_rows),
        "Post-apply evidence validation failed",
    )

    return written


def rollback_transaction(tx):
    return tx.run(
        ROLLBACK_QUERY,
        version=VERSION,
    ).single()["deleted"]


def run(args):
    require(
        not (args.apply and args.rollback),
        "Choose --apply or --rollback",
    )
    require(
        args.batch_size > 0,
        "Batch size must be positive",
    )

    # Initialize the existing PressKG settings.
    _ = CFG

    password = os.environ.get("NEO4J_PASSWORD")
    require(password, "NEO4J_PASSWORD is required")

    driver = GraphDatabase.driver(
        os.environ.get(
            "NEO4J_URI",
            "bolt://localhost:7687",
        ),
        auth=(
            os.environ.get("NEO4J_USER", "neo4j"),
            password,
        ),
    )

    try:
        driver.verify_connectivity()

        if args.rollback:
            with driver.session(
                database=args.database,
            ) as session:
                deleted = session.execute_write(
                    rollback_transaction
                )

            print(json.dumps({
                "version": VERSION,
                "operation": "ROLLBACK",
                "deleted_links": deleted,
                "original_facts_modified": 0,
            }, indent=2))
            return

        with driver.session(
            database=args.database,
            default_access_mode=READ_ACCESS,
        ) as session:
            events = session.execute_read(count_events)
            sources = session.execute_read(read_sources)
            existing = session.execute_read(read_existing)

        require(
            events > 0,
            "No CanonicalEvent nodes found. "
            "Apply Phase 7B.2 first.",
        )

        rows, stats = build_plan(sources)

        require(
            len(rows) > 0,
            "No evidence links satisfy the conservative "
            "matching rules. No changes made.",
        )

        already_created = validate_existing(
            existing, rows
        )

        report = {
            "version": VERSION,
            "operation": (
                "APPLY" if args.apply else "DRY_RUN"
            ),
            "canonical_events": events,
            "statistics": stats,
            "existing_evidence_links": already_created,
            "matching_policy":
                "CLAIM_ROLE_ARTICLE_EXACT_NORMALIZED_NAME",
            "llm_calls": 0,
            "checksum_validation": False,
            "original_facts_modified": 0,
            "graph_writes": 0,
        }

        if args.apply:
            with driver.session(
                database=args.database,
            ) as session:
                count = session.execute_write(
                    apply_transaction,
                    rows,
                    args.batch_size,
                )

            report["materialized_evidence_links"] = count
            report["graph_writes"] = count

        save_json(
            Path(args.out_dir) / "summary.json",
            report,
        )

        print(json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        ))

    finally:
        driver.close()


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--database",
        default="neo4j",
    )
    parser.add_argument(
        "--out-dir",
        default=(
            "output/resolution/"
            "validated-event-evidence-v1"
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=250,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
    )

    run(parser.parse_args())


if __name__ == "__main__":
    main()
