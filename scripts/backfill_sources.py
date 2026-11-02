
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from neo4j import GraphDatabase

from presskg.articles import short_hash
from presskg.graph_store import normalize
from presskg.settings import CFG


def read_source(root: Path, rel_path: str):
    path = (root / rel_path).resolve()

    if not path.is_relative_to(root):
        raise ValueError("Percorso fuori da Data")

    data = json.loads(
        path.read_text(encoding="utf-8")
    )

    if "source" not in data:
        return "missing_field", None

    value = data["source"]

    if not isinstance(value, str):
        return "invalid_type", None

    name = " ".join(value.split())

    if not name or name.casefold() == "undefined":
        return "undefined", None

    return "known", name


def write_batch(tx, rows):
    # Sostituisce ogni collegamento precedente.
    tx.run(
        """
        UNWIND $rows AS row

        MATCH (a:Article {id: row.article_id})

        OPTIONAL MATCH
            (a)-[old:PUBLISHED_BY]->(:Source)

        DELETE old

        SET
            a.source_name = row.source_name,
            a.source_status = row.status,
            a.source_type = null
        """,
        rows=rows,
    ).consume()

    known = [
        row for row in rows
        if row["status"] == "known"
    ]

    if known:
        tx.run(
            """
            UNWIND $rows AS row

            MATCH (a:Article {id: row.article_id})

            MERGE (s:Source {
                id: row.source_id
            })

            ON CREATE SET
                s.name = row.source_name

            MERGE (a)-[:PUBLISHED_BY]->(s)
            """,
            rows=known,
        ).consume()


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--data-dir",
        default="Data",
    )

    parser.add_argument(
        "--report",
        default="output/repairs/source_backfill.csv",
    )

    parser.add_argument(
        "--apply",
        action="store_true",
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
    )

    args = parser.parse_args()

    root = Path(args.data_dir).resolve()
    report = Path(args.report)
    report.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    driver = GraphDatabase.driver(
        CFG.neo4j_uri,
        auth=(
            CFG.neo4j_user,
            CFG.neo4j_password,
        ),
    )

    counts = Counter()
    changes = []

    try:
        with driver.session(
            database=CFG.neo4j_database
        ) as session:
            articles = session.run(
                """
                MATCH (a:Article)

                OPTIONAL MATCH
                    (a)-[:PUBLISHED_BY]->(s:Source)

                RETURN
                    a.id AS article_id,
                    a.rel_path AS rel_path,
                    collect(s.name) AS previous_sources
                ORDER BY article_id
                """
            ).data()

            for article in articles:
                try:
                    status, name = read_source(
                        root,
                        article["rel_path"],
                    )
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    UnicodeError,
                ):
                    status, name = "read_error", None

                counts[status] += 1

                row = {
                    "article_id": article["article_id"],
                    "rel_path": article["rel_path"],
                    "previous_sources": " | ".join(
                        str(x)
                        for x in article["previous_sources"]
                        if x is not None
                    ),
                    "status": status,
                    "source_name": name,
                    "source_id": (
                        "source-"
                        + short_hash(normalize(name))
                        if name else None
                    ),
                }

                changes.append(row)

            with report.open(
                "w",
                newline="",
                encoding="utf-8",
            ) as file:
                writer = csv.DictWriter(
                    file,
                    fieldnames=[
                        "article_id",
                        "rel_path",
                        "previous_sources",
                        "status",
                        "source_name",
                        "source_id",
                    ],
                )

                writer.writeheader()
                writer.writerows(changes)

            print("Articoli:", len(articles))
            print("Distribuzione:", dict(counts))
            print("Report:", report)

            if not args.apply:
                print("DRY RUN - nessuna modifica")
                return

            valid = [
                row for row in changes
                if row["status"] in {
                    "known",
                    "undefined",
                }
            ]

            for start in range(
                0,
                len(valid),
                args.batch_size,
            ):
                session.execute_write(
                    write_batch,
                    valid[
                        start:start + args.batch_size
                    ],
                )

            # Rimuove le Source diventate inutilizzate.
            session.run(
                """
                MATCH (s:Source)

                WHERE NOT EXISTS {
                    MATCH (s)--()
                }

                DELETE s
                """
            ).consume()

            print(
                "Articoli aggiornati:",
                len(valid),
            )

    finally:
        driver.close()


if __name__ == "__main__":
    main()
