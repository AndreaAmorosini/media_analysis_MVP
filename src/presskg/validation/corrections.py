
#!/usr/bin/env python3
"""Correction & Reverification per PressKG."""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import re
import unicodedata

from openai import OpenAI
from tqdm import tqdm

from presskg.settings import CFG
from presskg.entity_ontology import TYPES
from presskg.ontology import CANONICAL as BASE_PREDICATES, ALIASES as BASE_ALIASES
from presskg.predicate_catalog import CANONICAL as NEWS_PREDICATES, ALIASES as NEWS_ALIASES , normalize_predicate_name

from .primary import (
    BOOL_FIELDS,
    MODES,
    ask_judge,
    claim_hash,
    check_relation_roles,
    dump,
    fetch_one,
    normalized,
    open_driver,
    validate_judgement,
)

from .decisions import (
    checked_judgement,
    read_rows,
)

VERSION = "correction-reverification-v1"

PREDICATES = (
    set(BASE_PREDICATES)
    | set(NEWS_PREDICATES)
)

VERIFY_PROMPT = """
Sei un verificatore indipendente di triple
estratte da articoli di giornale italiani.

Ricevi UNA SOLA tripla candidata, la sua
evidence e un breve context.

Non conosci il claim originale e non devi
ricostruirlo. Non proporre ulteriori correzioni.

Controlla:
- soggetto e tipo del soggetto
- oggetto e tipo dell'oggetto
- significato esatto del predicato
- direzione dell'azione
- modalità e attribuzione
- stato temporale effettivo dell'azione

Approvare NON significa finanziare.
Attuare NON significa eseguire materialmente.
Gestire un servizio NON significa gestire
gli abitanti o i comuni serviti.
Una denuncia futura NON è già avvenuta.

Rispondi SOLO con un oggetto JSON completo:

{
 "verdict":"supported_as_is|unsupported|insufficient_context",
 "subject_correct":true,
 "subject_type_correct":true,
 "object_correct":true,
 "object_type_correct":true,
 "predicate_correct":true,
 "direction_correct":true,
 "modality_correct":true,
 "attribution_correct":true,
 "semantic_family":"causal|operational|financial|communication|legal|participation|descriptive|quantitative|other",
 "temporal_status":"completed|ongoing|planned|forecast|hypothetical|unknown",
 "corrected_claim":null,
 "supporting_span":"citazione letterale e continua",
 "reason":"breve motivazione"
}

Utilizza supported_as_is SOLTANTO quando
tutte le dimensioni sono corrette.

Tutti i campi *_correct devono essere true
quando il verdetto è supported_as_is.

Il supporting_span deve essere copiato
letteralmente da evidence oppure context.

Se il testo non consente di stabilire
la relazione con sicurezza, utilizza
insufficient_context.

Non inventare soggetti, predicati o oggetti.
Non assegnare sentiment né reputazione.
"""


def digest(value):
    return hashlib.sha256(
        dump(value).encode("utf-8")
    ).hexdigest()


def read_jsonl(
    path,
    key,
    allow_repeated=False,
):
    result = {}

    with Path(path).open(
        encoding="utf-8"
    ) as source:
        for line_no, line in enumerate(source, 1):
            if not line.strip():
                continue

            row = json.loads(line)
            identifier = row[key]

            if identifier in result and not allow_repeated:
                raise ValueError(
                    f"ID duplicato in {path}:{line_no}: "
                    f"{identifier}"
                )

            result[identifier] = row

    return result


def check_proposal(claim, proposal):
    """Controlli deterministici sulla correzione."""

    errors = []

    if not isinstance(proposal, dict):
        return ["proposal_not_object"]

    context = normalized(
        claim.get("context")
    )

    for role in ("subject", "object"):
        entity = proposal.get(role)

        if not isinstance(entity, dict):
            errors.append(f"{role}_missing")
            continue

        name = entity.get("name")
        entity_type = entity.get("type")

        if not isinstance(name, str) or not name.strip():
            errors.append(f"{role}_invalid_name")

        elif normalized(name) not in context:
            errors.append(f"{role}_not_grounded")

        if entity_type not in TYPES:
            errors.append(f"{role}_invalid_type")

    predicate = proposal.get("predicate")

    if predicate not in PREDICATES or predicate == "OTHER":
        errors.append(
            "predicate_outside_ontology_or_other"
        )

    if proposal.get("modality") not in MODES:
        errors.append("invalid_modality")

    attribution = proposal.get("attribution")

    if attribution is not None:
        if not isinstance(attribution, str):
            errors.append("invalid_attribution")
        elif (
            attribution.strip()
            and normalized(attribution) not in context
        ):
            errors.append("attribution_not_grounded")

    obj = proposal.get("object")

    if isinstance(obj, dict):
        errors.extend(
            check_relation_roles(
                predicate,
                obj.get("type"),
            )
        )

    if (
        not claim.get("quote")
        or claim.get("quote") != claim.get("proof")
    ):
        errors.append("invalid_original_evidence")

    return sorted(set(errors))


