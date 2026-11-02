"""Persistenza transazionale del Knowledge Graph."""

from __future__ import annotations

import re

from neo4j import GraphDatabase

from .articles import Article, short_hash
from .settings import CFG


CONSTRAINTS = (
    """
    CREATE CONSTRAINT article_id IF NOT EXISTS
    FOR (a:Article)
    REQUIRE a.id IS UNIQUE
    """,
    """
    CREATE CONSTRAINT chunk_id IF NOT EXISTS
    FOR (c:Chunk)
    REQUIRE c.id IS UNIQUE
    """,
    """
    CREATE CONSTRAINT claim_id IF NOT EXISTS
    FOR (c:Claim)
    REQUIRE c.id IS UNIQUE
    """,
    """
    CREATE CONSTRAINT graphnode_id IF NOT EXISTS
    FOR (n:GraphNode)
    REQUIRE n.id IS UNIQUE
    """,
    """
    CREATE CONSTRAINT source_id IF NOT EXISTS
    FOR (s:Source)
    REQUIRE s.id IS UNIQUE
    """,
    """
    CREATE CONSTRAINT entity_mention_id IF NOT EXISTS
    FOR (m:EntityMention)
    REQUIRE m.id IS UNIQUE
    """,
)


def normalize(value: str) -> str:
    return " ".join(re.sub(r"[^\w ]", " ", value.casefold()).split())


