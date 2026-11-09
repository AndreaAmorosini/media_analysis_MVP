
"""PressKG 7B.1: Event and Evidence Inventory.

Inspect existing event nodes, properties and relationships.

- Read-only Neo4j.
- No LLM calls.
- No checksum validation.
- No changes to existing graph data.
- Produces a reproducible structural audit.
"""

from __future__ import annotations

import argparse
import json
import os

from collections import Counter, defaultdict
from pathlib import Path

from neo4j import GraphDatabase, READ_ACCESS

from presskg.settings import CFG


VERSION = "event-evidence-inventory-v1"

EVENT_QUERY = """
MATCH (e:EventMention)
RETURN
    elementId(e) AS element_id,
    labels(e) AS labels,
    properties(e) AS properties
ORDER BY element_id
"""

EVENT_LABELS_QUERY = """
CALL db.labels() YIELD label
WHERE toLower(label) CONTAINS 'event'
RETURN label
ORDER BY label
"""

EVENT_RELATIONS_QUERY = """
MATCH (e:EventMention)-[r]-(n)
UNWIND labels(n) AS neighbor_label
RETURN
    type(r) AS relationship_type,
    neighbor_label,
    CASE WHEN startNode(r) = e
         THEN 'OUTGOING'
         ELSE 'INCOMING'
    END AS direction,
    count(*) AS occurrences
ORDER BY occurrences DESC
"""

ARTICLE_LINKS_QUERY = """
MATCH (e:EventMention)
OPTIONAL MATCH (e)--(a:Article)
RETURN
    elementId(e) AS element_id,
    count(DISTINCT a) AS direct_articles
"""

GRAPH_COUNTS_QUERY = """
MATCH (n)
UNWIND labels(n) AS label
WITH label
WHERE label IN [
    'Article',
    'Event',
    'EventMention',
    'Topic',
    'TopicMention',
    'ValidatedFact',
    'ValidatedMention',
    'CanonicalEntity',
    'PredicateProposal'
]
RETURN label, count(*) AS total
ORDER BY label
"""

DATE_FIELDS = (
    "date",
    "event_date",
    "start_date",
    "end_date",
    "event_datetime",
    "start_time",
    "timestamp",
)

LOCATION_FIELDS = (
    "location",
    "place",
    "venue",
    "city",
    "municipality",
    "location_id",
    "location_key",
)

TYPE_FIELDS = (
    "event_type",
    "type",
    "category",
    "event_category",
)

ARTICLE_FIELDS = (
    "article_id",
    "source_article_id",
    "article_ids",
)

IDENTITY_FIELDS = (
    "id",
    "event_id",
    "canonical_id",
    "projection_key",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def json_default(value):
    return str(value)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    temp = path.with_suffix(path.suffix + ".tmp")

    temp.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            default=json_default,
        ) + "\n",
        encoding="utf-8",
    )

    temp.replace(path)


def save_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=json_default,
                ) + "\n"
            )


def has_value(properties, fields):
    for field in fields:
        value = properties.get(field)

        if value is None or value == "" or value == []:
            continue

        return True

    return False


def selected_fields(properties, fields):
    return {
        field: properties[field]
        for field in fields
        if field in properties
        and properties[field] is not None
    }


