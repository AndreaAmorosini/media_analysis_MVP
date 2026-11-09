
"""PressKG 7C: deterministic canonical graph audit.

Checks canonical entities, facts, aggregated relations,
canonical events, validated evidence and provenance.

Read-only. No LLM calls. No checksum validation.
"""

from __future__ import annotations

import argparse
import json
import os
import re

from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from neo4j import GraphDatabase, READ_ACCESS
from presskg.settings import CFG


EVENT_VERSION = "canonical-event-projection-v1"
EVIDENCE_VERSION = "validated-event-evidence-v1"
CONSOLIDATION_VERSION = "canonical-graph-consolidation-v1"


QUERIES = {
    "entities": """
        MATCH (n:CanonicalEntity)
        RETURN n.projection_key AS key
    """,

    "facts": """
        MATCH (s:CanonicalEntity)
              -[r:CANONICAL_FACT]->
              (o:CanonicalEntity)
        RETURN
            s.projection_key AS subject_key,
            o.projection_key AS object_key,
            r.fact_id AS fact_id,
            r.projection_key AS projection_key,
            r.article_id AS article_id,
            r.predicate AS predicate,
            r.effective_predicate AS effective_predicate
    """,

    "aggregates": """
        MATCH (s:CanonicalEntity)
              -[r:CANONICAL_RELATION]->
              (o:CanonicalEntity)
        RETURN
            s.projection_key AS subject_key,
            o.projection_key AS object_key,
            r.aggregate_key AS aggregate_key,
            r.consolidation_version AS version,
            r.predicate AS predicate,
            r.fact_count AS fact_count,
            r.article_count AS article_count,
            r.fact_ids AS fact_ids,
            r.article_ids AS article_ids,
            r.first_seen AS first_seen,
            r.last_seen AS last_seen
    """,

    "articles": """
        MATCH (a:Article)
        RETURN
            a.id AS id,
            a.publication_date AS publication_date,
            a.source_name AS source_name
    """,

    "events": """
        MATCH (e:CanonicalEvent)
        OPTIONAL MATCH
            (m:EventMention)-[r:EVENT_RESOLVED_AS]->(e)
        OPTIONAL MATCH
            (e)-[d:DOCUMENTED_IN]->(a:Article)
        RETURN
            e.event_key AS event_key,
            e.projection_version AS version,
            e.identity_scope AS scope,
            e.mention_id AS mention_id,
            e.article_id AS article_id,
            count(DISTINCT r) AS mention_links,
            count(DISTINCT d) AS article_links,
            collect(DISTINCT m.id) AS mention_ids,
            collect(DISTINCT a.id) AS article_ids
    """,

    "mentions": """
        MATCH (m:EventMention)
        OPTIONAL MATCH
            (m)-[r:EVENT_RESOLVED_AS]->(e:CanonicalEvent)
        RETURN
            m.id AS mention_id,
            m.article_id AS article_id,
            count(DISTINCT r) AS canonical_links
    """,

    "evidence": """
        MATCH (e:CanonicalEvent)
              -[r:SUPPORTED_BY]->
              (f:ValidatedFact)
        WHERE r.projection_version = $evidence_version
        RETURN
            e.event_key AS event_key,
            e.article_id AS event_article,
            f.id AS fact_id,
            f.article_id AS fact_article,
            r.evidence_key AS evidence_key,
            r.article_id AS relationship_article,
            r.event_role AS role,
            r.claim_ids AS claim_ids,
            EXISTS {
                MATCH (m:EventMention)
                      -[:EVENT_RESOLVED_AS]->(e)
                MATCH (a:Article)-[:HAS_EVENT]->(m)
                WHERE m.id = e.mention_id
                  AND a.id = e.article_id
            } AS mention_path_exists,
            EXISTS {
                MATCH (c:Claim)
                      -[:HAS_VALIDATED_FACT]->(f)
                MATCH (c)-[p:SUBJECT|OBJECT]->(m:EventMention)
                      -[:EVENT_RESOLVED_AS]->(e)
                WHERE c.id IN r.claim_ids
                  AND type(p) = r.event_role
            } AS claim_path_exists
    """,
}


def fail_if(condition, message, errors, samples):
    if condition:
        errors[message] += 1
        if len(samples) < 100:
            samples.append(message)


