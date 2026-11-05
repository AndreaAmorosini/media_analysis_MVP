
#!/usr/bin/env python3
"""Materializza una vista versionata dei claim accettati e corretti.

Non modifica Claim, ClaimValidation, VerifiedClaimCorrection
né le relazioni FACT. Non usa LLM.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone

from tqdm import tqdm
from presskg.settings import CFG
from .primary import claim_hash, open_driver


VERSION = "validated-fact-view-v1"
BATCH_ID_DEFAULT = "qwen38-gemma31-gemma12-v1"
STATES = {"accepted", "corrected_verified"}


SOURCE_QUERY = """
MATCH (v:ClaimValidation {batch_id:$batch_id})
WHERE v.status IN ['accepted', 'corrected_verified']

MATCH (c:Claim {id:v.claim_id})
MATCH (a:Article {id:c.article_id})-[:HAS_CHUNK]->
      (ch:Chunk {id:c.chunk_id})-[:HAS_CLAIM]->(c)

MATCH (c)-[:SUBJECT]->(s:GraphNode)
MATCH (c)-[:OBJECT]->(o:GraphNode)
MATCH (c)-[:HAS_VALIDATION]->(v)

OPTIONAL MATCH
(c)-[:HAS_VERIFIED_CORRECTION]->
(xc:VerifiedClaimCorrection {batch_id:$batch_id})

OPTIONAL MATCH
(v)-[:VERIFIED_CORRECTION]->
(xv:VerifiedClaimCorrection)

RETURN
    v {
        .id, .claim_id, .claim_hash, .batch_id,
        .status, .correction_id, .primary_run_hash,
        .temporal_status, .semantic_family,
        .candidate_for_propagation
    } AS validation,

    c {
        .id, .article_id, .chunk_id, .predicate,
        .predicate_raw, .modality, .attribution,
        .quote, .body_start, .body_end
    } AS claim,

    s { .id, .name, .kind } AS subject,
    o { .id, .name, .kind } AS object,

    a {
        .id, .content_hash, .publication_date
    } AS article,

    ch.id AS linked_chunk_id,

    substring(
        a.body,
        c.body_start,
        c.body_end - c.body_start
    ) AS proof,

    substring(
        a.body,
        CASE
            WHEN c.body_start > $padding
            THEN c.body_start - $padding
            ELSE 0
        END,
        (c.body_end - c.body_start) + 2*$padding
    ) AS context,

    collect(DISTINCT xc {
        .id, .batch_id, .claim_id, .claim_hash,
        .primary_run_hash, .source_candidate_id,
        .candidate_hash, .subject_name, .subject_type,
        .predicate, .object_name, .object_type,
        .modality, .attribution, .temporal_status,
        .semantic_family, .supporting_span
    }) AS claim_corrections,

    collect(DISTINCT xv.id)
        AS validation_correction_ids

