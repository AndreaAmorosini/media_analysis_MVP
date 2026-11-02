
from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from pathlib import Path

import numpy as np
from neo4j import GraphDatabase

from presskg.settings import CFG


MODEL = ("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")


def norm(value):
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))

    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def event_description(event):
    parts = [event.get("name") or "", event.get("event_type") or "", event.get("location") or ""]

    neighbors = sorted(
        event.get("neighbors") or [],
        key=lambda x: (str(x.get("predicate") or ""), str(x.get("name") or "")),
    )
    for neighbor in neighbors[:12]:
        predicate = neighbor.get("predicate") or ""
        name = neighbor.get("name") or ""
        evidence = (neighbor.get("evidence") or "")[:240]
        
        parts.append(f"{predicate} {name}. {evidence}")
        
    return ". ".join(part for part in parts if part.strip())


def write_batch(tx, rows):
    tx.run(
        """
        UNWIND $rows AS row

        MATCH (e:EventMention {
            id:row.event_id
        })

        MERGE (t:GraphNode:Topic {
            id:row.topic_node_id
        })

        ON CREATE SET
            t.name = row.topic_name,
            t.topic_id = row.topic_id,
            t.kind = 'Topic'

        MERGE (e)-[r:HAS_TOPIC]->(t)

        SET
            r.method = row.method,
            r.score = row.score,
            r.model = row.model
        """,
        rows=rows,
    ).consume()


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--topics", default="config/topics.json")
    parser.add_argument("--out", default="output/topics/topic_candidates.csv")
    parser.add_argument("--method", choices=("direct", "embedding"), default="embedding")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--min-score", type=float, default=0.70)
    parser.add_argument("--min-margin", type=float, default=0.12)
    parser.add_argument("--apply-direct", action="store_true")
    parser.add_argument("--apply-auto", action="store_true")
    parser.add_argument("--apply-approved", default=None)

    args = parser.parse_args()

    topics = json.loads(Path(args.topics).read_text(encoding="utf-8"))["topics"]
    topic_by_id = {topic["id"]: topic for topic in topics}

    if len(topic_by_id) != len(topics):
        raise ValueError("Topic ID duplicati")

    aliases = {}

    for topic in topics:
        names = [topic["name"], *topic.get("aliases", [])]

        for name in names:
            key = norm(name)
            if (key in aliases and aliases[key] != topic["id"]):
                raise ValueError(f"Alias ambiguo: {name}")

            aliases[key] = topic["id"]

    driver = GraphDatabase.driver(CFG.neo4j_uri, auth=(CFG.neo4j_user, CFG.neo4j_password))

    try:
        with driver.session(database=CFG.neo4j_database) as session:
            events = session.run(
                """
                MATCH (e:EventMention)

                OPTIONAL MATCH
                    (e)-[r:FACT]-(g:GlobalEntity)
                    
                OPTIONAL MATCH (c:Claim {
                    id:r.claim_id
                })

                WITH
                    e,
                    collect(
                        DISTINCT CASE
                            WHEN g IS NULL THEN null
                            ELSE {
                                name:g.name,
                                kind:g.kind,
                                predicate:r.predicate,
                                evidence:c.quote
                            }
                        END
                    ) AS neighbors

                RETURN
                    e.id AS id,
                    e.name AS name,
                    e.event_type AS event_type,
                    e.location AS location,
                    e.event_status AS event_status,
                    e.event_date_text AS event_date_text,
                    neighbors
                """
            ).data()

            candidates = []
            unresolved = []

            for event in events:
                direct = set()

                for neighbor in (event.get("neighbors") or []):
                    if not neighbor:
                        continue
                    if neighbor.get("kind") != "Topic":
                        continue
                    topic_id = aliases.get(norm(neighbor.get("name")))
                    if topic_id:
                        direct.add(topic_id)

                if direct:
                    for topic_id in sorted(direct):
                        candidates.append({
                            "event_id": event["id"],
                            "topic_id": topic_id,
                            "topic_name": topic_by_id[topic_id]["name"],
                            "topic_node_id": (f"taxonomy-topic:{topic_id}"),
                            "method": "direct_graph_topic",
                            "score": 1.0,
                            "margin": 1.0,
                            "model": "none",
                            "approved": "",
                        })
                else:
                    unresolved.append(event)

            if (args.method == "embedding" and unresolved):
                from sentence_transformers import (SentenceTransformer)

                model = SentenceTransformer(args.model, device="cpu")
                descriptions = [(topic["name"] + ". " + topic["description"]) for topic in topics]
                event_texts = [event_description(event) for event in unresolved]

                topic_vectors = model.encode(descriptions, normalize_embeddings=True, convert_to_numpy=True)
                event_vectors = model.encode(event_texts, batch_size=64, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=True)

                similarities = (np.asarray(event_vectors) @ np.asarray(topic_vectors).T)

                for event, scores in zip(unresolved, similarities):
                    order = np.argsort(scores)[::-1]

                    best = int(order[0])

                    second_score = (float(scores[order[1]]) if len(order) > 1 else -1.0)

                    score = float(scores[best])
                    margin = score - second_score

                    topic = topics[best]

                    candidates.append({
                        "event_id": event["id"],
                        "topic_id": topic["id"],
                        "topic_name": topic["name"],
                        "topic_node_id": (f"taxonomy-topic:{topic['id']}"),
                        "method": "graph_embedding",
                        "score": round(score, 5),
                        "margin": round(margin, 5),
                        "model": args.model,
                        "approved": "",
                    })

            events_by_id = {event["id"]: event for event in events}
            for candidate in candidates:
                event = events_by_id[candidate["event_id"]]
                candidate["event_name"] = event.get("name") or ""
                candidate["context_preview"] = event_description(event)[:800]
                
                if candidate["method"] == "direct_graph_topic":
                    candidate["decision"] = "direct_candidate"
                elif (candidate["score"] >= args.min_score and candidate["margin"] >= args.min_margin):
                    candidate["decision"] = "high_similarity_candidate"
                else:
                    candidate["decision"] = "abstain"

            fields = [
                "event_id",
                "topic_id",
                "topic_name",
                "topic_node_id",
                "method",
                "score",
                "margin",
                "model",
                "approved",
                "event_name",
                "context_preview",
                "decision",
            ]

            path = Path(args.out)

            # Non sovrascrivere un CSV già annotato.
            if (args.apply_approved and path.resolve() == Path(args.apply_approved).resolve()):
                path = path.with_name("topic_candidates_latest.csv")

            path.parent.mkdir(parents=True, exist_ok=True)

            with path.open("w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=fields)
                writer.writeheader()
                writer.writerows(candidates)

            print("Eventi:", len(events))
            print("Candidati:", len(candidates))
            print("Report:", path)

            selected = []

            if args.apply_direct:
                selected.extend(row for row in candidates if row["method"] == "direct_graph_topic")

            if args.apply_auto:
                selected.extend(
                    row for row in candidates
                    if (row["method"] == "graph_embedding" and row["score"] >= args.min_score and row["margin"] >= args.min_margin)
                )

            if args.apply_approved:
                with open(args.apply_approved, newline="", encoding="utf-8") as file:
                    annotated = list(csv.DictReader(file))

                valid_pairs = {(row["event_id"], row["topic_id"]): row for row in candidates}

                for row in annotated:
                    if row.get("approved", "").strip().lower() not in {"yes", "si", "true", "1"}:
                        continue

                    pair = (row["event_id"], row["topic_id"])
                    if pair not in valid_pairs:
                        raise ValueError(f"Candidato non valido: {pair}")

                    selected.append(valid_pairs[pair])

            selected = list({(row["event_id"], row["topic_id"]): row for row in selected}.values())

            if not selected:
                print("Nessun HAS_TOPIC scritto. Modalità di anteprima.")
                return

            for start in range(0, len(selected), 200):
                session.execute_write(write_batch, selected[start:start + 200])

            print("HAS_TOPIC creati/aggiornati:", len(selected))

    finally:
        driver.close()


if __name__ == "__main__":
    main()