def date_string(value):
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def run_query(session, query, **parameters):
    return [
        dict(record)
        for record in session.run(query, **parameters)
    ]


def audit(data):
    errors = Counter()
    warnings = Counter()
    samples = []

    entities = data["entities"]
    facts = data["facts"]
    aggregates = data["aggregates"]
    articles = data["articles"]
    events = data["events"]
    mentions = data["mentions"]
    evidence = data["evidence"]

    # --------------------------------------------------
    # Canonical entity identity
    # --------------------------------------------------

    entity_keys = [row["key"] for row in entities]

    fail_if(
        any(not key for key in entity_keys),
        "CanonicalEntity without projection_key",
        errors, samples,
    )

    fail_if(
        len(set(entity_keys)) != len(entity_keys),
        "Duplicate CanonicalEntity projection_key",
        errors, samples,
    )

    # --------------------------------------------------
    # Article provenance
    # --------------------------------------------------

    article_index = {}

    for row in articles:
        article_id = row["id"]

        fail_if(
            not article_id or article_id in article_index,
            "Missing or duplicate Article.id",
            errors, samples,
        )

        article_index[article_id] = row

        publication_date = date_string(
            row["publication_date"]
        )

        if publication_date:
            try:
                if not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}",
                    publication_date,
                ):
                    raise ValueError("Invalid format")

                date.fromisoformat(publication_date)
            except ValueError:
                errors["Invalid article publication_date"] += 1

        if not row["source_name"] or (
            str(row["source_name"]).strip().lower()
            == "undefined"
        ):
            warnings["Article without identified source"] += 1

    # --------------------------------------------------
    # CANONICAL_FACT and CANONICAL_RELATION
    # --------------------------------------------------

    expected_groups = defaultdict(
        lambda: {"facts": set(), "articles": set()}
    )
    fact_ids = set()
    projection_keys = set()

    for row in facts:
        fid = row["fact_id"]
        pkey = row["projection_key"]

        fail_if(
            not fid or fid in fact_ids,
            "Missing or duplicate CANONICAL_FACT.fact_id",
            errors, samples,
        )
        fact_ids.add(fid)

        fail_if(
            not pkey or pkey in projection_keys,
            "Missing or duplicate fact projection_key",
            errors, samples,
        )
        projection_keys.add(pkey)

        fail_if(
            row["article_id"] not in article_index,
            "Canonical fact with missing Article",
            errors, samples,
        )

        effective = (
            row["effective_predicate"] or row["predicate"]
        )

        fail_if(
            not effective,
            "Canonical fact without effective predicate",
            errors, samples,
        )

        if effective == "OTHER":
            continue

        key = (
            row["subject_key"],
            effective,
            row["object_key"],
        )

        expected_groups[key]["facts"].add(fid)
        expected_groups[key]["articles"].add(
            row["article_id"]
        )

    actual_groups = {}

    for row in aggregates:
        key = (
            row["subject_key"],
            row["predicate"],
            row["object_key"],
        )

        fail_if(
            key in actual_groups,
            "Duplicate aggregate semantic triple",
            errors, samples,
        )
        actual_groups[key] = row

        fail_if(
            row["predicate"] == "OTHER",
            "OTHER present in consolidated relations",
            errors, samples,
        )

        fail_if(
            row["version"] != CONSOLIDATION_VERSION,
            "Unexpected aggregation version",
            errors, samples,
        )

        expected = expected_groups.get(key)

        if expected is None:
            errors["Aggregate without supporting canonical facts"] += 1
            continue

        actual_facts = set(row["fact_ids"] or [])
        actual_articles = set(row["article_ids"] or [])

        fail_if(
            actual_facts != expected["facts"]
            or row["fact_count"] != len(expected["facts"]),
            "Aggregate fact coverage mismatch",
            errors, samples,
        )

        fail_if(
            actual_articles != expected["articles"]
            or row["article_count"] != len(
                expected["articles"]
            ),
            "Aggregate article coverage mismatch",
            errors, samples,
        )

    missing = set(expected_groups) - set(actual_groups)

    if missing:
        errors["Missing aggregate semantic triples"] += len(
            missing
        )

    # --------------------------------------------------
    # CanonicalEvent and EventMention
    # --------------------------------------------------

    event_keys = set()

    for row in events:
        key = row["event_key"]

        fail_if(
            not key or key in event_keys,
            "Missing or duplicate CanonicalEvent.event_key",
            errors, samples,
        )
        event_keys.add(key)

        fail_if(
            row["version"] != EVENT_VERSION,
            "Unexpected CanonicalEvent projection version",
            errors, samples,
        )

        fail_if(
            row["scope"] != "MENTION_LOCAL",
            "Unexpected event identity scope",
            errors, samples,
        )

        fail_if(
            row["mention_links"] != 1
            or row["mention_ids"] != [row["mention_id"]],
            "CanonicalEvent without unique mention provenance",
            errors, samples,
        )

        fail_if(
            row["article_links"] != 1
            or row["article_ids"] != [row["article_id"]],
            "CanonicalEvent without unique article provenance",
            errors, samples,
        )

        fail_if(
            row["article_id"] not in article_index,
            "CanonicalEvent references missing Article",
            errors, samples,
        )

    for row in mentions:
        fail_if(
            row["canonical_links"] != 1,
            "EventMention without unique canonical projection",
            errors, samples,
        )

    # --------------------------------------------------
    # Validated event evidence
    # --------------------------------------------------

    evidence_keys = set()

    for row in evidence:
        key = row["evidence_key"]

        fail_if(
            not key or key in evidence_keys,
            "Missing or duplicate event evidence key",
            errors, samples,
        )
        evidence_keys.add(key)

        fail_if(
            row["event_article"] != row["fact_article"]
            or row["event_article"] != row["relationship_article"],
            "Evidence article provenance mismatch",
            errors, samples,
        )

        fail_if(
            row["role"] not in ("SUBJECT", "OBJECT"),
            "Invalid event evidence role",
            errors, samples,
        )

        fail_if(
            not row["mention_path_exists"],
            "Missing event mention provenance path",
            errors, samples,
        )

        fail_if(
            not row["claim_path_exists"],
            "Missing validated claim provenance path",
            errors, samples,
        )

    if not evidence:
        warnings["No validated event evidence links"] += 1

    statistics = {
        "canonical_entities": len(entities),
        "canonical_facts": len(facts),
        "canonical_relations": len(aggregates),
        "semantic_triples_expected": len(expected_groups),
        "articles": len(articles),
        "canonical_events": len(events),
        "event_mentions": len(mentions),
        "validated_event_evidence_links": len(evidence),
        "unique_events_with_evidence": len({
            row["event_key"] for row in evidence
        }),
    }

    return {
        "version": "canonical-graph-audit-v1",
        "status": "PASS" if not errors else "FAIL",
        "statistics": statistics,
        "errors": dict(sorted(errors.items())),
        "warnings": dict(sorted(warnings.items())),
        "error_samples": samples,
        "llm_calls": 0,
        "graph_writes": 0,
        "checksum_validation": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="neo4j")
    parser.add_argument(
        "--out-dir",
        default="output/resolution/canonical-graph-audit-v1",
    )
    args = parser.parse_args()

    _ = CFG

    password = os.getenv("NEO4J_PASSWORD")
    if not password:
        raise RuntimeError("NEO4J_PASSWORD is required")

    driver = GraphDatabase.driver(
        os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        auth=(
            os.getenv("NEO4J_USER", "neo4j"),
            password,
        ),
    )

    try:
        driver.verify_connectivity()

        with driver.session(
            database=args.database,
            default_access_mode=READ_ACCESS,
        ) as session:
            data = {
                name: run_query(
                    session,
                    query,
                    evidence_version=EVIDENCE_VERSION,
                ) if name == "evidence"
                else run_query(session, query)
                for name, query in QUERIES.items()
            }

        report = audit(data)

        output = Path(args.out_dir)
        output.mkdir(parents=True, exist_ok=True)

        target = output / "summary.json"
        tmp = output / "summary.json.tmp"

        tmp.write_text(
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
        tmp.replace(target)

        print(json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        ))

        if report["status"] != "PASS":
            raise SystemExit(1)

    finally:
        driver.close()


if __name__ == "__main__":
    main()