ORDER BY validation.claim_id
"""


CONSTRAINTS = (
    "CREATE CONSTRAINT vf_batch_id IF NOT EXISTS "
    "FOR (b:ValidatedFactBatch) REQUIRE b.id IS UNIQUE",

    "CREATE CONSTRAINT vf_fact_id IF NOT EXISTS "
    "FOR (f:ValidatedFact) REQUIRE f.id IS UNIQUE",

    "CREATE CONSTRAINT vf_mention_id IF NOT EXISTS "
    "FOR (m:ValidatedMention) REQUIRE m.id IS UNIQUE",
)


def digest(value):
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def literal(text):
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFC", text or ""),
    ).strip()


def required(value, label):
    if (
        not isinstance(value, str)
        or not value.strip()
    ):
        raise ValueError(
            f"Campo obbligatorio assente: {label}"
        )
    return value


def make_row(source, batch_id):
    v = source["validation"]
    c = source["claim"]
    s = source["subject"]
    o = source["object"]
    a = source["article"]

    cid = required(c["id"], "Claim.id")
    state = v["status"]

    if (
        state not in STATES
        or v["claim_id"] != cid
        or v["batch_id"] != batch_id
    ):
        raise ValueError(
            f"Validazione incoerente: {cid}"
        )

    if (
        c["article_id"] != a["id"]
        or c["chunk_id"] != source["linked_chunk_id"]
    ):
        raise ValueError(
            f"Articolo/chunk incoerenti: {cid}"
        )

    if (
        not isinstance(c["body_start"], int)
        or not isinstance(c["body_end"], int)
    ):
        raise ValueError(
            f"Offset originali assenti: {cid}"
        )

    if (
        c["body_start"] < 0
        or c["body_end"] <= c["body_start"]
    ):
        raise ValueError(
            f"Offset originali invalidi: {cid}"
        )

    if (
        source["proof"] != c["quote"]
        or not c["quote"]
    ):
        raise ValueError(
            f"Evidence grezza non verificabile: {cid}"
        )

    # Ricostruzione esatta della firma usata
    # dalla Semantic Claim Validation.
    raw = {
        "claim_id": cid,
        "article_id": c["article_id"],
        "subject_id": s["id"],
        "subject": s["name"],
        "subject_type": s["kind"],
        "predicate": c["predicate"],
        "predicate_raw": c["predicate_raw"],
        "object_id": o["id"],
        "object": o["name"],
        "object_type": o["kind"],
        "modality": c["modality"],
        "attribution": c["attribution"],
        "quote": c["quote"],
        "body_start": c["body_start"],
        "body_end": c["body_end"],
        "content_hash": a["content_hash"],
    }

    if claim_hash(raw) != v["claim_hash"]:
        raise ValueError(
            f"Claim hash non valido: {cid}"
        )

    corrections = source["claim_corrections"]
    linked_ids = source[
        "validation_correction_ids"
    ]

    if state == "accepted":
        if (
            corrections
            or linked_ids
            or v.get("correction_id")
        ):
            raise ValueError(
                "Claim accepted collegato "
                f"a correzione: {cid}"
            )

        subject, obj = s, o
        predicate = c["predicate"]
        modality = c["modality"]
        attribution = c["attribution"]

        temporal = v.get("temporal_status")
        family = v.get("semantic_family")

        supporting_span = c["quote"]
        correction_id = None

        candidate = bool(
            v.get(
                "candidate_for_propagation",
                False,
            )
        )

        representation = "original"

    else:
        if (
            len(corrections) != 1
            or len(linked_ids) != 1
        ):
            raise ValueError(
                "Collegamenti di correzione "
                f"non univoci: {cid}"
            )

        x = corrections[0]

        if (
            x["id"] != linked_ids[0]
            or x["id"] != v.get("correction_id")
            or x["batch_id"] != batch_id
            or x["claim_id"] != cid
            or x["claim_hash"] != v["claim_hash"]
            or x["primary_run_hash"]
            != v["primary_run_hash"]
            or not x.get("source_candidate_id")
            or not x.get("candidate_hash")
        ):
            raise ValueError(
                "Provenienza correzione "
                f"incoerente: {cid}"
            )

        supporting_span = required(
            x["supporting_span"],
            "supporting_span",
        )

        if literal(supporting_span) not in literal(
            source["context"]
        ):
            raise ValueError(
                "Supporting span non presente "
                f"nel contesto: {cid}"
            )

        subject = {
            "name": x["subject_name"],
            "kind": x["subject_type"],
        }

        obj = {
            "name": x["object_name"],
            "kind": x["object_type"],
        }

        predicate = x["predicate"]
        modality = x["modality"]
        attribution = x["attribution"]

        temporal = x["temporal_status"]
        family = x["semantic_family"]

        correction_id = x["id"]
        candidate = False
        representation = "verified_correction"

    for label, value in (
        ("subject", subject),
        ("object", obj),
    ):
        required(
            value["name"],
            f"{label}.name",
        )
        required(
            value["kind"],
            f"{label}.kind",
        )

    required(predicate, "predicate")
    required(modality, "modality")

    # Un ID deterministico per batch e claim.
    fact_id = (
        "vf-"
        + digest([VERSION, batch_id, cid])[:32]
    )

    fact = {
        "id": fact_id,
        "batch_id": batch_id,
        "version": VERSION,
        "claim_id": cid,
        "validation_id": v["id"],
        "claim_hash": v["claim_hash"],
        "article_id": a["id"],
        "chunk_id": c["chunk_id"],
        "representation": representation,
        "predicate": predicate,
        "subject_name": subject["name"],
        "subject_kind": subject["kind"],
        "object_name": obj["name"],
        "object_kind": obj["kind"],
        "modality": modality,
        "attribution": attribution,
        "temporal_status": temporal,
        "semantic_family": family,
        "original_quote": c["quote"],
        "supporting_span": supporting_span,
        "publication_date": a.get(
            "publication_date"
        ),
        "candidate_for_propagation": candidate,
        "eligible_for_propagation": False,
        "correction_id": correction_id,
    }

    # Neo4j non salva proprietà con valore null.
    fact = {
        key: value
        for key, value in fact.items()
        if value is not None
    }

    mentions = []

    for role, argument in (
        ("subject", subject),
        ("object", obj),
    ):
        mentions.append({
            "id": f"{fact_id}:{role}",
            "fact_id": fact_id,
            "batch_id": batch_id,
            "article_id": a["id"],
            "claim_id": cid,
            "role": role,
            "name": argument["name"],
            "kind": argument["kind"],
            "representation": representation,
        })

    return {
        "id": fact_id,
        "claim_id": cid,
        "validation_id": v["id"],
        "article_id": a["id"],
        "batch_id": batch_id,
        "correction_id": correction_id,
        "fact": fact,
        "subject": mentions[0],
        "object": mentions[1],
    }


def assert_compatible(stored, expected, node_id):
    if stored is None:
        return

    for key, value in expected.items():
        if stored.get(key) != value:
            raise ValueError(
                "Conflitto con nodo persistito "
                f"{node_id}: {key}"
            )


def write_batch(tx, rows, batch_key):
    # Controllo delle eventuali materializzazioni
    # già presenti, necessario per il resume.
    prior = tx.run("""
        UNWIND $rows AS row

        OPTIONAL MATCH (
            f:ValidatedFact {id:row.id}
        )

        OPTIONAL MATCH (
            s:ValidatedMention {id:row.subject.id}
        )

        OPTIONAL MATCH (
            o:ValidatedMention {id:row.object.id}
        )

        RETURN
            row.id AS id,
            properties(f) AS fact,
            properties(s) AS subject,
            properties(o) AS object
    """, rows=rows).data()

    if len(prior) != len(rows):
        raise ValueError(
            "Duplicazioni nel preflight della vista"
        )

    by_id = {
        item["id"]: item
        for item in prior
    }

    if len(by_id) != len(rows):
        raise ValueError(
            "ID ValidatedFact ripetuti nello stesso batch"
        )

    for row in rows:
        stored = by_id[row["id"]]

        for key in (
            "fact",
            "subject",
            "object",
        ):
            assert_compatible(
                stored[key],
                row[key],
                row["id"],
            )

    # Creazione di soli nodi e archi derivati.
    outcome = tx.run("""
        UNWIND $rows AS row

        MATCH (
            b:ValidatedFactBatch {id:$batch_key}
        )

        WHERE b.status IN ['building', 'ready']

        MATCH
        (c:Claim {id:row.claim_id})
        -[:HAS_VALIDATION]->
        (v:ClaimValidation {id:row.validation_id})

        WHERE
            v.batch_id = row.batch_id
            AND v.claim_hash = row.fact.claim_hash
            AND (
                (
                    row.fact.representation = 'original'
                    AND v.status = 'accepted'
                )
                OR (
                    row.fact.representation =
                        'verified_correction'
                    AND v.status = 'corrected_verified'
                )
            )

        MATCH (
            a:Article {id:row.article_id}
        )

        OPTIONAL MATCH (
            x:VerifiedClaimCorrection {
                id:row.correction_id
            }
        )

        WITH row, b, c, v, a, x

        WHERE
            row.correction_id IS NULL
            OR (
                x IS NOT NULL
                AND x.claim_id = row.claim_id
                AND x.batch_id = row.batch_id
            )

        MERGE (
            f:ValidatedFact {id:row.id}
        )

        ON CREATE SET f += row.fact

        MERGE (
            sm:ValidatedMention {id:row.subject.id}
        )

        ON CREATE SET sm += row.subject

        MERGE (
            om:ValidatedMention {id:row.object.id}
        )

        ON CREATE SET om += row.object

        MERGE (b)-[:HAS_FACT]->(f)
        MERGE (c)-[:HAS_VALIDATED_FACT]->(f)
        MERGE (v)-[:VALIDATED_AS]->(f)
        MERGE (a)-[:HAS_VALIDATED_FACT]->(f)

        MERGE (f)-[:SUBJECT_MENTION]->(sm)
        MERGE (f)-[:OBJECT_MENTION]->(om)

        FOREACH (
            _ IN CASE
                WHEN x IS NULL THEN []
                ELSE [1]
            END |
            MERGE (x)-[:MATERIALIZED_AS]->(f)
        )

        RETURN count(f) AS written
    """, rows=rows, batch_key=batch_key).single()

    if (
        outcome is None
        or outcome["written"] != len(rows)
    ):
        raise RuntimeError(
            "Scrittura ValidatedFact incompleta: rollback"
        )


def audit_links(session, batch_key):
    return session.run("""
        MATCH (
            b:ValidatedFactBatch {id:$batch_key}
        )-[:HAS_FACT]->(f:ValidatedFact)

        OPTIONAL MATCH
        (c:Claim)-[:HAS_VALIDATED_FACT]->(f)

        OPTIONAL MATCH
        (v:ClaimValidation)-[:VALIDATED_AS]->(f)

        OPTIONAL MATCH
        (a:Article)-[:HAS_VALIDATED_FACT]->(f)

        OPTIONAL MATCH
        (f)-[:SUBJECT_MENTION]->
        (sm:ValidatedMention)

        OPTIONAL MATCH
        (f)-[:OBJECT_MENTION]->
        (om:ValidatedMention)

        OPTIONAL MATCH
        (x:VerifiedClaimCorrection)
        -[:MATERIALIZED_AS]->(f)

        WITH
            f,
            count(DISTINCT c) AS nc,
            count(DISTINCT v) AS nv,
            count(DISTINCT a) AS na,
            count(DISTINCT sm) AS ns,
            count(DISTINCT om) AS no,
            count(DISTINCT x) AS nx

        RETURN
            count(f) AS total,
            count(DISTINCT f.claim_id)
                AS distinct_claims,

            sum(
                CASE
                    WHEN
                        nc = 1
                        AND nv = 1
                        AND na = 1
                        AND ns = 1
                        AND no = 1
                        AND nx = CASE
                            WHEN f.representation =
                                'verified_correction'
                            THEN 1
                            ELSE 0
                        END
                    THEN 0
                    ELSE 1
                END
            ) AS broken_links,

            sum(
                CASE
                    WHEN f.eligible_for_propagation = true
                    THEN 1
                    ELSE 0
                END
            ) AS prematurely_enabled

    """, batch_key=batch_key).single()


def run(args):
    if args.commit and args.limit:
        raise ValueError(
            "--commit non consente --limit"
        )

    if (
        args.batch_size < 1
        or args.limit < 0
        or args.padding < 1
    ):
        raise ValueError(
            "--batch-size >=1, --limit >=0, --padding >=1"
        )

    batch_key = (
        f"{VERSION}:{args.batch_id}"
    )

    driver = open_driver()

    try:
        with driver.session(
            database=CFG.neo4j_database
        ) as session:

            # Il batch di validazione deve
            # comprendere tutti i claim originali.
            validations = session.run("""
                MATCH (
                    v:ClaimValidation {
                        batch_id:$batch_id
                    }
                )

                RETURN
                    count(v) AS n,
                    count(DISTINCT v.claim_id)
                        AS distinct_claims,
                    sum(
                        CASE
                            WHEN v.status IN [
                                'accepted',
                                'corrected_verified'
                            ]
                            THEN 1
                            ELSE 0
                        END
                    ) AS eligible
            """, batch_id=args.batch_id).single()

            total_claims = session.run("""
                MATCH (c:Claim)
                RETURN count(c) AS n
            """).single()["n"]

            if (
                validations["n"] == 0
                or validations["n"] != total_claims
                or validations["distinct_claims"]
                != total_claims
            ):
                raise ValueError(
                    "Batch validazioni non completo: "
                    f"{validations['n']}/{total_claims}"
                )

            expected = validations["eligible"]

            query = SOURCE_QUERY + (
                "\nLIMIT $sample"
                if args.limit
                else ""
            )

            records = list(session.run(
                query,
                batch_id=args.batch_id,
                padding=args.padding,
                sample=args.limit,
            ))

            if (
                not args.limit
                and len(records) != expected
            ):
                raise ValueError(
                    "Link o dati sorgente mancanti: "
                    f"{len(records)}/{expected}"
                )

            # Tutti i record selezionati vengono
            # verificati prima di scrivere.
            rows = [
                make_row(record, args.batch_id)
                for record in tqdm(
                    records,
                    desc="Validate source",
                    unit="fact",
                )
            ]

            if (
                len({
                    row["claim_id"]
                    for row in rows
                }) != len(rows)
            ):
                raise ValueError(
                    "Un claim ha più fact nella vista"
                )

            fingerprint = digest([
                [
                    row["id"],
                    row["fact"]["claim_hash"],
                    row["correction_id"],
                    row["fact"]["predicate"],
                    row["fact"]["supporting_span"],
                ]
                for row in rows
            ])

            counts = Counter(
                row["fact"]["representation"]
                for row in rows
            )

            print(
                "Source checks passed:",
                dict(counts),
                "total:",
                len(rows),
            )

            print(
                "Source fingerprint:",
                fingerprint,
            )

            if not args.commit:
                for row in rows[:args.limit or 5]:
                    fact = row["fact"]

                    print(json.dumps(
                        {
                            key: fact.get(key)
                            for key in (
                                "claim_id",
                                "representation",
                                "predicate",
                                "subject_name",
                                "object_name",
                                "supporting_span",
                            )
                        },
                        ensure_ascii=False,
                    ))

                print(
                    "DRY RUN: nessuna scrittura"
                )
                return

            for cypher in CONSTRAINTS:
                session.run(cypher).consume()

            state = session.run("""
                MERGE (
                    b:ValidatedFactBatch {id:$key}
                )

                ON CREATE SET
                    b.batch_id = $batch_id,
                    b.version = $version,
                    b.status = 'building',
                    b.expected_facts = $expected,
                    b.source_fingerprint = $fingerprint,
                    b.created_at = $created_at

                RETURN properties(b) AS data

            """,
                key=batch_key,
                batch_id=args.batch_id,
                version=VERSION,
                expected=expected,
                fingerprint=fingerprint,
                created_at=datetime.now(
                    timezone.utc
                ).isoformat(),
            ).single()["data"]

            for field, wanted in (
                ("batch_id", args.batch_id),
                ("version", VERSION),
                ("expected_facts", expected),
                ("source_fingerprint", fingerprint),
            ):
                if state.get(field) != wanted:
                    raise ValueError(
                        "Batch vista già esistente "
                        f"e incompatibile: {field}"
                    )

            if state.get("status") not in {
                "building",
                "ready",
            }:
                raise ValueError(
                    "Stato batch vista inatteso"
                )

            for start in tqdm(
                range(
                    0,
                    len(rows),
                    args.batch_size,
                ),
                desc="Materialize",
                unit="batch",
            ):
                session.execute_write(
                    write_batch,
                    rows[
                        start:start + args.batch_size
                    ],
                    batch_key,
                )

            stats = audit_links(
                session,
                batch_key,
            )

            if (
                stats["total"] != expected
                or stats["distinct_claims"] != expected
                or stats["broken_links"] != 0
                or stats["prematurely_enabled"] != 0
            ):
                raise ValueError(
                    "Vista incompleta o incoerente: "
                    f"{dict(stats)}"
                )

            session.run("""
                MATCH (
                    b:ValidatedFactBatch {id:$key}
                )

                WHERE
                    b.source_fingerprint = $fingerprint
                    AND b.expected_facts = $expected

                SET
                    b.status = 'ready',
                    b.ready_at = coalesce(
                        b.ready_at,
                        $now
                    )
            """,
                key=batch_key,
                fingerprint=fingerprint,
                expected=expected,
                now=datetime.now(
                    timezone.utc
                ).isoformat(),
            ).consume()

            print(
                "COMMIT completed:",
                dict(stats),
            )

    finally:
        driver.close()


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
    )

    parser.add_argument(
        "--batch-id",
        default=BATCH_ID_DEFAULT,
    )

    parser.add_argument(
        "--padding",
        type=int,
        default=600,
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limita il dry run a N fatti; 0=tutti",
    )

    parser.add_argument(
        "--commit",
        action="store_true",
    )

    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