class GraphStore:
    def __init__(self):
        if not CFG.neo4j_password:
            raise ValueError("NEO4J_PASSWORD non configurata")

        self.driver = GraphDatabase.driver(
            CFG.neo4j_uri,
            auth=(CFG.neo4j_user, CFG.neo4j_password),
        )

        self.db = CFG.neo4j_database

    def close(self):
        self.driver.close()

    def verify(self):
        self.driver.verify_connectivity()

    def init(self):
        for cypher in CONSTRAINTS:
            self.driver.execute_query(cypher, database_=self.db)

    @staticmethod
    def _identity_static(entity: dict) -> dict:
        name = entity["name"].strip()
        kind = entity["type"]

        if not name:
            raise ValueError("Entità senza nome valido")

        if kind == "Event":
            event_id = entity.get("id")
            if not event_id:
                raise ValueError(
                    f"EventMention senza ID: {name}"
                )

            return {
                **entity,
                "name": name,
                "kind": "Event",
                "is_event": True,
            }

        entity_id = (
            "global-"
            + short_hash(
                f"{kind}:{normalize(name)}"
            )
        )

        return {
            **entity,
            "id": entity_id,  # FIX ESSENZIALE
            "name": name,
            "kind": kind,
            "is_event": False,
        }
        
    def status(self, article_id: str):
        records, _, _ = self.driver.execute_query(
            """
            MATCH (a:Article {id:$aid})
            RETURN
                a.content_hash AS hash,
                a.extraction_key AS key
            """,
            aid=article_id,
            database_=self.db,
        )

        if not records:
            return None

        return (records[0]["hash"], records[0]["key"])

    def _write(self, tx, article: Article, payload: dict, extraction_key: str, replace: bool):
        record = tx.run(
            """
            MATCH (a:Article {id:$aid})
            RETURN
                a.content_hash AS hash,
                a.extraction_key AS key
            """,
            aid=article.id,
        ).single()

        if record is not None:
            if (record["hash"] == article.content_hash and record["key"] == extraction_key):
                return "skipped"

            if not replace:
                raise ValueError(f"Articolo già presente ma cambiato: {article.rel_path}; usare --replace")

            # Sostituzione atomica della parte
            # documentale e delle relazioni FACT.
            tx.run(
                """
                MATCH ()-[r:FACT {article_id:$aid}]->()
                DELETE r
                """,
                aid=article.id,
            ).consume()

            tx.run(
                """
                MATCH (c:Claim {article_id:$aid})
                DETACH DELETE c
                """,
                aid=article.id,
            ).consume()

            tx.run(
                """
                MATCH (c:Chunk {article_id:$aid})
                DETACH DELETE c
                """,
                aid=article.id,
            ).consume()

            tx.run(
                """
                MATCH (n:EventMention {article_id:$aid})
                DETACH DELETE n
                """,
                aid=article.id,
            ).consume()

            tx.run(
                """
                MATCH (a:Article {id:$aid})
                DETACH DELETE a
                """,
                aid=article.id,
            ).consume()


        source_name = ((article.source_name or "").strip())
        source_type = ((article.source_type or "").strip())

        source_known = (bool(source_name) and source_name.casefold() not in {
                "unknown",
                "sconosciuto",
                "n/a",
                "none",
                "null",
                "non disponibile",
                "undefined"
            }
        )

        # L'articolo esiste anche quando
        # la testata non è conosciuta.
        tx.run(
            """
            CREATE (a:Article {
                id:$id,
                rel_path:$path,
                title:$title,
                subtitle:$subtitle,
                publication_date:$date,
                body:$body,
                content_hash:$hash,
                extraction_key:$key,
                source_name:$source_name,
                source_status:$source_status
            })
            """,
            id=article.id,
            path=article.rel_path,
            title=article.title,
            subtitle=article.subtitle,
            date=article.publication_date,
            body=article.body,
            hash=article.content_hash,
            key=extraction_key,
            source_name=(source_name if source_known else None),
            source_status=("explicit" if source_known else "missing"),
        ).consume()

        if source_known:
            source_id = ("source-" + short_hash(normalize(source_name)))

            tx.run(
                """
                MERGE (s:Source {id:$sid})

                ON CREATE SET
                    s.name=$name,
                    s.source_type=$stype

                WITH s

                MATCH (a:Article {id:$aid})

                MERGE (a)-[:PUBLISHED_BY]->(s)
                """,
                sid=source_id,
                name=source_name,
                stype=source_type or None,
                aid=article.id,
            ).consume()


        for chunk in payload["chunks"]:
            chunk_id = (f"{article.id}:chunk:{chunk['index']}")

            tx.run(
                """
                MATCH (a:Article {id:$aid})

                CREATE (c:Chunk {
                    id:$cid,
                    article_id:$aid,
                    chunk_index:$idx,
                    body_start:$start,
                    body_end:$end,
                    text:$text
                })

                CREATE (a)-[:HAS_CHUNK]->(c)
                """,
                aid=article.id,
                cid=chunk_id,
                idx=chunk["index"],
                start=chunk["start"],
                end=chunk["end"],
                text=chunk["text"],
            ).consume()

            local = {}

            for raw in chunk["entities"]:
                entity = self._identity_static(raw)

                local[raw["local_id"]] = entity

                if entity["is_event"]:
                    tx.run(
                        """
                        MERGE (
                            n:GraphNode:EventMention {
                                id:$id
                            }
                        )

                        SET
                            n.name=$name,
                            n.kind='Event',
                            n.type_group='semantic',
                            n.subtype=$subtype,
                            n.article_id=$aid,
                            n.event_type=$etype,
                            n.event_date=$date,
                            n.location=$location

                        WITH n
                        MATCH (a:Article {id:$aid})

                        MERGE (a)-[:HAS_EVENT]->(n)
                        """,
                        id=entity["id"],
                        name=entity["name"],
                        aid=article.id,
                        etype=entity["event_type"],
                        date=entity["event_date"],
                        location=entity["location"],
                        subtype=entity.get("subtype"),
                    ).consume()

                else:
                    tx.run(
                        """
                        MERGE (
                            n:GraphNode:GlobalEntity {
                                id:$id
                            }
                        )

                        ON CREATE SET
                            n.name=$name,
                            n.kind=$kind,
                            n.type_group=$type_group,
                            n.subtype=$subtype

                        ON MATCH SET
                            n.type_group=coalesce(
                                n.type_group,
                                $type_group
                            ),
                            n.subtype=coalesce(
                                n.subtype,
                                $subtype
                            )
                        """,
                        id=entity["id"],
                        name=entity["name"],
                        kind=entity["kind"],
                        type_group=(
                            entity.get("type_group")
                            or "unmapped"
                        ),
                        subtype=entity.get("subtype"),
                    ).consume()


            for claim in chunk["claims"]:
                subject = local[claim["subject_local"]]
                obj = local[claim["object_local"]]

                # Verifica dell'evidenza principale.
                start = claim["body_start"]
                end = claim["body_end"]

                if (
                    not isinstance(start, int)
                    or not isinstance(end, int)
                    or start < 0
                    or end <= start
                    or end > len(article.body)
                    or article.body[start:end] != claim["quote"]
                ):
                    raise ValueError(f"Evidenza principale non valida: {claim['id']}")

                # Verifica dell'evidenza contestuale,
                # quando fornita dall'estrattore.
                context_start = claim.get("context_body_start")
                context_end = claim.get("context_body_end")
                context_quote = claim.get("context_quote")

                if any(value is not None for value in (context_start, context_end, context_quote)):
                    if (
                        not isinstance(context_start, int)
                        or not isinstance(context_end, int)
                        or not isinstance(context_quote, str)
                        or context_start < 0
                        or context_end <= context_start
                        or context_end > len(article.body)
                        or article.body[context_start:context_end] != context_quote
                    ):
                        raise ValueError(f"Evidenza contestuale non valida: {claim['id']}")

                tx.run(
                    """
                    MATCH (ch:Chunk {id:$cid})
                    MATCH (s:GraphNode {id:$sid})
                    MATCH (o:GraphNode {id:$oid})

                    CREATE (c:Claim {
                        id:$id,
                        article_id:$aid,
                        chunk_id:$cid,

                        predicate:$pred,
                        predicate_raw:$pred_raw,
                        predicate_family:$family,

                        modality:$mode,
                        attribution:$attr,
                        attribution_role:$attr_role,
                        subject_role:$role,

                        quote:$quote,
                        body_start:$start,
                        body_end:$end,

                        context_evidence_id:$context_id,
                        context_quote:$context_quote,
                        context_body_start:$context_start,
                        context_body_end:$context_end,

                        needs_attribution_review:$attr_review,
                        needs_semantic_review:$semantic_review,

                        subject_type_raw:$subject_type_raw,
                        object_type_raw:$object_type_raw,
                        subject_subtype:$subject_subtype,
                        object_subtype:$object_subtype
                    })

                    CREATE (ch)-[:HAS_CLAIM]->(c)
                    CREATE (c)-[:SUBJECT]->(s)
                    CREATE (c)-[:OBJECT]->(o)
                    
                    CREATE (sm:EntityMention {
                        id:$subject_mention_id,
                        article_id:$aid,
                        claim_id:$id,
                        role:'subject',
                        name:$subject_name,
                        kind:$subject_kind
                    })

                    CREATE (om:EntityMention {
                        id:$object_mention_id,
                        article_id:$aid,
                        claim_id:$id,
                        role:'object',
                        name:$object_name,
                        kind:$object_kind
                    })

                    CREATE (c)-[:SUBJECT_MENTION]->(sm)
                    CREATE (c)-[:OBJECT_MENTION]->(om)

                    CREATE (sm)-[:CANDIDATE_GLOBAL]->(s)
                    CREATE (om)-[:CANDIDATE_GLOBAL]->(o)

                    CREATE (s)-[:FACT {
                        claim_id:$id,
                        article_id:$aid,

                        predicate:$pred,
                        predicate_raw:$pred_raw,
                        predicate_family:$family,

                        modality:$mode,
                        attribution:$attr,
                        attribution_role:$attr_role,
                        subject_role:$role,

                        needs_attribution_review:$attr_review,
                        needs_semantic_review:$semantic_review
                    }]->(o)
                    """,

                    cid=chunk_id,
                    sid=subject["id"],
                    oid=obj["id"],

                    id=claim["id"],
                    aid=article.id,

                    pred=claim["predicate"],
                    pred_raw=claim.get("predicate_raw", claim["predicate"]),
                    family=claim.get("predicate_family", "unmapped"),

                    mode=claim["modality"],
                    attr=claim.get("attribution"),
                    attr_role=claim.get("attribution_role"),
                    role=claim.get("subject_role"),

                    quote=claim["quote"],
                    start=start,
                    end=end,

                    context_id=claim.get("context_evidence_id"),
                    context_quote=context_quote,
                    context_start=context_start,
                    context_end=context_end,

                    attr_review=bool(claim.get("needs_attribution_review", False)),
                    semantic_review=bool(claim.get("needs_semantic_review", False)),

                    subject_type_raw=subject.get("type_raw"),
                    object_type_raw=obj.get("type_raw"),
                    subject_subtype=subject.get("subtype"),
                    object_subtype=obj.get("subtype"),
                    
                    subject_mention_id=f"{claim['id']}:subject",
                    object_mention_id=f"{claim['id']}:object",

                    subject_name=subject["name"],
                    object_name=obj["name"],

                    subject_kind=subject["kind"],
                    object_kind=obj["kind"],
                ).consume()

        if replace:
            tx.run(
                """
                MATCH (m:EntityMention {article_id:$aid})
                DETACH DELETE m
                """,
                aid=article.id,
            ).consume()
            
            # Elimina entità globali rimaste isolate
            # dopo la sostituzione.
            tx.run(
                """
                MATCH (e:GlobalEntity)
                WHERE NOT EXISTS {
                    MATCH (e)--()
                }
                DELETE e
                """
            ).consume()

        return "written"

    def write(self, article: Article, payload: dict, extraction_key: str, replace: bool = False):
        with self.driver.session(database=self.db) as session:
            return session.execute_write(self._write, article, payload, extraction_key, replace)

    def stats(self):
        records, _, _ = self.driver.execute_query(
            """
            MATCH (n)

            WITH labels(n) AS labels
            UNWIND labels AS kind

            RETURN
                kind,
                count(*) AS total

            ORDER BY kind
            """,
            database_=self.db,
        )

        return {record["kind"]: record["total"] for record in records}