
"""Predicate Quality Audit.

Read-only audit of OTHER predicates on the materialized
canonical fact graph.

Produces:
  other_facts.jsonl
  semantic_groups.jsonl
  repeated_pairs.jsonl
  refinement_queue.jsonl
  summary.json

"""

from __future__ import annotations

from argparse import Namespace
import hashlib
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

from neo4j import GraphDatabase, READ_ACCESS
from presskg.common import require
from presskg.common.hashing import sha256_file
from presskg.resolution import paths
from presskg.resolution._common import close_step_driver, step_driver
from presskg.validation.batch import default_batch_id
from presskg.validation.materialization import VERSION as FACT_VIEW_VERSION
from presskg.resolution.context import StepContext, StepResult



VERSION = "predicate-quality-audit-v1"
FACT_VERSION = "canonical-fact-projection-v1"

EVIDENCE_FIELDS = (
    "supporting_span",
    "original_quote",
    "evidence_quote",
    "evidence_text",
    "supporting_quote",
    "supporting_text",
    "source_quote",
    "source_text",
    "quote",
)

MAX_EXCERPT_CHARS = 1600


def json_safe(value):
    """Convert Neo4j-native values into JSON-compatible data."""
    if value is None or isinstance(
        value, (str, int, float, bool)
    ):
        return value

    if isinstance(value, dict):
        return {
            str(key): json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (tuple, list, set)):
        return [json_safe(item) for item in value]

    return str(value)


def write_jsonl_safe(path, rows):
    with Path(path).open(
        "w", encoding="utf-8"
    ) as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    json_safe(row),
                    ensure_ascii=False,
                    sort_keys=True,
                ) + "\n"
            )


