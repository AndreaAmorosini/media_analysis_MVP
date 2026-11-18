
"""Canonical graph consolidation.

Builds an aggregated semantic projection over CANONICAL_FACT.

- Uses effective_predicate when available.
- Excludes unresolved OTHER relationships.
- Preserves fact/article provenance.
"""

from __future__ import annotations

from argparse import Namespace
import hashlib
import json
import os

from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from neo4j import GraphDatabase, READ_ACCESS

from presskg.config import CFG
from presskg.common import require
from presskg.common.io import write_json_stable
from presskg.resolution import paths
from presskg.resolution._common import close_step_driver, step_driver
from presskg.resolution.context import StepContext, StepResult



VERSION = "canonical-graph-consolidation-v1"


READ_FACTS = """
MATCH (s:CanonicalEntity)
      -[r:CANONICAL_FACT]->
      (o:CanonicalEntity)

OPTIONAL MATCH (a:Article {id: r.article_id})

WITH s, r, o, collect(a) AS articles

RETURN
    s.projection_key AS subject_key,
    o.projection_key AS object_key,
    r.projection_key AS projection_key,
    r.fact_id AS fact_id,
    r.article_id AS article_id,
    r.source_batch_key AS source_batch_key,
    r.predicate AS original_predicate,
    r.effective_predicate AS effective_predicate,
    r.predicate_refinement_version AS refinement_version,
    r.predicate_refinement_status AS refinement_status,
    size(articles) AS article_count,
    CASE WHEN size(articles) = 1
         THEN articles[0].publication_date
         ELSE null END AS publication_date,
    CASE WHEN size(articles) = 1
         THEN articles[0].source_name
         ELSE null END AS source_name
"""


READ_EXISTING = """
MATCH (s:CanonicalEntity)
      -[r:CANONICAL_RELATION]->
      (o:CanonicalEntity)
WHERE r.consolidation_version = $version
RETURN
    r.aggregate_key AS aggregate_key,
    s.projection_key AS subject_key,
    o.projection_key AS object_key,
    r.predicate AS predicate,
    r.fact_count AS fact_count,
    r.article_count AS article_count,
    r.fact_ids AS fact_ids,
    r.article_ids AS article_ids
"""


APPLY_QUERY = """
UNWIND $rows AS x

MATCH (s:CanonicalEntity {projection_key: x.subject_key})
MATCH (o:CanonicalEntity {projection_key: x.object_key})

MERGE (s)-[r:CANONICAL_RELATION {
    aggregate_key: x.aggregate_key
}]->(o)

SET
    r.consolidation_version = $version,
    r.predicate = x.predicate,
    r.fact_count = x.fact_count,
    r.article_count = x.article_count,
    r.source_count = x.source_count,
    r.fact_ids = x.fact_ids,
    r.article_ids = x.article_ids,
    r.source_names = x.source_names,
    r.first_seen = x.first_seen,
    r.last_seen = x.last_seen,
    r.dated_fact_count = x.dated_fact_count,
    r.undated_fact_count = x.undated_fact_count,
    r.source_batch_keys = x.source_batch_keys,
    r.consolidation_status = 'AGGREGATED'

RETURN count(r) AS written
"""


ROLLBACK_QUERY = """
MATCH ()-[r:CANONICAL_RELATION]->()
WHERE r.consolidation_version = $version
WITH collect(r) AS relationships
FOREACH (r IN relationships | DELETE r)
RETURN size(relationships) AS deleted
"""


def hash_value(value):
    data = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(
        data.encode("utf-8")
    ).hexdigest()


def fetch_facts(tx):
    return [
        dict(record)
        for record in tx.run(READ_FACTS)
    ]


def fetch_existing(tx):
    return [
        dict(record)
        for record in tx.run(
            READ_EXISTING,
            version=VERSION,
        )
    ]


def valid_date(value):
    if value is None or value == "":
        return None

    require(
        isinstance(value, str),
        f"Unexpected publication_date type: {value!r}",
    )

    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid publication date: {value!r}"
        ) from exc

    require(
        parsed.isoformat() == value,
        f"Noncanonical publication date: {value!r}",
    )

    return value