def canonicalize_proposal(proposal):
    #Canonicalizza esclusivamente predicati riconosciuti.
    if not isinstance(proposal, dict):
        return proposal, None

    result = deepcopy(proposal)

    raw = result.get("predicate")

    name = normalize_predicate_name(raw)

    if name in PREDICATES and name != "OTHER":
        canonical = name
    else:
        base = BASE_ALIASES.get(name)
        news = NEWS_ALIASES.get(name)

        if (
            base is not None
            and news is not None
            and base != news
        ):
            raise ValueError(
                f"Alias ambiguo: {name}: "
                f"{base} / {news}"
            )

        canonical = (
            news if news is not None else base
        )

    if canonical not in PREDICATES:
        return result, None

    if canonical == "OTHER":
        return result, None

    result["predicate"] = canonical

    return result, canonical

def candidate_for(row, judgement):
    """Identificatore deterministico della proposta canonica."""

    proposal, canonical = canonicalize_proposal(
        judgement.get("corrected_claim")
    )

    if canonical is None:
        raise ValueError(
            "Impossibile creare un candidato "
            "con predicato non canonico"
        )

    base = {
        "claim_id": row["claim_id"],
        "claim_hash": row["claim_hash"],
        "primary_run_hash": row["run_hash"],
        "primary_model": row["model"],
        "proposal": proposal,
    }

    candidate_digest = digest(base)

    return {
        **base,
        "article_id": row["article_id"],
        "candidate_id": (
            "cc-" + candidate_digest[:32]
        ),
        "candidate_hash": candidate_digest,
    }


def target_claim(original, candidate):
    """Sostituisce i campi della tripla, non le evidenze."""

    proposal = candidate["proposal"]

    return {
        **original,
        "subject": proposal["subject"]["name"],
        "subject_type": proposal["subject"]["type"],
        "predicate": proposal["predicate"],
        "object": proposal["object"]["name"],
        "object_type": proposal["object"]["type"],
        "modality": proposal["modality"],
        "attribution": proposal.get("attribution"),
    }

def canonical_literal(text):
    if not isinstance(text, str):
        return ""
    
    text = unicodedata.normalize("NFC", text)
    return re.sub(r"\s+", " ", text).strip()

def check_reverification(judgement, claim):
    errors = validate_judgement(
        judgement,
        claim,
    )

    if not isinstance(judgement, dict):
        return errors

    if judgement.get("verdict") not in {
        "supported_as_is",
        "unsupported",
        "insufficient_context",
    }:
        errors.append(
            "invalid_reverification_verdict"
        )

    if judgement.get("corrected_claim") is not None:
        errors.append(
            "no_additional_corrections"
        )

    if judgement.get("verdict") == "supported_as_is":
        fields = (
            *BOOL_FIELDS,
            "subject_type_correct",
            "object_type_correct",
        )

        if any(
            judgement.get(field) is not True
            for field in fields
        ):
            errors.append(
                "component_not_approved"
            )

        span = judgement.get("supporting_span")

        if (
            not isinstance(span, str)
            or len(span.strip()) < 12
        ):
            errors.append(
                "short_supporting_span"
            )

        elif not any(canonical_literal(span) in canonical_literal(source) for source in (claim.get("quote"), claim.get("context")) if isinstance(source, str) and source):
            errors.append(
                "supporting_span_not_literal"
            )

    return sorted(set(errors))