def inspect_events(events, article_links, sample_size):
    property_counts = Counter()
    label_counts = Counter()
    coverage = Counter()
    link_distribution = Counter()

    samples = []
    potential_duplicates = defaultdict(list)

    for event in events:
        element_id = event["element_id"]
        properties = event["properties"] or {}

        label_counts.update(event["labels"] or [])
        property_counts.update(properties.keys())

        has_date = has_value(properties, DATE_FIELDS)
        has_location = has_value(
            properties, LOCATION_FIELDS
        )
        has_type = has_value(properties, TYPE_FIELDS)
        has_article_ref = has_value(
            properties, ARTICLE_FIELDS
        )

        direct_articles = article_links.get(element_id, 0)

        coverage["total"] += 1

        coverage["with_date"] += int(has_date)
        coverage["with_location"] += int(has_location)
        coverage["with_type"] += int(has_type)
        coverage["with_article_property"] += int(
            has_article_ref
        )
        coverage["with_direct_article_link"] += int(
            direct_articles > 0
        )
        coverage["without_direct_article_link"] += int(
            direct_articles == 0
        )

        if has_date and has_location and has_type:
            coverage["with_date_location_type"] += 1

        link_distribution[
            str(direct_articles)
        ] += 1

        identity = selected_fields(
            properties,
            IDENTITY_FIELDS,
        )

        for field, value in identity.items():
            if isinstance(value, (str, int, float)):
                potential_duplicates[
                    f"{field}:{value}"
                ].append(element_id)

        if len(samples) < sample_size:
            fields_to_show = (
                IDENTITY_FIELDS
                + DATE_FIELDS
                + LOCATION_FIELDS
                + TYPE_FIELDS
                + ARTICLE_FIELDS
            )

            samples.append({
                "element_id_diagnostic": element_id,
                "labels": event["labels"],
                "property_keys": sorted(properties),
                "selected_properties": selected_fields(
                    properties,
                    fields_to_show,
                ),
                "direct_articles": direct_articles,
                "has_date": has_date,
                "has_location": has_location,
                "has_type": has_type,
            })

    duplicate_candidates = [
        {
            "identity_value": key,
            "event_count": len(ids),
            "element_ids_diagnostic": ids[:10],
        }
        for key, ids in potential_duplicates.items()
        if len(ids) > 1
    ]

    duplicate_candidates.sort(
        key=lambda row: (
            -row["event_count"],
            row["identity_value"],
        )
    )

    return {
        "coverage": dict(coverage),
        "property_counts": dict(
            sorted(
                property_counts.items(),
                key=lambda x: (-x[1], x[0]),
            )
        ),
        "label_counts": dict(sorted(label_counts.items())),
        "direct_article_link_distribution": dict(
            sorted(
                link_distribution.items(),
                key=lambda x: int(x[0]),
            )
        ),
        "samples": samples,
        "duplicate_identity_candidates":
            duplicate_candidates,
    }


def run(args):
    require(
        args.sample_size >= 0,
        "sample-size must be non-negative",
    )

    # Import initializes the existing PressKG environment.
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

        with driver.session(
            database=args.database,
            default_access_mode=READ_ACCESS,
        ) as session:

            labels = [
                record["label"]
                for record in session.run(
                    EVENT_LABELS_QUERY
                )
            ]

            counts = {
                record["label"]: record["total"]
                for record in session.run(
                    GRAPH_COUNTS_QUERY
                )
            }

            events = [
                dict(record)
                for record in session.run(EVENT_QUERY)
            ]

            relations = [
                dict(record)
                for record in session.run(
                    EVENT_RELATIONS_QUERY
                )
            ]

            article_links = {
                record["element_id"]:
                    record["direct_articles"]
                for record in session.run(
                    ARTICLE_LINKS_QUERY
                )
            }

        audit = inspect_events(
            events,
            article_links,
            args.sample_size,
        )

        summary = {
            "version": VERSION,
            "database": args.database,
            "event_like_labels": labels,
            "graph_node_counts": counts,
            "event_count": len(events),
            "coverage": audit["coverage"],
            "property_counts": audit["property_counts"],
            "event_label_counts": audit["label_counts"],
            "direct_article_link_distribution":
                audit["direct_article_link_distribution"],
            "relation_patterns": relations,
            "potential_duplicate_identity_groups":
                len(audit["duplicate_identity_candidates"]),
            "llm_calls": 0,
            "graph_writes": 0,
            "checksum_validation": False,
            "notes": [
                "Article links are counted only when direct.",
                "No event coreference is inferred.",
                "Publication date is not assumed to be event date.",
                "Element IDs are diagnostic, not stable IDs.",
                "Property completeness does not establish correctness.",
            ],
        }

        out = Path(args.out_dir)

        save_json(
            out / "summary.json",
            summary,
        )

        save_jsonl(
            out / "event_samples.jsonl",
            audit["samples"],
        )

        save_jsonl(
            out / "potential_duplicate_events.jsonl",
            audit["duplicate_identity_candidates"],
        )

        save_jsonl(
            out / "event_relation_patterns.jsonl",
            relations,
        )

        print(json.dumps(
            {
                "version": VERSION,
                "event_count": len(events),
                "event_like_labels": labels,
                "coverage": audit["coverage"],
                "relation_patterns": relations[:20],
                "potential_duplicate_identity_groups":
                    len(audit["duplicate_identity_candidates"]),
                "graph_writes": 0,
            },
            ensure_ascii=False,
            indent=2,
            default=json_default,
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
            "event-evidence-inventory-v1"
        ),
    )

    parser.add_argument(
        "--sample-size",
        type=int,
        default=100,
    )

    run(parser.parse_args())


if __name__ == "__main__":
    main()
