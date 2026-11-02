
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import unicodedata
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from datetime import date

from neo4j import GraphDatabase

from presskg.settings import CFG


def norm(value):
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))

    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def informative(name):
    tokens = norm(name).split()

    # Filtro strutturale generico.
    # Nessuna blacklist di eventi specifici.
    return (len(tokens) >= 2 and len("".join(tokens)) >= 8)

def parse_date(value):
    if value is None:
        return None
    
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None

def candidate_pairs(events, participants):
    buckets = defaultdict(list)

    for event in events:
        if event.get("event_status") in {
            "generic", "hypothetical", "uncertain"
        }:
            continue
        
        name = norm(event["name"])

        if not informative(name):
            continue

        event_type = norm(event["event_type"])
        event_date = str(event["event_date"] or "").strip()
        location = norm(event["location"])

        if event["article_id"]:
            buckets[("same_article", event["article_id"], name, event_type)].append(event)

        if event_date and location:
            buckets[("cross_article", event_date, location, name, event_type)].append(event)
            
        if(parse_date(event.get("publication_date")) is not None):
            buckets[("publication_window", name, event_type)].append(event)

    results = {}

    for signature, group in buckets.items():
        if len(group) > 60:
            continue

        for a, b in combinations(group, 2):
            if signature[0] == "publication_window":
                if a["article_id"] == b["article_id"]:
                    continue
                
                date_a = parse_date(a.get("publication_date"))
                date_b = parse_date(b.get("publication_date"))
                
                if date_a is None or date_b is None:
                    continue
                
                if abs((date_a - date_b).days) > 7:
                    continue
                
                if(a.get("event_date") and b.get("event_date") and norm(a["event_date"]) != norm(b["event_date"])):
                    continue
                
            date_a = a.get("event_date")
            date_b = b.get("event_date")
            
            if date_a and date_b and str(date_a) != str(date_b):
                continue
            
            end_a = a.get("event_end_date")
            end_b = b.get("event_end_date")
            
            if end_a and end_b and str(end_a) != str(end_b):
                continue
            
            location_a = norm(a.get("location"))
            location_b = norm(b.get("location"))
            
            if location_a and location_b and location_a != location_b:
                continue
            
            shared = (participants[a["id"]] & participants[b["id"]])

            if not shared:
                continue
            
            if (signature[0] == "publication_window" and len(shared) < 2):
                continue

            pair = tuple(sorted((a["id"], b["id"])))
            method = signature[0]

            candidate = {
                "event_id_a": pair[0],
                "event_id_b": pair[1],
                "name": a["name"],
                "event_date": (a["event_date"] or b["event_date"] or ""),
                "location": (a["location"] or b["location"] or ""),
                "event_type": (a["event_type"] or b["event_type"] or ""),
                "method": method,
                "shared_participants": " | ".join(sorted(shared)),
                "approved": "",
                "article_id_a": a["article_id"],
                "article_id_b": b["article_id"],
                "article_path_a": a.get("article_path") or "",
                "article_path_b": b.get("article_path") or "",
                "publication_date_a": a.get("publication_date") or "",
                "publication_date_b": b.get("publication_date") or "",
                "shared_participant_count": len(shared),
            }

            if (pair not in results or method == "same_article"):
                results[pair] = candidate

    return results