def prepare(args):
    primary = read_rows(Path(args.primary))

    output = Path(args.out)
    report = Path(
        args.report
        if args.report
        else str(output) + ".excluded.csv"
    )

    if output.resolve() == report.resolve():
        raise ValueError(
            "Output candidati e report devono essere distinti"
        )

    for path in (output, report):
        if path.exists() and not args.overwrite:
            raise FileExistsError(
                f"File già esistente: {path}. "
                "Usare --overwrite."
            )

    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)

    temporary = Path(str(output) + ".tmp")
    report_tmp = Path(str(report) + ".tmp")

    report_fields = [
        "claim_id",
        "original_predicate",
        "proposed_predicate",
        "canonical_predicate",
        "reason",
        "primary_validation_errors",
    ]

    counts = Counter()
    reasons = Counter()

    def record_exclusion(writer, cid, row, proposal,
                         canonical, problems):
        problems = sorted(set(problems))

        writer.writerow({
            "claim_id": cid,
            "original_predicate": row.get("predicate", ""),
            "proposed_predicate": (
                str(proposal.get("predicate") or "")
                if isinstance(proposal, dict)
                else ""
            ),
            "canonical_predicate": canonical or "",
            "reason": ";".join(problems),
            "primary_validation_errors": (
                row.get("validation_errors") or ""
            ),
        })

        counts["excluded"] += 1
        reasons.update(problems)

    driver = open_driver()

    try:
        with (
            driver.session(
                database=CFG.neo4j_database
            ) as session,
            temporary.open(
                "w", encoding="utf-8"
            ) as destination,
            report_tmp.open(
                "w", encoding="utf-8", newline=""
            ) as rejected,
        ):
            writer = csv.DictWriter(
                rejected, fieldnames=report_fields
            )
            writer.writeheader()

            for cid, row in tqdm(
                primary.items(),
                desc="Prepare corrections",
                unit="claim",
            ):
                if row["verdict"] != "correction_proposed":
                    continue

                if (
                    args.limit > 0
                    and counts["proposed"] >= args.limit
                ):
                    break

                counts["proposed"] += 1

                try:
                    raw_judgement = json.loads(
                        row.get("judge_json") or "{}"
                    )
                except (ValueError, TypeError):
                    raw_judgement = None

                raw_proposal = (
                    raw_judgement.get("corrected_claim")
                    if isinstance(raw_judgement, dict)
                    else None
                )

                proposal, canonical = canonicalize_proposal(
                    raw_proposal
                )

                claim = fetch_one(
                    session,
                    cid,
                    args.context_chars,
                )

                if claim is None:
                    record_exclusion(
                        writer, cid, row,
                        raw_proposal, canonical,
                        ["missing_original_claim"],
                    )
                    continue

                judgement = checked_judgement(
                    row, claim
                )

                if judgement is None:
                    record_exclusion(
                        writer, cid, row,
                        raw_proposal, canonical,
                        ["invalid_primary_judgement"],
                    )
                    continue

                proposal, canonical = canonicalize_proposal(
                    judgement.get("corrected_claim")
                )

                problems = check_proposal(
                    claim, proposal
                )

                if canonical is None:
                    problems.append(
                        "predicate_outside_ontology_or_other"
                    )

                if problems:
                    record_exclusion(
                        writer, cid, row,
                        judgement.get("corrected_claim"),
                        canonical,
                        problems,
                    )
                    continue

                candidate = candidate_for(
                    row, judgement
                )

                destination.write(
                    dump(candidate) + "\n"
                )

                counts["prepared"] += 1

        # I file originali vengono preservati
        # fino al completamento della preparazione.
        if args.overwrite:
            timestamp = datetime.now(
                timezone.utc
            ).strftime("%Y%m%dT%H%M%S%fZ")

            for path in (output, report):
                if path.exists():
                    backup = path.with_name(
                        path.name
                        + f".before_prepare.{timestamp}.bak"
                    )
                    shutil.copy2(path, backup)
                    print("Backup:", backup, flush=True)

        temporary.replace(output)
        report_tmp.replace(report)

    finally:
        driver.close()
        temporary.unlink(missing_ok=True)
        report_tmp.unlink(missing_ok=True)

    print("Candidates:", dict(counts), flush=True)
    print("Exclusion reasons:", dict(reasons), flush=True)
    print("Candidates output:", output, flush=True)
    print("Excluded report:", report, flush=True)


