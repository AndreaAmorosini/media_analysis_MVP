
"""Conservative canonical event projection.

One CanonicalEvent per EventMention, without cross-article merges.

Preserves existing EventMention, Article, Claim and FACT data.
Creates EVENT_RESOLVED_AS and DOCUMENTED_IN relationships.

"""

from __future__ import annotations

from argparse import Namespace
import hashlib
import json
import os

from collections import Counter
from pathlib import Path

from neo4j import GraphDatabase, READ_ACCESS
from presskg.config import CFG
from presskg.common import require
from presskg.common.io import write_json_stable
from presskg.resolution import paths
from presskg.resolution._common import close_step_driver, step_driver
from presskg.resolution.context import StepContext, StepResult



VERSION = "canonical-event-projection-v1"


SOURCE_QUERY = """
MATCH (e:EventMention)
OPTIONAL MATCH (a:Article)-[h:HAS_EVENT]->(e)
WITH e, count(h) AS link_count,
     collect(a.id) AS linked_article_ids
RETURN
    e.id AS mention_id,
    e.article_id AS article_id,
    e.name AS name,
    e.kind AS kind,
    e.type_group AS type_group,
    e.event_date AS event_date,
    e.location AS location,
    e.event_type AS event_type,
    link_count,
    linked_article_ids
ORDER BY mention_id
"""


EXISTING_QUERY = """
MATCH (c:CanonicalEvent)
WHERE c.projection_version = $version
OPTIONAL MATCH
    (m:EventMention)-[r:EVENT_RESOLVED_AS]->(c)
OPTIONAL MATCH
    (c)-[d:DOCUMENTED_IN]->(a:Article)
RETURN
    c.event_key AS event_key,
    c.mention_id AS mention_id,
    c.article_id AS article_id,
    c.identity_scope AS identity_scope,
    c.name AS name,
    count(DISTINCT r) AS mention_links,
    count(DISTINCT d) AS document_links,
    collect(DISTINCT m.id) AS mention_ids,
    collect(DISTINCT a.id) AS article_ids
"""


CONFLICT_QUERY = """
MATCH (m:EventMention)-[:EVENT_RESOLVED_AS]->
      (c:CanonicalEvent)
WHERE c.projection_version IS NULL
   OR c.projection_version <> $version
RETURN count(*) AS conflicts
"""


CREATE_CONSTRAINT = """
CREATE CONSTRAINT canonical_event_key_unique
IF NOT EXISTS
FOR (c:CanonicalEvent)
REQUIRE c.event_key IS UNIQUE
"""


APPLY_QUERY = """
UNWIND $rows AS x

MATCH (m:EventMention {id: x.mention_id})
MATCH (a:Article {id: x.article_id})
MATCH (a)-[:HAS_EVENT]->(m)

MERGE (c:CanonicalEvent {
    event_key: x.event_key
})

ON CREATE SET
    c.projection_version = $version,
    c.identity_scope = 'MENTION_LOCAL',
    c.mention_id = x.mention_id,
    c.article_id = x.article_id,
    c.name = x.name,
    c.kind = x.kind,
    c.type_group = x.type_group,
    c.event_date = x.event_date,
    c.location = x.location,
    c.event_type = x.event_type

MERGE (m)-[r:EVENT_RESOLVED_AS {
    projection_version: $version
}]->(c)

MERGE (c)-[d:DOCUMENTED_IN {
    projection_version: $version
}]->(a)

RETURN count(DISTINCT c) AS written
"""


ROLLBACK_SAFETY_QUERY = """
MATCH (c:CanonicalEvent)
WHERE c.projection_version = $version
OPTIONAL MATCH (c)-[r]-()
WITH c, collect(r) AS relationships
RETURN
    count(c) AS nodes,
    sum(
        CASE WHEN size(relationships) <> 2
            OR any(r IN relationships
                WHERE type(r) NOT IN [
                    'EVENT_RESOLVED_AS',
                    'DOCUMENTED_IN'
                ]
                OR r.projection_version <> $version
            )
        THEN 1 ELSE 0 END
    ) AS unsafe_nodes
"""


ROLLBACK_QUERY = """
MATCH (c:CanonicalEvent)
WHERE c.projection_version = $version
WITH collect(c) AS nodes
FOREACH (c IN nodes | DETACH DELETE c)
RETURN size(nodes) AS deleted
"""


def event_key(mention_id):
    return hashlib.sha256(
        (
            VERSION + "\0" + mention_id
        ).encode("utf-8")
    ).hexdigest()


def fetch_sources(tx):
    return [
        dict(record)
        for record in tx.run(SOURCE_QUERY)
    ]


def fetch_existing(tx):
    return [
        dict(record)
        for record in tx.run(
            EXISTING_QUERY,
            version=VERSION,
        )
    ]


def check_external_conflicts(tx):
    count = tx.run(
        CONFLICT_QUERY,
        version=VERSION,
    ).single()["conflicts"]

    require(
        count == 0,
        "EventMention already linked to another "
        "canonical event projection",
    )


