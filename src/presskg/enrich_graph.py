
import argparse
import re

from collections import Counter
from neo4j import GraphDatabase

from .articles import short_hash
from .settings import CFG


VERSION = "topics-v1"
NORMALIZER_VERSION = "predicates-v1"


# Soltanto corrispondenze lessicali controllate.
PREDICATE_MAP = {
    "COLLABORATES_WITH": "COLLABORATES_WITH",
    "PARTNERS_WITH": "COLLABORATES_WITH",
    "WORKS_WITH": "COLLABORATES_WITH",
    "COOPERATES_WITH": "COLLABORATES_WITH",

    "CONTAINS": "CONTAINS",
    "INCLUDES": "CONTAINS",

    "PROVIDES": "PROVIDES",
    "SUPPLIES": "PROVIDES",

    "FUNDS": "FUNDS",
    "FINANCES": "FUNDS",

    "LOCATED_IN": "LOCATED_IN",
    "IS_LOCATED_IN": "LOCATED_IN",

    "ACCEPTS": "ACCEPTS",
    "OPERATES": "OPERATES",
    "IMPROVES": "IMPROVES",
}


# I topic sono categorie neutrali.
# Non rappresentano polarità positive o negative.
TOPIC_PATTERNS = {
    "Continuità del servizio":
        r"\b(?:interruzion\w*|disserviz\w*|"
        r"sospension\w*|ripristin\w*|"
        r"fornitur\w* idric\w*|mancanza d.acqua)\b",

    "Reti e manutenzione":
        r"\b(?:guast\w*|rottur\w*|condott\w*|"
        r"tubatur\w*|manutenzion\w*|"
        r"perdit\w* idric\w*|rete idric\w*)\b",

    "Ambiente e depurazione":
        r"\b(?:inquinament\w*|depurazion\w*|"
        r"scarich\w*|sostenibil\w*|"
        r"ecosistem\w*|contaminazion\w*)\b",

    "Investimenti e infrastrutture":
        r"\b(?:investiment\w*|finanziament\w*|"
        r"infrastruttur\w*|ammodernament\w*|"
        r"nuov\w* impiant\w*)\b",

    "Tariffe e fatturazione":
        r"\b(?:tariff\w*|bollett\w*|"
        r"fattur\w*|conguagl\w*)\b",

    "Regolazione e sanzioni":
        r"\b(?:sanzion\w*|violazion\w*|"
        r"arera|multa\w*|ordinanz\w*)\b",

    "Governance e appalti":
        r"\b(?:governance|trasparenz\w*|"
        r"appalt\w*|dirigent\w*|"
        r"consigl\w* di amministrazione|"
        r"corruzion\w*)\b",

    "Salute e sicurezza":
        r"\b(?:potabil\w*|sicurezz\w*|"
        r"salute\b|rischi\w* sanitari)\b",
}


TOPIC_REGEX = [
    (topic, re.compile(pattern, re.I))
    for topic, pattern in TOPIC_PATTERNS.items()
]


def normalize(value):
    return " ".join(
        re.sub(
            r"[^\w ]",
            " ",
            str(value).casefold(),
        ).split()
    )


def canonical_predicate(raw):
    key = re.sub(
        r"[\s-]+",
        "_",
        str(raw or "").strip().upper(),
    )

    return PREDICATE_MAP.get(key)


def detect_topics(quote):
    text = str(quote or "")

    for topic, pattern in TOPIC_REGEX:
        match = pattern.search(text)

        if match:
            yield topic, match.group(0)


def batches(items, size=300):
    for start in range(0, len(items), size):
        yield items[start:start + size]


PREDICATES_CYPHER = """
UNWIND $rows AS row

MATCH (c:Claim {id:row.id})
WHERE c.predicate = 'OTHER'

SET
    c.predicate = row.canonical,
    c.normalization_version = $version

WITH c, row

OPTIONAL MATCH ()-[r:FACT {claim_id:c.id}]->()

FOREACH (
    rel IN CASE WHEN r IS NULL THEN [] ELSE [r] END |
    SET
        rel.predicate = row.canonical,
        rel.normalization_version = $version
)

RETURN count(DISTINCT c) AS updated
"""