def verify(args):
    candidates = read_jsonl(
        args.candidates,
        "candidate_id",
    )

    if any(
        candidate["primary_model"] == args.model
        for candidate in candidates.values()
    ):
        raise ValueError(
            "Il verificatore deve essere diverso "
            "dal giudice primario."
        )

    run_signature = digest({
        "version": VERSION,
        "prompt": VERIFY_PROMPT,
        "model": args.model,
        "tokens": args.max_tokens,
        "context": args.context_chars,
        "retries": args.retries,
        "json_mode": CFG.json_mode,
    })[:24]

    output = Path(args.out)
    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    previous = {}

    if output.exists():
        if not args.resume:
            raise FileExistsError(
                f"{output} già esistente. "
                "Usare --resume."
            )

        previous = read_jsonl(
            output,
            "candidate_id",
            allow_repeated=True,
        )

        if any(
            item.get("run_signature") != run_signature
            or item.get("model") != args.model
            for item in previous.values()
        ):
            raise ValueError(
                "Firma diversa dalle valutazioni salvate. "
                "Usare un nuovo file di output."
            )

    write_target = output
    if args.retry_failed:
        if not args.resume or not output.is_file():
            raise ValueError("--retry-failed richiede --resume e un file di riverifiche esistente")
        
        write_target = Path(str(output) + ".retrying")
        
        if write_target.exists():
            staged = read_jsonl(write_target, "candidate_id", allow_repeated=True)
            for cid, item in staged.items():
                if (cid not in candidates or item.get("run_signature") != run_signature or item.get("model") != args.model or item.get("candidate_hash") != candidates[cid]["candidate_hash"]
                    or item.get("claim_hash") != candidates[cid]["claim_hash"] or item.get("claim_id") != candidates[cid]["claim_id"]):
                    raise ValueError(f"Retry styage incoerente: {cid}")
            previous.update(staged)

    pending = [
        candidate
        for candidate in candidates.values()
        if (
            candidate["candidate_id"] not in previous
            or (
                args.retry_failed
                and previous[
                    candidate["candidate_id"]
                ].get("errors")
            )
        )
    ]

    if args.limit > 0:
        pending = pending[:args.limit]

    print(
        f"Candidati: {len(candidates)}; "
        f"già registrati: {len(previous)}; "
        f"da valutare: {len(pending)}",
        flush=True,
    )

    client = OpenAI(
        base_url=CFG.base_url,
        api_key=CFG.api_key,
        timeout=args.timeout,
        max_retries=0,
    )

    driver = open_driver()
    consecutive_errors = 0

    try:
        with (
            driver.session(
                database=CFG.neo4j_database
            ) as session,
            write_target.open(
                "a",
                encoding="utf-8",
            ) as destination,
        ):
            judge_args = SimpleNamespace(
                retries=args.retries,
                max_tokens=args.max_tokens,
            )

            for candidate in tqdm(
                pending,
                desc="Reverify",
                unit="claim",
            ):
                cid = candidate["claim_id"]

                original = fetch_one(
                    session,
                    cid,
                    args.context_chars,
                )

                if (
                    original is None
                    or claim_hash(original)
                    != candidate["claim_hash"]
                ):
                    raise ValueError(
                        f"Claim assente o modificato: {cid}"
                    )

                problems = check_proposal(
                    original,
                    candidate["proposal"],
                )

                if problems:
                    raise ValueError(
                        f"Candidato non più valido: {cid}"
                    )

                synthetic_claim = target_claim(
                    original,
                    candidate,
                )

                fields = (
                    "title",
                    "publication_date",
                    "subject",
                    "subject_type",
                    "predicate",
                    "object",
                    "object_type",
                    "modality",
                    "attribution",
                    "quote",
                    "context",
                )

                payload = {
                    field: synthetic_claim.get(field)
                    for field in fields
                }

                payload["evidence"] = payload.pop("quote")

                judgement, errors, attempts = ask_judge(
                    client,
                    args.model,
                    VERIFY_PROMPT,
                    payload,
                    judge_args,
                    synthetic_claim,
                    extra_validator=check_reverification
                )

                errors = sorted(set(
                    errors
                    + check_reverification(
                        judgement,
                        synthetic_claim,
                    )
                ))

                record = {
                    "candidate_id": candidate["candidate_id"],
                    "candidate_hash": candidate["candidate_hash"],
                    "claim_id": cid,
                    "claim_hash": candidate["claim_hash"],
                    "model": args.model,
                    "run_signature": run_signature,
                    "judgement": judgement,
                    "errors": errors,
                    "attempts": attempts,
                    "validation_policy_version": "reverification-nfc-whitespace-v1"
                }

                destination.write(
                    dump(record) + "\n"
                )
                destination.flush()

                consecutive_errors = (
                    consecutive_errors + 1
                    if errors
                    else 0
                )

                # if (
                #     consecutive_errors
                #     >= args.max_consecutive_errors
                # ):
                #     raise RuntimeError(
                #         "Troppe risposte consecutive non valide. "
                #         "Verificare Unsloth e riprendere "
                #         "con --resume."
                #     )

    finally:
        driver.close()
        
    
    if args.retry_failed and args.limit == 0:
        base = read_jsonl(
            output,
            "candidate_id",
            allow_repeated=True,
        )

        staged = (
            read_jsonl(
                write_target,
                "candidate_id",
                allow_repeated=True,
            )
            if write_target.exists()
            else {}
        )

        base.update(staged)

        if set(base) != set(candidates):
            raise ValueError(
                "Candidati e riverifiche non coincidono. "
                "Nessun file originale sostituito."
            )

        for cid, candidate in candidates.items():
            item = base[cid]

            if (
                item["candidate_hash"]
                != candidate["candidate_hash"]
                or item["claim_hash"]
                != candidate["claim_hash"]
                or item["claim_id"]
                != candidate["claim_id"]
                or item["run_signature"]
                != run_signature
                or item["model"] != args.model
            ):
                raise ValueError(
                    f"Riverifica incompatibile: {cid}"
                )

        if write_target.exists():
            temporary = Path(str(output) + ".merged.tmp")

            with temporary.open(
                "w", encoding="utf-8"
            ) as destination:
                for cid in candidates:
                    destination.write(
                        dump(base[cid]) + "\n"
                    )

            backup = output.with_name(
                output.name
                + ".before_retry."
                + digest({
                    "candidate_ids": sorted(base),
                    "source_size": output.stat().st_size,
                    "source_mtime_ns": output.stat().st_mtime_ns,
                })[:16]
                + ".bak"
            )

            shutil.copy2(output, backup)
            temporary.replace(output)
            write_target.unlink(missing_ok=True)

            print(
                "Riverifiche consolidate:", len(base),
                flush=True,
            )
            print("Backup:", backup, flush=True)