def write_cluster(tx, rows):
    tx.run(
        """
        UNWIND $rows AS row

        MATCH (e:EventMention {id:row.event_id})

        MERGE (c:GraphNode:CanonicalEvent {
            id:row.canonical_id
        })

        ON CREATE SET
            c.name = row.name,
            c.event_date = row.event_date,
            c.location = row.location,
            c.event_type = row.event_type

        MERGE (e)-[r:REFERS_TO]->(c)

        SET r.method = 'reviewed_event_cluster'
        """,
        rows=rows,
    ).consume()


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--out", default="output/resolution/event_candidates.csv",)
    parser.add_argument("--apply-approved", default=None)

    args = parser.parse_args()

    driver = GraphDatabase.driver(CFG.neo4j_uri, auth=(CFG.neo4j_user, CFG.neo4j_password))

    try:
        with driver.session(database=CFG.neo4j_database) as session:
            events = session.run(
                """
                MATCH (e:EventMention)

                OPTIONAL MATCH (a:Article {
                    id:e.article_id
                })

                RETURN
                    e.id AS id,
                    e.name AS name,
                    e.article_id AS article_id,
                    e.event_type AS event_type,
                    e.event_date AS event_date,
                    e.event_end_date AS event_end_date,
                    e.event_status AS event_status,
                    e.event_data_text as event_data_text,
                    e.location AS location,
                    coalesce(a.article_publication_date, a.publication_date) AS publication_date,
                    a.rel_path as article_path
                """
            ).data()

            participants = defaultdict(set)

            for row in session.run(
                """
                MATCH
                    (e:EventMention)-[:FACT]-
                    (g:GlobalEntity)

                WHERE g.kind IN [
                    'Person',
                    'Company',
                    'Organization',
                    'PublicAuthority'
                ]

                RETURN
                    e.id AS event_id,
                    coalesce(
                        g.canonical_id,
                        g.id
                    ) AS participant_id
                """
            ):
                participants[row["event_id"]].add(row["participant_id"])

            candidates = candidate_pairs(events, participants)
            path = Path(args.out)

            if (args.apply_approved and path.resolve() == Path(args.apply_approved).resolve()):
                path = path.with_name("event_candidates_latest.csv")

            path.parent.mkdir(parents=True, exist_ok=True)

            fields = [
                "event_id_a",
                "event_id_b",
                "name",
                "event_date",
                "location",
                "event_type",
                "method",
                "shared_participants",
                "approved",
            ]

            with path.open("w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=fields)
                writer.writeheader()
                writer.writerows(candidates.values())

            print("EventMention:", len(events))
            print("Coppie candidate:", len(candidates))

            if not args.apply_approved:
                print("DRY RUN:", path)
                return

            with open(args.apply_approved, newline="", encoding="utf-8") as file:
                approved_rows = list(csv.DictReader(file))

            approved_pairs = set()

            for row in approved_rows:
                if row.get("approved", "").strip().lower() not in {"yes", "si", "true", "1"}:
                    continue

                pair = tuple(sorted((row["event_id_a"], row["event_id_b"])))
                if pair not in candidates:
                    raise ValueError(f"Coppia non valida: {pair}")

                approved_pairs.add(pair)

            # Costruzione dei cluster di menzioni.
            adjacency = defaultdict(set)

            for a, b in approved_pairs:
                adjacency[a].add(b)
                adjacency[b].add(a)

            visited = set()
            clusters = []

            for event_id in adjacency:
                if event_id in visited:
                    continue

                stack = [event_id]
                cluster = set()

                while stack:
                    current = stack.pop()
                    if current in cluster:
                        continue

                    cluster.add(current)
                    stack.extend(adjacency[current])

                visited.update(cluster)
                clusters.append(sorted(cluster))

            event_by_id = {e["id"]: e for e in events}

            writes = []

            for cluster in clusters:
                # Evita fusioni per transitività:
                # A~B e B~C non implica A~C.
                for pair in combinations(cluster, 2):
                    if tuple(sorted(pair)) not in approved_pairs:
                        raise ValueError("Cluster non completamente verificato: " + str(cluster))

                members = [event_by_id[eid] for eid in cluster]

                for field in ("event_date", "location", "event_type"):
                    values = {norm(e[field]) for e in members if e[field]}

                    if len(values) > 1:
                        raise ValueError(f"Metadati evento incompatibili: {field}, {cluster}")

                digest = hashlib.sha256("|".join(cluster).encode()).hexdigest()[:24]

                cid = f"canonical-event:{digest}"
                representative = members[0]

                for e in members:
                    writes.append({
                        "event_id": e["id"],
                        "canonical_id": cid,
                        "name": representative["name"],
                        "event_date": (representative["event_date"]),
                        "location": (representative["location"]),
                        "event_type": (representative["event_type"]),
                    })

            # Non assegnare menzioni già risolte
            # a un evento canonico differente.
            current = {
                r["event_id"]: r["canonical_id"]
                for r in session.run(
                    """
                    MATCH
                        (e:EventMention)-[:REFERS_TO]->
                        (c:CanonicalEvent)
                    RETURN
                        e.id AS event_id,
                        c.id AS canonical_id
                    """
                )
            }

            for row in writes:
                existing = current.get(row["event_id"])
                if existing and existing != row["canonical_id"]:
                    raise ValueError("Risoluzione evento precedente in conflitto: " + row["event_id"])

            for start in range(0, len(writes), 200):
                session.execute_write(write_cluster, writes[start:start + 200])

            print("Menzioni collegate:", len(writes))

    finally:
        driver.close()


if __name__ == "__main__":
    main()