TOPICS_CYPHER = """
UNWIND $rows AS row

MATCH (e:EventMention {id:row.event_id})
MATCH (c:Claim {id:row.claim_id})

MERGE (t:GraphNode:GlobalEntity {id:row.topic_id})

ON CREATE SET
    t.name = row.topic,
    t.kind = 'Topic'

MERGE (e)-[r:HAS_TOPIC {
    claim_id:row.claim_id,
    version:$version
}]->(t)

ON CREATE SET
    r.article_id = row.article_id,
    r.method = 'quote_keyword',
    r.match_text = row.match_text

RETURN count(r) AS linked
"""


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--apply",
        action="store_true",
        help="Applica le modifiche al grafo",
    )

    args = parser.parse_args()

    predicate_changes = []
    topic_links = []

    original_other = Counter()
    topic_counter = Counter()

    seen = set()

    driver = GraphDatabase.driver(
        CFG.neo4j_uri,
        auth=(
            CFG.neo4j_user,
            CFG.neo4j_password,
        ),
    )

    try:
        with driver.session(
            database=CFG.neo4j_database
        ) as session:

            # 1. Recupero predicati OTHER
            for record in session.run(
                """
                MATCH (c:Claim {predicate:'OTHER'})

                RETURN
                    c.id AS id,
                    c.predicate_raw AS raw
                """
            ):
                raw = record["raw"] or ""

                original_other[raw] += 1

                mapped = canonical_predicate(raw)

                if mapped:
                    predicate_changes.append({
                        "id": record["id"],
                        "canonical": mapped,
                    })

            # 2. Individuazione Topic dalle citazioni
            for record in session.run(
                """
                MATCH (e:EventMention)-[r:FACT]-(:GraphNode)
                MATCH (c:Claim {id:r.claim_id})

                RETURN
                    e.id AS event_id,
                    c.id AS claim_id,
                    c.article_id AS article_id,
                    c.quote AS quote
                """
            ):
                for topic, match_text in detect_topics(
                    record["quote"]
                ):
                    key = (
                        record["event_id"],
                        record["claim_id"],
                        topic,
                    )

                    if key in seen:
                        continue

                    seen.add(key)

                    topic_id = (
                        "global-"
                        + short_hash(
                            "Topic:" + normalize(topic)
                        )
                    )

                    topic_links.append({
                        "event_id": record["event_id"],
                        "claim_id": record["claim_id"],
                        "article_id": record["article_id"],
                        "topic_id": topic_id,
                        "topic": topic,
                        "match_text": match_text,
                    })

                    topic_counter[topic] += 1

            print(
                "OTHER totali:",
                sum(original_other.values()),
            )

            print(
                "OTHER normalizzabili:",
                len(predicate_changes),
            )

            print(
                "Principali OTHER:",
                original_other.most_common(15),
            )

            print(
                "Collegamenti Event-Topic candidati:",
                len(topic_links),
            )

            print(
                "Distribuzione Topic:",
                dict(topic_counter),
            )

            if not args.apply:
                print(
                    "DRY RUN: nessuna modifica effettuata."
                )
                return

            # 3. Applicazione delle normalizzazioni
            for batch in batches(predicate_changes):
                session.run(
                    PREDICATES_CYPHER,
                    rows=batch,
                    version=NORMALIZER_VERSION,
                ).consume()

            # 4. Collegamenti Event-Topic
            for batch in batches(topic_links):
                session.run(
                    TOPICS_CYPHER,
                    rows=batch,
                    version=VERSION,
                ).consume()

            print("Arricchimento completato.")

    finally:
        driver.close()


if __name__ == "__main__":
    main()