def build_plan(facts):
    """
    Group facts by oriented subject, effective predicate
    and object.

    Every individual fact remains in CANONICAL_FACT.
    """
    groups = defaultdict(list)
    seen_fact_ids = set()
    seen_projection_keys = set()

    statistics = Counter()
    original_predicates = Counter()
    effective_predicates = Counter()

    for fact in facts:
        fid = fact["fact_id"]
        projection_key = fact["projection_key"]

        require(
            isinstance(fid, str) and fid,
            "Canonical fact without fact_id",
        )

        require(
            isinstance(projection_key, str)
            and projection_key,
            f"Missing projection_key: {fid}",
        )

        require(
            fid not in seen_fact_ids,
            f"Duplicate fact_id: {fid}",
        )

        require(
            projection_key not in seen_projection_keys,
            f"Duplicate canonical projection_key: {projection_key}",
        )

        seen_fact_ids.add(fid)
        seen_projection_keys.add(projection_key)

        require(
            isinstance(fact["subject_key"], str)
            and bool(fact["subject_key"]),
            f"Missing subject: {fid}",
        )

        require(
            isinstance(fact["object_key"], str)
            and bool(fact["object_key"]),
            f"Missing object: {fid}",
        )

        require(
            fact["subject_key"] != fact["object_key"],
            f"Unexpected canonical self-loop: {fid}",
        )

        require(
            isinstance(fact["article_id"], str)
            and bool(fact["article_id"]),
            f"Missing article provenance: {fid}",
        )

        require(
            fact["article_count"] == 1,
            f"Article missing or ambiguous: {fid}",
        )

        original = fact["original_predicate"]

        require(
            isinstance(original, str) and original,
            f"Missing original predicate: {fid}",
        )

        effective = fact["effective_predicate"] or original

        require(
            isinstance(effective, str) and effective,
            f"Missing effective predicate: {fid}",
        )

        original_predicates[original] += 1
        effective_predicates[effective] += 1

        if effective != original:
            require(
                original == "OTHER",
                f"Unexpected predicate replacement: {fid}",
            )

            require(
                fact["refinement_status"] == "REFINED"
                and fact["refinement_version"]
                == "predicate-overlay-v1",
                f"Invalid refinement provenance: {fid}",
            )

            statistics["refined_facts"] += 1

        if (
            fact["refinement_status"]
            == "PROPOSED_NEW_PREDICATE"
        ):
            require(
                original == "OTHER" and effective == "OTHER",
                f"Premature taxonomy promotion: {fid}",
            )

            statistics["taxonomy_proposal_facts"] += 1

        statistics["total_facts"] += 1

        if effective == "OTHER":
            statistics["unresolved_other"] += 1
            continue

        group_key = (
            fact["subject_key"],
            effective,
            fact["object_key"],
        )

        groups[group_key].append(fact)
        statistics["semantically_projectable_facts"] += 1

    aggregates = []

    for (subject, predicate, obj), items in sorted(
        groups.items()
    ):
        item_fact_ids = sorted({
            x["fact_id"] for x in items
        })

        require(
            len(item_fact_ids) == len(items),
            "Duplicate fact in aggregation",
        )

        article_ids = sorted({
            x["article_id"] for x in items
        })

        sources = sorted({
            x["source_name"].strip()
            for x in items
            if isinstance(x["source_name"], str)
            and x["source_name"].strip()
        })

        batches = sorted({
            x["source_batch_key"]
            for x in items
            if isinstance(x["source_batch_key"], str)
            and x["source_batch_key"]
        })

        dates = [
            valid_date(x["publication_date"])
            for x in items
        ]

        available_dates = [
            x for x in dates if x is not None
        ]

        aggregate_key = hash_value({
            "version": VERSION,
            "subject": subject,
            "predicate": predicate,
            "object": obj,
        })

        aggregates.append({
            "aggregate_key": aggregate_key,
            "subject_key": subject,
            "object_key": obj,
            "predicate": predicate,
            "fact_count": len(items),
            "article_count": len(article_ids),
            "source_count": len(sources),
            "fact_ids": item_fact_ids,
            "article_ids": article_ids,
            "source_names": sources,
            "source_batch_keys": batches,
            "first_seen": (
                min(available_dates)
                if available_dates else None
            ),
            "last_seen": (
                max(available_dates)
                if available_dates else None
            ),
            "dated_fact_count": len(available_dates),
            "undated_fact_count":
                len(items) - len(available_dates),
        })

    require(
        sum(x["fact_count"] for x in aggregates)
        == statistics["semantically_projectable_facts"],
        "Fact aggregation coverage mismatch",
    )

    require(
        statistics["semantically_projectable_facts"]
        + statistics["unresolved_other"]
        == statistics["total_facts"],
        "Fact coverage mismatch",
    )

    return {
        "version": VERSION,
        "aggregates": aggregates,
        "statistics": dict(statistics),
        "original_predicates":
            dict(sorted(original_predicates.items())),
        "effective_predicates":
            dict(sorted(effective_predicates.items())),
    }