def build_plan(records):
    rows = []
    seen_mentions = set()
    statistics = Counter()

    for item in records:
        mention_id = item["mention_id"]
        article_id = item["article_id"]

        require(
            isinstance(mention_id, str)
            and bool(mention_id.strip()),
            "EventMention without stable ID",
        )

        require(
            mention_id not in seen_mentions,
            f"Duplicate EventMention ID: {mention_id}",
        )
        seen_mentions.add(mention_id)

        require(
            isinstance(article_id, str)
            and bool(article_id.strip()),
            f"Missing article_id: {mention_id}",
        )

        require(
            item["link_count"] == 1
            and item["linked_article_ids"] == [article_id],
            f"Invalid HAS_EVENT provenance: {mention_id}",
        )

        require(
            isinstance(item["name"], str)
            and bool(item["name"].strip()),
            f"Missing event name: {mention_id}",
        )

        row = {
            "event_key": event_key(mention_id),
            "mention_id": mention_id,
            "article_id": article_id,
            "name": item["name"],
            "kind": item["kind"],
            "type_group": item["type_group"],
            "event_date": item["event_date"],
            "location": item["location"],
            "event_type": item["event_type"],
        }

        rows.append(row)

        statistics["total_mentions"] += 1

        for field in (
            "event_date",
            "location",
            "event_type",
        ):
            if row[field] not in (None, ""):
                statistics[f"with_{field}"] += 1

    require(
        len({r["event_key"] for r in rows}) == len(rows),
        "Canonical event key collision",
    )

    return rows, dict(statistics)


def validate_existing(existing, rows):
    expected = {
        row["event_key"]: row
        for row in rows
    }

    seen = set()

    for record in existing:
        key = record["event_key"]

        require(
            key not in seen,
            f"Duplicate CanonicalEvent: {key}",
        )
        seen.add(key)

        require(
            key in expected,
            f"Unexpected existing CanonicalEvent: {key}",
        )

        source = expected[key]

        require(
            record["mention_id"] == source["mention_id"]
            and record["article_id"] == source["article_id"]
            and record["identity_scope"] == "MENTION_LOCAL"
            and record["name"] == source["name"],
            f"CanonicalEvent identity conflict: {key}",
        )

        require(
            record["mention_links"] == 1
            and record["document_links"] == 1
            and record["mention_ids"]
                == [source["mention_id"]]
            and record["article_ids"]
                == [source["article_id"]],
            f"CanonicalEvent relationship conflict: {key}",
        )

    return len(existing)


def apply_transaction(tx, rows, batch_size):
    check_external_conflicts(tx)

    current, _ = build_plan(fetch_sources(tx))

    require(
        current == rows,
        "EventMention sources changed since preflight",
    )

    validate_existing(fetch_existing(tx), rows)

    total = 0

    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]

        count = tx.run(
            APPLY_QUERY,
            rows=batch,
            version=VERSION,
        ).single()["written"]

        require(
            count == len(batch),
            "Incomplete CanonicalEvent materialization",
        )

        total += count

    existing = fetch_existing(tx)

    require(
        validate_existing(existing, rows) == len(rows),
        "Post-apply validation failed",
    )

    return total


def rollback_transaction(tx):
    safety = tx.run(
        ROLLBACK_SAFETY_QUERY,
        version=VERSION,
    ).single()

    require(
        safety["unsafe_nodes"] == 0,
        "Rollback blocked: canonical events have "
        "unexpected or additional relationships",
    )

    deleted = tx.run(
        ROLLBACK_QUERY,
        version=VERSION,
    ).single()["deleted"]

    require(
        deleted == safety["nodes"],
        "Rollback cardinality mismatch",
    )

    return deleted


def run(ctx: StepContext) -> StepResult:
    args = ctx.args
    database = getattr(args, "database", "neo4j")
    out_dir = getattr(args, "out_dir", str(paths.step_dir(args, "event_projection")))
    batch_size = getattr(args, "batch_size", 250)
    apply = getattr(args, "apply", False)
    rollback = getattr(args, "rollback", False)
    
    require(
        not (apply and rollback),
        "Choose --apply or --rollback",
    )

    require(
        batch_size > 0,
        "Batch size must be positive",
    )

    _ = CFG

    password = os.environ.get("NEO4J_PASSWORD")
    require(password, "NEO4J_PASSWORD is required")

    driver = step_driver(args)

    try:
        driver.verify_connectivity()

        if rollback:
            with driver.session(
                database=database,
            ) as session:
                deleted = session.execute_write(
                    rollback_transaction
                )

            result = {
                "version": VERSION,
                "operation": "ROLLBACK",
                "deleted_canonical_events": deleted,
                "original_mentions_modified": 0,
            }

            print(json.dumps(result, indent=2))
            return StepResult.from_summary(result)

        with driver.session(
            database=database,
            default_access_mode=READ_ACCESS,
        ) as session:

            sources = session.execute_read(
                fetch_sources
            )

            existing = session.execute_read(
                fetch_existing
            )

            session.execute_read(
                check_external_conflicts
            )

        rows, statistics = build_plan(sources)

        already_created = validate_existing(
            existing, rows
        )

        report = {
            "version": VERSION,
            "operation": (
                "APPLY" if apply else "DRY_RUN"
            ),
            "statistics": statistics,
            "canonical_events_planned": len(rows),
            "already_materialized": already_created,
            "identity_scope": "MENTION_LOCAL",
            "cross_article_merges": 0,
            "inferred_event_dates": 0,
            "llm_calls": 0,
            "original_mentions_modified": 0,
            "graph_writes": 0,
        }

        if apply:
            with driver.session(
                database=database,
            ) as session:
                session.run(CREATE_CONSTRAINT).consume()

                written = session.execute_write(
                    apply_transaction,
                    rows,
                    batch_size,
                )

            report["canonical_events_materialized"] = written
            report["graph_writes"] = written

        write_json_stable(
            Path(out_dir) / "summary.json",
            report,
        )

        print(json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        ))
    except Exception as exc:
        print(f"ERROR: {exc}")
        return StepResult.fail(f"{type(exc).__name__}: {exc}")
    finally:
        close_step_driver(args)
        
    return StepResult.from_summary(report)