def correction_record(
    candidate,
    verification,
    batch_id,
):
    proposal = candidate["proposal"]
    judgement = verification["judgement"]
    cid = candidate["claim_id"]

    return {
        "validation_id": "cvauto-" + hashlib.sha256(
            f"{batch_id}|{cid}".encode()
        ).hexdigest()[:32],
        "id": "vcc-" + digest({
            "batch_id": batch_id,
            "candidate_hash": candidate["candidate_hash"],
        })[:32],
        "batch_id": batch_id,
        "claim_id": cid,
        "claim_hash": candidate["claim_hash"],
        "primary_run_hash": candidate["primary_run_hash"],
        "source_candidate_id": candidate["candidate_id"],
        "candidate_hash": candidate["candidate_hash"],
        "subject_name": proposal["subject"]["name"],
        "subject_type": proposal["subject"]["type"],
        "predicate": proposal["predicate"],
        "object_name": proposal["object"]["name"],
        "object_type": proposal["object"]["type"],
        "modality": proposal["modality"],
        "attribution": proposal.get("attribution"),
        "temporal_status": judgement["temporal_status"],
        "semantic_family": judgement["semantic_family"],
        "supporting_span": judgement["supporting_span"],
        "primary_model": candidate["primary_model"],
        "verification_model": verification["model"],
        "verification_run_hash": verification["run_signature"],
        "verification_json": dump(judgement),
        "updated_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }



def store_corrections(tx, records):
    ids = [row["id"] for row in records]

    if len(ids) != len(set(ids)):
        raise ValueError(
            "ID delle correzioni duplicati"
        )

    existing = tx.run("""
        UNWIND $rows AS row

        OPTIONAL MATCH
        (c:Claim {id: row.claim_id})
        -[:HAS_VALIDATION]->
        (v:ClaimValidation {id: row.validation_id})

        OPTIONAL MATCH
        (x:VerifiedClaimCorrection {id: row.id})

        OPTIONAL MATCH
        (v)-[:VERIFIED_CORRECTION]->
        (linked:VerifiedClaimCorrection)

        RETURN
            row.id AS id,
            properties(v) AS validation,
            properties(x) AS correction,
            collect(DISTINCT linked.id) AS linked_ids
    """, rows=records).data()

    if len(existing) != len(records):
        raise RuntimeError(
            "Preflight correzioni: cardinalità incoerente"
        )

    by_id = {item["id"]: item for item in existing}
    to_write = []

    for row in records:
        item = by_id[row["id"]]
        validation = item["validation"]
        correction = item["correction"]
        linked_ids = item["linked_ids"]

        if validation is None:
            raise RuntimeError(
                f"Validazione originale assente: "
                f"{row['claim_id']}"
            )

        for field in (
            "claim_hash",
            "batch_id",
            "primary_run_hash",
        ):
            if validation.get(field) != row[field]:
                raise RuntimeError(
                    f"Validazione incoerente: "
                    f"{row['claim_id']}: {field}"
                )

        if validation.get("status") not in {
            "pending_correction",
            "abstained",
            "corrected_verified",
        }:
            raise RuntimeError(
                f"Stato non correggibile: "
                f"{row['claim_id']}"
            )

        previous_id = validation.get("correction_id")

        if (
            previous_id is not None
            and previous_id != row["id"]
        ):
            raise RuntimeError(
                f"Correzione incompatibile: "
                f"{row['claim_id']}"
            )

        if (
            linked_ids
            and linked_ids != [row["id"]]
        ):
            raise RuntimeError(
                f"Correzioni collegate incompatibili: "
                f"{row['claim_id']}"
            )

        if correction is not None:
            for field, expected in row.items():
                if field == "updated_at":
                    continue

                if correction.get(field) != expected:
                    raise RuntimeError(
                        f"Correzione persistita differente: "
                        f"{row['id']}, campo {field}"
                    )

            if (
                validation.get("status")
                != "corrected_verified"
                or previous_id != row["id"]
                or linked_ids != [row["id"]]
            ):
                raise RuntimeError(
                    f"Correzione già esistente "
                    f"con collegamenti incoerenti: {row['id']}"
                )

            # Già persistita in modo coerente.
            continue

        if (
            validation.get("status")
            == "corrected_verified"
        ):
            raise RuntimeError(
                f"Validazione già corretta "
                f"ma correzione mancante: {row['id']}"
            )

        to_write.append(row)

    if not to_write:
        return

    result = tx.run("""
        UNWIND $rows AS row

        MATCH
        (c:Claim {id: row.claim_id})
        -[:HAS_VALIDATION]->
        (v:ClaimValidation {id: row.validation_id})

        WHERE
            v.claim_hash = row.claim_hash
            AND v.batch_id = row.batch_id
            AND v.primary_run_hash = row.primary_run_hash

        MERGE
        (x:VerifiedClaimCorrection {id: row.id})

        ON CREATE SET
            x += row,
            x.ready_for_resolution = true,
            x.eligible_for_propagation = false

        MERGE (c)-[:HAS_VERIFIED_CORRECTION]->(x)
        MERGE (v)-[:VERIFIED_CORRECTION]->(x)

        SET
            v.status = 'corrected_verified',
            v.eligible_for_propagation = false,
            v.correction_id = row.id,
            v.updated_at = row.updated_at

        RETURN count(x) AS n
    """, rows=to_write).single()

    if (
        result is None
        or result["n"] != len(to_write)
    ):
        raise RuntimeError(
            "Persistenza delle correzioni incompleta: "
            "rollback"
        )


def apply(args):
    primary = read_rows(
        Path(args.primary)
    )

    candidates = read_jsonl(
        args.candidates,
        "candidate_id",
    )

    verifications = read_jsonl(
        args.verified,
        "candidate_id",
    )

    secondary = (
        read_rows(Path(args.secondary))
        if args.secondary
        else {}
    )

    if any(
        row["model"] == primary[cid]["model"]
        for cid, row in secondary.items()
        if cid in primary
    ):
        raise ValueError(
            "Giudice secondario uguale al primario."
        )
        
    if not candidates:
        raise ValueError("Nessun candidato disponibile")
    
    if set(candidates) != set(verifications):
        missing = set(candidates) - set(verifications)
        extra = set(verifications) - set(candidates)
        
        raise ValueError(f"Candidati e riverifiche non corrispondono: mancanti={len(missing)}, extra={len(extra)}")
    
    models = {
        v.get("model") for v in verifications.values()
    }
    
    signatures = {
        v.get("run_signature") for v in verifications.values()
    }
    
    if (len(models) != 1 or None in models or "" in models or len(signatures) != 1 or None in signatures or "" in signatures):
        raise ValueError("Modelli o firme delle riverifiche assenti o non uniformi")
    
    claim_ids = [candidate["claim_id"] for candidate in candidates.values()]
    
    if len(claim_ids) != len(set(claim_ids)):
        raise ValueError("Piu' correzzioni per lo stesso claim")
    
    for candidate_id, candidate in candidates.items():
        cid = candidate["claim_id"]
        source = primary.get(cid)
        verification = verifications[candidate_id]
        
        if source is None:
            raise ValueError(f"Claim primario mancante: {cid}")
        
        if (source["verdict"] != "correction_proposed" or source["claim_hash"] != candidate["claim_hash"] or source["run_hash"] != candidate["primary_run_hash"] or source["model"] != candidate["primary_model"]):
            raise ValueError(f"Provenienza del candidato incoerente: {cid}")
        
        if (verification["candidate_id"] != candidate_id or verification["candidate_hash"] != candidate["candidate_hash"] or verification["claim_id"] != cid or verification["claim_hash"] != candidate["claim_hash"] or
            verification["model"] == candidate["primary_model"]):
            raise ValueError(f"Riverifica incompatibile: {candidate_id}")

    counts = Counter()
    accepted = []

    driver = open_driver()

    try:
        with driver.session(
            database=CFG.neo4j_database
        ) as session:

            for candidate in tqdm(
                candidates.values(),
                desc="Correction gate",
                unit="claim",
            ):
                cid = candidate["claim_id"]

                row = primary.get(cid)

                if (
                    row is None
                    or row["verdict"] != "correction_proposed"
                ):
                    raise ValueError(
                        f"Candidato non presente "
                        f"nella primaria: {cid}"
                    )

                original = fetch_one(
                    session,
                    cid,
                    args.context_chars,
                )

                if (
                    original is None
                    or claim_hash(original)
                    != candidate["claim_hash"]
                ):
                    raise ValueError(
                        f"Claim assente o modificato: {cid}"
                    )

                first = checked_judgement(
                    row,
                    original,
                )

                if (
                    first is None
                    or candidate_for(row, first)
                    != candidate
                ):
                    raise ValueError(
                        f"Proposta diversa dal giudizio "
                        f"primario: {cid}"
                    )

                if check_proposal(
                    original,
                    candidate["proposal"],
                ):
                    counts["invalid_proposal"] += 1
                    continue

                original_second = secondary.get(cid)

                if original_second is not None:
                    second_judgement = checked_judgement(
                        original_second,
                        original,
                    )

                    if (
                        second_judgement
                        and second_judgement["verdict"]
                        == "supported_as_is"
                    ):
                        counts["disputed_original"] += 1
                        continue

                verification = verifications.get(
                    candidate["candidate_id"]
                )

                if verification is None:
                    counts["missing_reverification"] += 1
                    continue

                if (
                    verification["candidate_hash"]
                    != candidate["candidate_hash"]
                    or verification["claim_hash"]
                    != candidate["claim_hash"]
                    or verification["claim_id"] != cid
                    or verification["model"]
                    == candidate["primary_model"]
                ):
                    raise ValueError(
                        f"Riverifica incoerente: {cid}"
                    )

                checked = check_reverification(
                    verification.get("judgement"),
                    target_claim(original, candidate),
                )

                if verification.get("errors") or checked:
                    counts["invalid_reverification"] += 1
                    continue

                verdict = verification["judgement"]["verdict"]

                if verdict != "supported_as_is":
                    counts["not_supported"] += 1
                    continue

                accepted.append(
                    correction_record(
                        candidate,
                        verification,
                        args.batch_id,
                    )
                )
                counts["verified_correction"] += 1

            if args.commit:
                if counts["missing_reverification"]:
                    raise RuntimeError(
                        "Batch incompleto: mancano riverifiche. "
                        "Riprendere prima del commit."
                    )

                if accepted:
                    session.run("""
                        CREATE CONSTRAINT
                        verified_correction_id
                        IF NOT EXISTS
                        FOR (x:VerifiedClaimCorrection)
                        REQUIRE x.id IS UNIQUE
                    """).consume()

                    for offset in range(
                        0,
                        len(accepted),
                        args.batch_size,
                    ):
                        batch = accepted[
                            offset:offset + args.batch_size
                        ]

                        # Richiede il gate originale
                        # già persistito sul database.
                        stored = session.run("""
                            UNWIND $rows AS row

                            OPTIONAL MATCH
                            (c:Claim {id:row.claim_id})
                            -[:HAS_VALIDATION]->
                            (v:ClaimValidation {
                                id:row.validation_id
                            })

                            RETURN
                                row.claim_id AS claim_id,
                                v.claim_hash AS stored_hash,
                                v.status AS stored_status
                        """, rows=batch).data()

                        expected = {
                            item["claim_id"]: item["claim_hash"]
                            for item in batch
                        }

                        if (
                            len(stored) != len(batch)
                            or any(
                                r["stored_hash"]
                                != expected[r["claim_id"]]
                                or r["stored_status"] not in {
                                    "pending_correction",
                                    "abstained",
                                    "corrected_verified",
                                }
                                for r in stored
                            )
                        ):
                            raise RuntimeError(
                                "Gate originale non persistito "
                                "o incoerente."
                            )

                        session.execute_write(
                            store_corrections,
                            batch,
                        )

    finally:
        driver.close()

    print(
        "Correction decisions:",
        dict(counts),
        flush=True,
    )

    print(
        "COMMIT Neo4j"
        if args.commit
        else "DRY RUN: nessuna scrittura",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    sub = parser.add_subparsers(
        dest="command",
        required=True,
    )

    prepare_cmd = sub.add_parser("prepare")
    prepare_cmd.add_argument("--primary", required=True)
    prepare_cmd.add_argument("--out", required=True)
    prepare_cmd.add_argument(
        "--context-chars",
        type=int,
        default=600,
    )
    prepare_cmd.add_argument(
        "--overwrite",
        action="store_true",
    )
    prepare_cmd.add_argument("--report", default=None, help="CSV con cause di esclusione")
    prepare_cmd.add_argument(
        "--limit",
        type=int,
        default=0,
    )

    verify_cmd = sub.add_parser("verify")
    verify_cmd.add_argument("--candidates", required=True)
    verify_cmd.add_argument("--out", required=True)
    verify_cmd.add_argument("--model", required=True)
    verify_cmd.add_argument(
        "--context-chars",
        type=int,
        default=600,
    )
    verify_cmd.add_argument(
        "--max-tokens",
        type=int,
        default=1600,
    )
    verify_cmd.add_argument(
        "--timeout",
        type=float,
        default=600,
    )
    verify_cmd.add_argument(
        "--retries",
        type=int,
        default=1,
    )
    verify_cmd.add_argument(
        "--max-consecutive-errors",
        type=int,
        default=5,
    )
    verify_cmd.add_argument(
        "--limit",
        type=int,
        default=0,
    )
    verify_cmd.add_argument(
        "--resume",
        action="store_true",
    )
    verify_cmd.add_argument(
        "--retry-failed",
        action="store_true",
    )

    apply_cmd = sub.add_parser("apply")
    apply_cmd.add_argument("--primary", required=True)
    apply_cmd.add_argument("--candidates", required=True)
    apply_cmd.add_argument("--verified", required=True)
    apply_cmd.add_argument("--secondary")
    apply_cmd.add_argument("--batch-id", required=True)
    apply_cmd.add_argument(
        "--context-chars",
        type=int,
        default=600,
    )
    apply_cmd.add_argument(
        "--batch-size",
        type=int,
        default=200,
    )
    apply_cmd.add_argument(
        "--commit",
        action="store_true",
    )

    args = parser.parse_args()

    if args.context_chars <= 0:
        parser.error("--context-chars deve essere positivo")

    if (
        hasattr(args, "batch_size")
        and args.batch_size <= 0
    ):
        parser.error("--batch-size deve essere positivo")

    if (
        hasattr(args, "limit")
        and args.limit < 0
    ):
        parser.error("--limit deve essere >= 0")

    if (
        hasattr(args, "retries")
        and args.retries < 0
    ):
        parser.error("--retries deve essere >= 0")

    {
        "prepare": prepare,
        "verify": verify,
        "apply": apply,
    }[args.command](args)


if __name__ == "__main__":
    main()
