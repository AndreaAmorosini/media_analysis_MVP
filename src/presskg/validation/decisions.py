#!/usr/bin/env python3
"""Automatic conservative claim gate for PressKG.

Run from project root: pixi run python scripts/auto_apply_claim_validations.py ...
Reuses scripts/validate_claims.py for hash checks and judgement validation.
Never modifies Claim, FACT, or original evidence.
"""
from __future__ import annotations

import argparse
from argparse import Namespace
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from tqdm import tqdm
from presskg.config import CFG
from presskg.validation.batch import default_batch_id
from presskg.validation.primary import (
    FIELDS,
    claim_hash,
    fetch_one,
    open_driver,
    validate_judgement,
)

POLICY = "auto-gate-v1"
RISKY = {
    "ACCUSES", "BLAMES", "CAUSES", "DENIES", "DENOUNCES",
    "EXECUTES", "FILES_COMPLAINT", "FINANCED_BY", "FUNDS",
    "IMPLEMENTS", "MANAGES", "PREVENTS", "REDUCES", "RESTORES",
    "RESOLVES", "SANCTIONS", "VIOLATES", "ELIMINATES",
}
# Only a *candidate* for future graph propagation, not a reputation score.
PROPAGATION_CANDIDATES = RISKY | {"AFFECTS", "IMPROVES", "PROVIDES"}


def read_rows(path: Path) -> dict[str, dict[str, str]]:
    rows = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != FIELDS:
            raise ValueError(f"Schema CSV non compatibile: {path}")
        run_hashes = set()
        models = set()
        for row in reader:
            cid = row["claim_id"]
            if not cid or cid in rows:
                raise ValueError(f"claim_id mancante o duplicato: {cid!r} in {path}")
            rows[cid] = row
            run_hashes.add(row["run_hash"])
            models.add(row["model"])
        if len(run_hashes) > 1 or len(models) > 1:
            raise ValueError(f"Modelli o firme di valutazione misti in {path}")
    return rows


def is_risky(row: dict) -> bool:
    return row["predicate"].upper() in RISKY or any(
        flag in (row.get("flags") or "").split(";")
        for flag in ("offset_mismatch", "funder_vs_implementer", "denies_vs_denounces",
                     "managed_service_misread_as_area_or_people", "prospective_versus_completed")
    )