def write_json_safe(path, value):
    Path(path).write_text(
        json.dumps(
            json_safe(value),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )


def normalize_predicate(value):
    return str(value or "").strip().upper()


def extract_evidence(properties, max_chars):
    """Extract stored evidence without asserting grounding.

    supporting_span and original_quote are kept separately.
    Neither is considered verified against the source article.
    """

    properties = properties or {}

    def clean(value):
        if not isinstance(value, str):
            return None
        text = " ".join(value.split())
        return text or None

    original_quote = clean(
        properties.get("original_quote")
    )
    supporting_span = clean(
        properties.get("supporting_span")
    )

    selected_field = None
    selected_text = None

    for field in EVIDENCE_FIELDS:
        candidate = clean(properties.get(field))

        if candidate:
            selected_field = field
            selected_text = candidate
            break

    if selected_text is None:
        return {
            "field": None,
            "text": None,
            "original_quote": original_quote,
            "supporting_span": supporting_span,
            "truncated": False,
            "grounding_verified": False,
        }

    return {
        "field": selected_field,
        "text": selected_text[:max_chars],
        "original_quote": original_quote,
        "supporting_span": supporting_span,
        "truncated": len(selected_text) > max_chars,
        "grounding_verified": False,
    }


def pair_key(row):
    return (
        row["subject_key"],
        row["object_key"],
    )


def facet_key(row):
    return (
        row["subject_facet"],
        row["object_facet"],
    )


def audit_record(raw, max_chars):
    rel = raw["relationship_properties"] or {}
    fact = raw["fact_properties"] or {}

    fact_id = rel.get("fact_id")
    key = rel.get("projection_key")

    require(
        isinstance(fact_id, str) and fact_id,
        "Canonical relationship missing fact_id",
    )
    require(
        isinstance(key, str) and key,
        f"Missing projection_key: {fact_id}",
    )

    require(
        normalize_predicate(rel.get("predicate"))
        == "OTHER",
        f"Unexpected predicate for {fact_id}",
    )

    source_predicate = fact.get("predicate")

    problems = []

    if raw["fact_count"] != 1:
        problems.append("FACT_CARDINALITY_NOT_ONE")

    if raw["fact_count"] == 1:
        if fact.get("id") != fact_id:
            problems.append("FACT_ID_MISMATCH")

        if (
            source_predicate is None
            or normalize_predicate(source_predicate)
            != normalize_predicate(rel["predicate"])
        ):
            problems.append("SOURCE_PREDICATE_MISMATCH")

        if (
            fact.get("article_id") is not None
            and fact["article_id"] != rel.get("article_id")
        ):
            problems.append("ARTICLE_ID_MISMATCH")

    if not raw["subject_key"] or not raw["object_key"]:
        problems.append("MISSING_CANONICAL_ENDPOINT")

    if raw["subject_key"] == raw["object_key"]:
        problems.append("CANONICAL_SELF_LOOP")

    evidence = extract_evidence(
        fact, max_chars
    )

    return {
        "projection_key": key,
        "fact_id": fact_id,
        "article_id": rel.get("article_id"),
        "source_batch_key":
            rel.get("source_batch_key"),
        "projection_version":
            rel.get("projection_version"),
        "predicate": rel.get("predicate"),
        "subject_key": raw["subject_key"],
        "object_key": raw["object_key"],
        "subject_name": raw["subject_name"],
        "object_name": raw["object_name"],
        "subject_facet": raw["subject_facet"]
            or "UNKNOWN",
        "object_facet": raw["object_facet"]
            or "UNKNOWN",
        "evidence": evidence,
        "fact_property_keys": sorted(fact.keys()),
        "structural_problems": sorted(set(problems)),
    }


PREDICATE_QUERY = """
MATCH ()-[r:CANONICAL_FACT]->()
WHERE r.projection_version = $version
  AND ($batch IS NULL OR r.source_batch_key = $batch)
RETURN
    r.predicate AS predicate,
    count(r) AS facts
ORDER BY facts DESC, predicate
"""


OTHER_QUERY = """
MATCH (s:CanonicalEntity)-[r:CANONICAL_FACT]->
      (o:CanonicalEntity)

WHERE r.projection_version = $version
  AND ($batch IS NULL OR r.source_batch_key = $batch)
  AND toUpper(trim(coalesce(r.predicate, ''))) = 'OTHER'

OPTIONAL MATCH (f:ValidatedFact {id: r.fact_id})

WITH s, r, o, collect(f) AS facts

RETURN
    properties(r) AS relationship_properties,
    s.projection_key AS subject_key,
    o.projection_key AS object_key,
    s.name AS subject_name,
    o.name AS object_name,
    s.semantic_facet AS subject_facet,
    o.semantic_facet AS object_facet,
    size(facts) AS fact_count,

    CASE WHEN size(facts) = 1
         THEN properties(facts[0])
         ELSE null
    END AS fact_properties

ORDER BY r.projection_key
"""


def read_snapshot(session, batch):
    def transaction(tx):
        predicate_rows = [
            dict(row)
            for row in tx.run(
                PREDICATE_QUERY,
                version=FACT_VERSION,
                batch=batch,
            )
        ]

        other_rows = [
            dict(row)
            for row in tx.run(
                OTHER_QUERY,
                version=FACT_VERSION,
                batch=batch,
            )
        ]

        return predicate_rows, other_rows

    # Both statements execute in one read transaction.
    return session.execute_read(transaction)


def group_semantics(rows, sample_size):
    groups = defaultdict(list)

    for row in rows:
        groups[facet_key(row)].append(row)

    output = []

    for (subject_facet, object_facet), items in groups.items():
        ordered = sorted(
            items,
            key=lambda row: (
                -row["pair_occurrences"],
                row["fact_id"],
            ),
        )

        output.append({
            "subject_facet": subject_facet,
            "object_facet": object_facet,
            "fact_count": len(items),
            "distinct_entity_pairs": len({
                pair_key(row) for row in items
            }),
            "facts_with_evidence": sum(
                row["evidence"]["text"] is not None
                for row in items
            ),
            "facts_with_structural_problems": sum(
                bool(row["structural_problems"])
                for row in items
            ),
            "examples": [
                {
                    "fact_id": row["fact_id"],
                    "subject_name":
                        row["subject_name"],
                    "object_name":
                        row["object_name"],
                    "evidence":
                        row["evidence"],
                }
                for row in ordered[:sample_size]
            ],
        })

    return sorted(
        output,
        key=lambda item: (
            -item["fact_count"],
            item["subject_facet"],
            item["object_facet"],
        ),
    )


def repeated_pairs(rows, min_occurrences, sample_size):
    by_pair = defaultdict(list)

    for row in rows:
        by_pair[pair_key(row)].append(row)

    output = []

    for (subject_key, object_key), items in by_pair.items():
        if len(items) < min_occurrences:
            continue

        ordered = sorted(
            items,
            key=lambda row: row["fact_id"],
        )

        output.append({
            "subject_key": subject_key,
            "object_key": object_key,
            "subject_name":
                ordered[0]["subject_name"],
            "object_name":
                ordered[0]["object_name"],
            "subject_facet":
                ordered[0]["subject_facet"],
            "object_facet":
                ordered[0]["object_facet"],
            "other_fact_count": len(items),
            "distinct_articles": len({
                row["article_id"]
                for row in items
                if row["article_id"]
            }),
            "fact_ids_sample": [
                row["fact_id"]
                for row in ordered[:sample_size]
            ],
        })

    return sorted(
        output,
        key=lambda row: (
            -row["other_fact_count"],
            row["subject_key"],
            row["object_key"],
        ),
    )


def queue_record(row):
    evidence_present = (
        row["evidence"]["text"] is not None
    )

    if row["structural_problems"]:
        action = "REPAIR_GRAPH_CONSISTENCY"
        priority = 0
    elif not evidence_present:
        action = "RETRIEVE_SOURCE_EVIDENCE"
        priority = 1
    else:
        action = "VERIFY_PREDICATE_FROM_EVIDENCE"
        priority = 2

    # Priority is workflow ordering, not confidence.
    return {
        "fact_id": row["fact_id"],
        "projection_key": row["projection_key"],
        "article_id": row["article_id"],
        "subject_key": row["subject_key"],
        "object_key": row["object_key"],
        "subject_name": row["subject_name"],
        "object_name": row["object_name"],
        "subject_facet": row["subject_facet"],
        "object_facet": row["object_facet"],
        "current_predicate": "OTHER",
        "pair_occurrences":
            row["pair_occurrences"],
        "evidence": row["evidence"],
        "structural_problems":
            row["structural_problems"],
        "next_action": action,
        "workflow_priority": priority,
        "proposed_predicate": None,
        "classification_status": "NOT_CLASSIFIED",
    }


def run(ctx: StepContext) -> StepResult:
    args = ctx.args
    database = getattr(args, "database", "neo4j")
    # in pipeline il batch e' quello derivato dai modelli configurati
    # (o --batch-id); standalone resta overridabile con source_batch_key
    source_batch_key = getattr(args, "source_batch_key", None) or (
        f"{FACT_VIEW_VERSION}:{getattr(args, 'batch_id', None) or default_batch_id()}"
    )
    out_dir = getattr(args, "out_dir", str(paths.step_dir(args, "quality_audit")))
    sample_size = getattr(args, "sample_size", 8)
    min_pair_occurrences = getattr(args, "min_pair_occurrences", 3)
    max_excerpt_chars = getattr(args, "max_excerpt_chars", MAX_EXCERPT_CHARS)
    
    try:
    
        require(
            sample_size > 0,
            "--sample-size must be positive",
        )
        require(
            min_pair_occurrences >= 2,
            "--min-pair-occurrences must be >= 2",
        )
        require(
            max_excerpt_chars > 0,
            "--max-excerpt-chars must be positive",
        )

        out = Path(out_dir)
        require(
            not out.exists(),
            f"Output directory already exists: {out}",
        )

        password = os.environ.get("NEO4J_PASSWORD")
        require(
            password,
            "NEO4J_PASSWORD is required",
        )

        driver = step_driver(args)

        try:
            driver.verify_connectivity()

            with driver.session(
                database=database,
                default_access_mode=READ_ACCESS,
            ) as session:
                predicate_rows, raw_rows = read_snapshot(
                    session, source_batch_key
                )
        finally:
            close_step_driver(args)

        predicate_counts = Counter()

        for entry in predicate_rows:
            predicate_counts[
                normalize_predicate(entry["predicate"])
            ] += entry["facts"]

        other_rows = [
            audit_record(
                raw,
                max_excerpt_chars,
            )
            for raw in raw_rows
        ]

        keys = [row["projection_key"] for row in other_rows]
        ids = [row["fact_id"] for row in other_rows]

        require(
            len(keys) == len(set(keys)),
            "Duplicate OTHER projection keys",
        )
        require(
            len(ids) == len(set(ids)),
            "Duplicate OTHER fact IDs",
        )
        require(
            len(other_rows)
            == predicate_counts["OTHER"],
            "OTHER count differs between graph queries",
        )

        pair_counts = Counter(
            pair_key(row) for row in other_rows
        )

        for row in other_rows:
            row["pair_occurrences"] = pair_counts[
                pair_key(row)
            ]

        other_rows.sort(
            key=lambda row: row["projection_key"]
        )

        groups = group_semantics(
            other_rows, sample_size
        )

        repeats = repeated_pairs(
            other_rows,
            min_pair_occurrences,
            sample_size,
        )

        queue = sorted(
            (queue_record(row) for row in other_rows),
            key=lambda item: (
                item["workflow_priority"],
                -item["pair_occurrences"],
                item["fact_id"],
            ),
        )

        error_counts = Counter()
        action_counts = Counter()
        evidence_fields = Counter()
        facet_counts = Counter()

        for row in other_rows:
            error_counts.update(
                row["structural_problems"]
            )
            facet_counts[
                f"{row['subject_facet']} -> "
                f"{row['object_facet']}"
            ] += 1

            if row["evidence"]["field"]:
                evidence_fields[
                    row["evidence"]["field"]
                ] += 1

        for entry in queue:
            action_counts[entry["next_action"]] += 1

        out.mkdir(parents=True)

        out_files = {
            "other_facts.jsonl": other_rows,
            "semantic_groups.jsonl": groups,
            "repeated_pairs.jsonl": repeats,
            "refinement_queue.jsonl": queue,
        }

        for name, data in out_files.items():
            write_jsonl_safe(out / name, data)

        total_facts = sum(predicate_counts.values())
        other_count = len(other_rows)

        summary = {
            "version": VERSION,
            "source_fact_projection_version":
                FACT_VERSION,
            "source_batch_key":
                source_batch_key,
            "database": database,
            "canonical_fact_count": total_facts,
            "other_fact_count": other_count,
            "other_fraction": (
                other_count / total_facts
                if total_facts else 0.0
            ),
            "distinct_predicates":
                len(predicate_counts),
            "predicate_counts":
                dict(sorted(predicate_counts.items())),
            "semantic_group_count": len(groups),
            "semantic_group_counts":
                dict(sorted(facet_counts.items())),
            "distinct_other_entity_pairs":
                len(pair_counts),
            "repeated_pair_groups":
                len(repeats),
            "evidence_field_counts":
                dict(sorted(evidence_fields.items())),
            "facts_with_evidence_preview":
                sum(
                    row["evidence"]["text"] is not None
                    for row in other_rows
                ),
            "facts_without_evidence_preview":
                sum(
                    row["evidence"]["text"] is None
                    for row in other_rows
                ),
            "structural_problem_counts":
                dict(sorted(error_counts.items())),
            "next_action_counts":
                dict(sorted(action_counts.items())),
            "proposed_predicates": 0,
            "approved_corrections": 0,
            "llm_calls": 0,
            "graph_writes": 0,
            "checksums": {
                name: sha256_file(out / name)
                for name in out_files
            },
        }

        write_json_safe(out / "summary.json", summary)

        print(json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ))
        
    except Exception as exc:
        print(f"ERROR: {exc}")
        return StepResult.fail(f"{type(exc).__name__}: {exc}")
    
    return StepResult.from_summary(summary)