def validate_existing(existing, aggregates):
    """
    Prevent conflicts with an existing projection.

    A previous complete apply can be repeated safely,
    but a different or partial projection must not be
    overwritten silently.
    """
    expected = {
        row["aggregate_key"]: row
        for row in aggregates
    }

    found = {}

    for row in existing:
        key = row["aggregate_key"]

        require(
            key not in found,
            f"Duplicate aggregate relationship: {key}",
        )

        found[key] = row

    if not found:
        return

    require(
        set(found) == set(expected),
        "Existing consolidated graph is incomplete "
        "or based on different source facts",
    )

    for key, current in found.items():
        target = expected[key]

        for field in (
            "subject_key",
            "object_key",
            "predicate",
            "fact_count",
            "article_count",
            "fact_ids",
            "article_ids",
        ):
            require(
                current[field] == target[field],
                f"Existing aggregate mismatch: {key}/{field}",
            )


def apply_transaction(tx, expected_plan, batch_size):
    """
    Rebuild and revalidate source data in the write
    transaction before creating aggregate relationships.
    """
    current_plan = build_plan(fetch_facts(tx))

    require(
        hash_value(current_plan["aggregates"])
        == hash_value(expected_plan["aggregates"]),
        "Source graph changed since dry-run",
    )

    aggregates = expected_plan["aggregates"]

    validate_existing(
        fetch_existing(tx),
        aggregates,
    )

    written = 0

    for start in range(0, len(aggregates), batch_size):
        batch = aggregates[start:start + batch_size]

        count = tx.run(
            APPLY_QUERY,
            rows=batch,
            version=VERSION,
        ).single()["written"]

        require(
            count == len(batch),
            "Aggregate write cardinality mismatch",
        )

        written += count

    require(
        written == len(aggregates),
        "Incomplete consolidated projection",
    )

    validate_existing(
        fetch_existing(tx),
        aggregates,
    )

    return written


def rollback_transaction(tx):
    return tx.run(
        ROLLBACK_QUERY,
        version=VERSION,
    ).single()["deleted"]


def run(ctx: StepContext) -> StepResult:
    args = ctx.args
    database = getattr(args, "database", "neo4j")
    out_dir = getattr(args, "out_dir", str(paths.step_dir(args, "consolidation")))
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

    # Importing CFG initializes the project's existing
    # environment configuration.
    _ = CFG

    password = os.environ.get("NEO4J_PASSWORD")

    require(
        bool(password),
        "NEO4J_PASSWORD is required",
    )

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

            print(json.dumps({
                "version": VERSION,
                "operation": "ROLLBACK",
                "deleted_aggregates": deleted,
                "original_facts_modified": 0,
            }, indent=2))

            return StepResult.ok_result(deleted_aggregates=deleted)

        with driver.session(
            database=database,
            default_access_mode=READ_ACCESS,
        ) as session:
            facts = session.execute_read(fetch_facts)
            existing = session.execute_read(fetch_existing)

        plan = build_plan(facts)

        validate_existing(
            existing,
            plan["aggregates"],
        )

        statistics = plan["statistics"]

        require(
            statistics.get("refined_facts", 0) > 0,
            "No predicate refinements detected. "
            "Apply Phase 6C before consolidation.",
        )

        report = {
            "version": VERSION,
            "operation": (
                "APPLY" if apply else "DRY_RUN"
            ),
            "statistics": statistics,
            "aggregate_relations":
                len(plan["aggregates"]),
            "existing_aggregate_relations":
                len(existing),
            "original_predicates":
                plan["original_predicates"],
            "effective_predicates":
                plan["effective_predicates"],
            "llm_calls": 0,
            "original_facts_modified": 0,
            "graph_writes": 0,
        }

        if apply:
            with driver.session(
                database=database,
            ) as session:
                written = session.execute_write(
                    apply_transaction,
                    plan,
                    batch_size,
                )

            report["graph_writes"] = written
            report["aggregate_relations_written"] = written

        write_json_stable(
            Path(out_dir) / "summary.json",
            report,
        )

        print(json.dumps(
            {
                "version": VERSION,
                "operation": report["operation"],
                "statistics": statistics,
                "aggregate_relations":
                    len(plan["aggregates"]),
                "graph_writes": report["graph_writes"],
                "original_facts_modified": 0,
            },
            ensure_ascii=False,
            indent=2,
        ))
    except Exception as exc:
        print(f"ERROR: {exc}")
        return StepResult.fail(f"{type(exc).__name__}: {exc}")
    finally:
        close_step_driver(args)
        
    return StepResult.from_summary(report)
