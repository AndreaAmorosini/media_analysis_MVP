import argparse

from collections import Counter

from neo4j import GraphDatabase

from .ontology import (
    canonical_predicate,
    VERSION,
)
from .settings import CFG


UPDATE_QUERY = """
UNWIND $rows AS row

MATCH (c:Claim {id:row.id})
WHERE c.predicate = 'OTHER'

SET
    c.predicate = row.canonical,
    c.predicate_family = row.family,
    c.normalization_version = $version

WITH c, row

OPTIONAL MATCH ()-[r:FACT {claim_id:c.id}]->()

FOREACH (
    rel IN CASE
        WHEN r IS NULL THEN []
        ELSE [r]
    END |
    SET
        rel.predicate = row.canonical,
        rel.predicate_family = row.family,
        rel.normalization_version = $version
)
"""


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--apply", action="store_true")

    args = parser.parse_args()

    driver = GraphDatabase.driver(CFG.neo4j_uri, auth=(CFG.neo4j_user, CFG.neo4j_password))

    try:
        with driver.session(database=CFG.neo4j_database) as session:

            records = session.run(
                """
                MATCH (c:Claim {predicate:'OTHER'})
                RETURN
                    c.id AS id,
                    c.predicate_raw AS raw
                """
            ).data()

            changes = []
            distribution = Counter()

            for record in records:
                canonical, family = canonical_predicate(record["raw"])

                if canonical == "OTHER":
                    continue

                changes.append({
                    "id": record["id"],
                    "canonical": canonical,
                    "family": family,
                })

                distribution[canonical] += 1

            print("OTHER iniziali:", len(records))
            print("Normalizzabili:", len(changes))
            print("OTHER residui stimati:", len(records) - len(changes))

            for name, count in distribution.most_common():
                print(f"{name}: {count}")

            if not args.apply:
                print("DRY RUN: nessuna scrittura.")
                return

            for start in range(0, len(changes), 300):
                batch = changes[start:start + 300]

                session.run(UPDATE_QUERY, rows=batch, version=VERSION).consume()

            print("Normalizzazione completata.")

    finally:
        driver.close()


if __name__ == "__main__":
    main()