def select_secondary(args):
    primary = read_rows(Path(args.primary))

    ids = sorted(
        cid
        for cid, row in primary.items()
        if (
            not args.skip_correction_proposals
            or row["verdict"] != "correction_proposed"
        )
        and (
            is_risky(row)
            or row["verdict"] != "supported_as_is"
            or bool(row["validation_errors"])
        )
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.writer(f)
        writer.writerow(["claim_id"])
        writer.writerows(([cid] for cid in ids))

    print(
        f"Selezionati per seconda verifica: "
        f"{len(ids)}/{len(primary)} -> {out}"
    )

def checked_judgement(row: dict | None, claim: dict):
    if row is None:
        return None
    if row["claim_hash"] != claim_hash(claim):
        raise ValueError(f"Claim modificato dal momento della valutazione: {claim['claim_id']}")
    if row["article_id"] != claim["article_id"] or row["predicate"] != claim["predicate"]:
        raise ValueError(f"Metadati CSV incoerenti per {claim['claim_id']}")
    try:
        data = json.loads(row["judge_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if row["validation_errors"] or row["verdict"] != data.get("verdict"):
        return None
    if validate_judgement(data, claim):
        return None
    return data


def decide(primary: dict | None, secondary: dict | None, risk: bool) -> str:
    if primary is None:
        return "abstained"
    p = primary["verdict"]
    s = secondary["verdict"] if secondary is not None else None

    if p == "supported_as_is":
        if secondary is not None and s != "supported_as_is":
            return "abstained"
        if risk and secondary is None:
            return "pending_second"
        return "accepted"
    if p == "correction_proposed":
        if secondary is None:
            return "pending_correction"
        if s == "correction_proposed" and (
            secondary.get("corrected_claim") == primary.get("corrected_claim")
        ):
            return "pending_correction"
        return "abstained"
    if p == "unsupported":
        return "rejected" if secondary is None or s == "unsupported" else "abstained"
    return "abstained"



def write_batch(tx, rows):
    ids = [row["id"] for row in rows]

    if len(ids) != len(set(ids)):
        raise ValueError(
            "ID di validazione duplicati nel batch"
        )

    existing = tx.run("""
        UNWIND $rows AS row

        OPTIONAL MATCH
        (c:Claim {id: row.claim_id})

        OPTIONAL MATCH
        (v:ClaimValidation {id: row.id})

        OPTIONAL MATCH
        (c)-[r:HAS_VALIDATION]->(v)

        OPTIONAL MATCH
        (v)-[:VERIFIED_CORRECTION]->
        (x:VerifiedClaimCorrection)

        RETURN
            row.id AS id,
            count(DISTINCT c) AS claim_count,
            count(DISTINCT r) AS link_count,
            properties(v) AS stored,
            collect(DISTINCT x.id) AS corrections
    """, rows=rows).data()

    if len(existing) != len(rows):
        raise RuntimeError(
            "Preflight Neo4j: cardinalità incoerente"
        )

    by_id = {item["id"]: item for item in existing}

    for row in rows:
        item = by_id[row["id"]]

        if item["claim_count"] != 1:
            raise RuntimeError(
                f"Claim assente o duplicato: {row['claim_id']}"
            )

        stored = item["stored"]

        if stored is None:
            continue

        if item["link_count"] != 1:
            raise RuntimeError(
                f"Validazione esistente non collegata: "
                f"{row['id']}"
            )

        # Una validazione esistente è immutabile
        # rispetto ai dati della valutazione.
        for field, expected in row.items():
            if field in {"status", "updated_at"}:
                continue

            if stored.get(field) != expected:
                raise RuntimeError(
                    f"Conflitto {row['id']}: "
                    f"campo {field}"
                )

        old_status = stored.get("status")

        if old_status == "corrected_verified":
            correction_ids = item["corrections"]

            if (
                len(correction_ids) != 1
                or stored.get("correction_id")
                != correction_ids[0]
            ):
                raise RuntimeError(
                    f"Correzione verificata incoerente: "
                    f"{row['id']}"
                )

            if row["status"] not in {
                "pending_correction",
                "abstained",
                "corrected_verified",
            }:
                raise RuntimeError(
                    f"Transizione di stato non consentita: "
                    f"{row['id']}"
                )

        elif old_status != row["status"]:
            raise RuntimeError(
                f"Conflitto sullo stato: {row['id']}: "
                f"{old_status} != {row['status']}"
            )

    result = tx.run("""
        UNWIND $rows AS row

        MATCH (c:Claim {id: row.claim_id})

        MERGE (v:ClaimValidation {id: row.id})

        ON CREATE SET
            v += row

        MERGE (c)-[:HAS_VALIDATION]->(v)

        RETURN count(v) AS n
    """, rows=rows).single()

    if (
        result is None
        or result["n"] != len(rows)
    ):
        raise RuntimeError(
            "Persistenza incompleta: rollback"
        )


def apply(args):
    if args.commit and args.limit != 0:
        raise ValueError(("Commit parziale non consentito: utilizzare --limit 0"))
    primary = read_rows(Path(args.primary))
    if not primary:
        raise ValueError("CSV primario vuoto")
    secondary = read_rows(Path(args.secondary)) if args.secondary else {}
    if secondary and not set(secondary).issubset(primary):
        raise ValueError("La valutazione secondaria contiene claim assenti dalla primaria")
    if secondary and next(iter(secondary.values()))["model"] == next(iter(primary.values()))["model"]:
        raise ValueError("I valutatori primario e secondario devono essere diversi")

    driver = open_driver()
    counts = Counter()
    now = datetime.now(timezone.utc).isoformat()
    buffer = []
    entries = list(primary.items())
    if args.limit:
        entries = entries[:args.limit]
    try:
        with driver.session(database=CFG.neo4j_database) as session:
            if args.commit:
                database_claims = session.run("""
                    MATCH (c:Claim)
                    RETURN count(c) AS total
                """).single()["total"]
                
                if database_claims != len(primary):
                    raise RuntimeError(
                        f"Corpus incompleto o incoerente: Neo4j={database_claims}, CSV={len(primary)}. Commit Annullato"
                    )
                
            if args.commit:
                session.run("""
                    CREATE CONSTRAINT claim_validation_id IF NOT EXISTS
                    FOR (v:ClaimValidation) REQUIRE v.id IS UNIQUE
                """).consume()
                
            for cid, p_row in tqdm(entries, desc="Claim gate", unit="claim"):
                claim = fetch_one(session, cid, args.context_chars)
                if claim is None:
                    raise ValueError(f"Claim non trovato in Neo4j: {cid}")
                p = checked_judgement(p_row, claim)
                s_row = secondary.get(cid)
                s = checked_judgement(s_row, claim) if s_row is not None else None
                if s_row is not None and s_row["claim_hash"] != p_row["claim_hash"]:
                    raise ValueError(f"I due giudici hanno revisionato versioni diverse: {cid}")
                status = decide(p, s, is_risky(p_row))
                counts[status] += 1
                modality = claim.get("modality")
                temporal = p.get("temporal_status") if p else "unknown"
                candidate = (
                    status == "accepted"
                    and claim["predicate"] in PROPAGATION_CANDIDATES
                    and modality == "asserted"
                    and temporal in {"completed", "ongoing"}
                )
                record = {
                    "id": "cvauto-" + hashlib.sha256(f"{args.batch_id}|{cid}".encode()).hexdigest()[:32],
                    "claim_id": cid,
                    "claim_hash": p_row["claim_hash"],
                    "batch_id": args.batch_id,
                    "policy_version": POLICY,
                    "status": status,
                    "eligible_for_propagation": False,
                    "candidate_for_propagation": candidate,
                    "primary_model": p_row["model"],
                    "secondary_model": s_row["model"] if s_row else None,
                    "primary_run_hash": p_row["run_hash"],
                    "secondary_run_hash": s_row["run_hash"] if s_row else None,
                    "primary_judge_json": p_row["judge_json"],
                    "secondary_judge_json": s_row["judge_json"] if s_row else None,
                    "corrected_claim_json": (
                        json.dumps(p["corrected_claim"], ensure_ascii=False)
                        if p and p.get("corrected_claim") is not None else None
                    ),
                    "temporal_status": temporal,
                    "semantic_family": p.get("semantic_family") if p else None,
                    "updated_at": now,
                }
                if args.commit:
                    buffer.append(record)
                    if len(buffer) >= args.batch_size:
                        session.execute_write(write_batch, buffer)
                        buffer.clear()
            if args.commit and buffer:
                session.execute_write(write_batch, buffer)
    finally:
        driver.close()
    print("Decisioni:", dict(sorted(counts.items())))
    print("Persistite su Neo4j" if args.commit else "DRY RUN: nessuna scrittura")


def run(args: Namespace) -> bool:
    command = getattr(args, "command", "apply")

    # batch id derivato dai modelli se non passato esplicitamente
    if getattr(args, "batch_id", None) is None:
        args.batch_id = default_batch_id()

    try:
        if command == "select-secondary":
            select_secondary(args)
        elif command == "apply":
            apply(args)
    except Exception as e:
        print(f"Errore inatteso: {e}")
        return False

    return True


def build_parser() -> argparse.ArgumentParser:
    """Sottocomandi: select-secondary e apply (gate automatico)."""
    parser = argparse.ArgumentParser(
        prog="presskg.validation.decisions",
        description="Gate automatico della validazione.",
    )
    parser.add_argument(
        "command", nargs="?", default="apply",
        choices=("select-secondary", "apply"),
    )

    parser.add_argument("--primary", default=None, help="CSV validazione primaria")
    parser.add_argument("--secondary", default=None, help="CSV secondo giudizio")
    parser.add_argument(
        "--out", default=None,
        help="CSV id selezionati (select-secondary)",
    )
    parser.add_argument("--skip-correction-proposals", action="store_true")
    parser.add_argument("--context-chars", type=int, default=600)
    parser.add_argument("--limit", type=int, default=0, help="0 = tutti")
    parser.add_argument(
        "--batch-id", default=None,
        help="id del batch ValidatedFact (default: derivato dai modelli)",
    )
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--commit", action="store_true", help="scrive su Neo4j")
    return parser