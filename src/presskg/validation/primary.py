#!/usr/bin/env python3
"""PressKG semantic review v1: Neo4j source READ-ONLY during generate.
Apply writes separate ClaimValidation nodes only after explicit approval.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from datetime import datetime, timezone
import shutil
import sqlite3

from neo4j import GraphDatabase
from openai import OpenAI
from json_repair import loads as repair_loads
from tqdm import tqdm
from presskg.settings import CFG
from presskg.entity_ontology import TYPES

VERSION = "claim-validation-v2.1-semantic-role-guards"
VERDICTS = {"supported_as_is", "correction_proposed", "unsupported", "insufficient_context"}
FAMILIES = {"causal", "operational", "financial", "communication", "legal", "participation", "descriptive", "quantitative", "other"}
TEMPORAL = {"completed", "ongoing", "planned", "forecast", "hypothetical", "unknown"}
MODES = {"asserted", "reported", "alleged", "denied", "hypothetical", "opinion", "uncertain"}
BOOL_FIELDS = ("subject_correct", "object_correct", "predicate_correct", "direction_correct", "modality_correct", "attribution_correct")
FIELDS = ["claim_id", "article_id", "subject", "predicate", "object", "flags", "verdict", "family", "temporal", "reason", "suggestion_json", "judge_json", "validation_errors", "claim_hash", "run_hash", "model", "judge_attempts", "review", "reviewer_note"]

PROMPT = """Sei un revisore indipendente di claim estratti da rassegne stampa italiane.
Non assegnare sentiment né reputazione. Valuta SOLO il claim target.
Il campo evidence è la citazione originale. context aiuta a disambiguare coreferenze,
ma NON autorizza ad attribuire al claim azioni appartenenti ad altri episodi.
Verifica chi compie l'azione, verso, relazione, oggetto, modalità e attribuzione.
'finanziato da X, Y soggetto attuatore' NON significa Y FUNDS il progetto.
'X denuncia Y' NON equivale a X DENIES Y. Una smentita autentica è DENIES.
Una previsione o un obiettivo NON è un risultato già realizzato.
Non dedurre responsabilità dalla sola gestione di una rete o dalla presenza a un evento.
Le fonti giornalistiche possono riferire accuse senza stabilirne la verità.
Se il claim è già corretto: supported_as_is; se la stessa evidenza permette
una correzione: correction_proposed; se contraddetto: unsupported;
se manca contesto sufficiente: insufficient_context.
La correction_proposed deve mantenere un soggetto, oggetto e predicato specifici,
con nomi ancorati al testo. Non creare relazioni dal nulla.
Non usare i flag euristici come verdetto: sono semplici motivi di campionamento.
Rispondi SOLTANTO con oggetto JSON:
{
 "verdict":"supported_as_is|correction_proposed|unsupported|insufficient_context",
 "subject_correct":true,
 "object_correct":true,
 "predicate_correct":true,
 "direction_correct":true,
 "modality_correct":true,
 "attribution_correct":true,
 "semantic_family":"causal|operational|financial|communication|legal|participation|descriptive|quantitative|other",
 "temporal_status":"completed|ongoing|planned|forecast|hypothetical|unknown",
 "corrected_claim":null,
 "supporting_span":"breve citazione esatta presente in evidence o context",
 "reason":"spiegazione sintetica"
}
Quando serve corrected_claim usa l'oggetto
{"subject":{"name":"...","type":"Company"}, "predicate":"IMPLEMENTS",
 "object":{"name":"...","type":"Project"}, "modality":"asserted", "attribution":null}.
Per ogni campo *_correct utilizza true, false o null se non accertabile.
Per supported_as_is tutti i campi *_correct devono essere true, inclusa attribuzione
(true anche quando l'attribuzione non è necessaria).
Non inventare nomi e non attribuire un fatto a chi lo sta solo riportando.
"""


# Specificazioni successive al prompt principale: dichiarazioni, ruoli e formato.
PROMPT += """

VERDETTI MUTUAMENTE ESCLUSIVI:
- supported_as_is => corrected_claim DEVE essere null; tutti i campi *_correct true.
- correction_proposed => corrected_claim DEVE essere un oggetto completo.
  Usa questo verdetto soltanto quando la tripla corretta e' supportata
  dal testo e sono identificabili soggetto, predicato e oggetto.
- unsupported => corrected_claim DEVE essere null. Usalo quando la
  relazione originale non e' supportata e non esiste una correzione
  completa e sufficientemente verificabile.
- insufficient_context => corrected_claim DEVE essere null, se manca
  evidenza per valutare il claim target.

Grounding: la stringa supporting_span deve essere una citazione COPIATA
LETTERALMENTE da evidence o context, breve e continua, senza ellissi,
riassunti, correzioni OCR o parole inventate.
Per i soggetti e gli oggetti modificati, copia preferibilmente un nome
presente letteralmente in evidence o context. Non inventare una
parafrasi come nome di una nuova entita'. Se il nome non e' verificabile,
non proporre la correzione.
Modality del corrected_claim: solo asserted, reported, alleged, denied,
hypothetical, opinion, uncertain. "planned" e "forecast" NON sono
modalita' di claim: usali nel campo separato temporal_status.

SEMANTICA:
- DENIES significa negare/smentire. "Denunciare un reato alla Procura"
  e' una segnalazione/denuncia formale (FILES_COMPLAINT), non DENIES.
  "Denunciare pubblicamente una criticita'" e' DENOUNCES, non DENIES.
  Una critica politica non e' automaticamente una denuncia giudiziaria.
- FUNDS: il soggetto finanzia il progetto/opera; se il testo dice
  "X finanzia, Y attua", Y NON e' il finanziatore. Un importo monetario
  e' un attributo del finanziamento, non il progetto finanziato.
- MANAGES: "X gestisce il servizio idrico in 74 comuni" NON significa
  "X gestisce i 74 comuni" o "X gestisce gli abitanti".
  La gestione di un servizio e la fornitura a una popolazione sono
  relazioni diverse. Non validare come supported_as_is un oggetto
  sbagliato solo perche' il nome del soggetto e' corretto.
- Progetti pianificati, obiettivi e interventi ancora da effettuare
  NON sono risultati gia' completati.

NON correggere contemporaneamente altre triple presenti nel contesto.
Una correzione deve riferirsi sempre e soltanto al claim target.
"""


PROMPT += """

CONTROLLO FINALE DELLA CORREZIONE

Prima di restituire correction_proposed,
verifica indipendentemente:

1. Il nuovo soggetto compie veramente
   l'azione descritta dal predicato?

2. L'oggetto rappresenta ciò su cui viene
   esercitata l'azione, oppure è soltanto
   una quantità, una località o un beneficiario?

3. Il nuovo predicato descrive precisamente
   l'azione del soggetto?

4. La relazione si riferisce al presente,
   a un risultato concluso, a un progetto
   futuro o a una semplice intenzione?

5. La nuova tripla corregge il claim target
   senza introdurre altri fatti?

REGOLE DI RUOLO

Una società che possiede una quota di una
società mista NON è automaticamente il
gestore diretto delle sue infrastrutture.

Un soggetto attuatore NON è automaticamente
il finanziatore del progetto.

Un programma o un piano NON è automaticamente
un finanziatore.

"X gestisce il servizio in 75 comuni"
NON significa "X gestisce 75 comuni".

"X realizza 97 interventi" NON significa
"X realizza la quantità 97".

REGOLE TEMPORALI

"Denuncerà" indica una denuncia futura,
non una denuncia già effettuata.

"Prevede di finanziare" non è equivalente
a un finanziamento già erogato.

"Consentirà di ridurre" descrive un
beneficio previsto, non un risultato
già osservato.

Il temporal_status deve descrivere
l'azione del claim target, non un altro
evento presente nel contesto.

Se il tempo dell'azione non è stabilibile,
usa temporal_status=unknown.

Non sostituire una tripla errata con
un'altra tripla soltanto plausibile.
Se non puoi verificare completamente
la correzione, preferisci unsupported
o insufficient_context.
"""


READ = """
MATCH (c:Claim)
WHERE c.id > $after AND ($predicates = [] OR c.predicate IN $predicates)
MATCH (c)-[:SUBJECT]->(s:GraphNode)
MATCH (c)-[:OBJECT]->(o:GraphNode)
MATCH (a:Article {id:c.article_id})
RETURN c.id AS claim_id, c.article_id AS article_id,
       c.predicate AS predicate, c.predicate_raw AS predicate_raw,
       c.modality AS modality, c.attribution AS attribution,
       c.quote AS quote, c.body_start AS body_start, c.body_end AS body_end,
       s.id AS subject_id, s.name AS subject, s.kind AS subject_type,
       o.id AS object_id, o.name AS object, o.kind AS object_type,
       a.title AS title, a.publication_date AS publication_date,
       a.content_hash AS content_hash,
       CASE WHEN c.body_start IS NOT NULL AND c.body_end > c.body_start
            THEN substring(a.body, c.body_start, c.body_end-c.body_start)
            ELSE null END AS proof,
       CASE WHEN c.body_start IS NOT NULL AND c.body_end > c.body_start
            THEN substring(a.body,
                CASE WHEN c.body_start > $padding THEN c.body_start-$padding ELSE 0 END,
                (c.body_end-c.body_start)+2*$padding)
            ELSE c.quote END AS context
ORDER BY c.id LIMIT $batch
"""

READ_ONE = READ.replace("c.id > $after AND ($predicates = [] OR c.predicate IN $predicates)", "c.id = $target")


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalized(value):
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


def claim_hash(c):
    return sha(dump({k: c.get(k) for k in (
        "claim_id", "article_id", "subject_id", "subject", "subject_type", "predicate",
        "predicate_raw", "object_id", "object", "object_type", "modality",
        "attribution", "quote", "body_start", "body_end", "content_hash"
    )}))


def flags(c):
    p = str(c.get("predicate") or "").upper()
    quote = normalized(c.get("quote"))
    results = []
    if c.get("proof") != c.get("quote"):
        results.append("offset_mismatch")
    if p == "DENIES" and "denunc" in quote:
        results.append("denies_vs_denounces")
    if p == "MANAGES" and (
        c.get("object_type") in {"Date", "Time", "Money", "Quantity", "PopulationGroup"}
        or c.get("subject_type") in {"Date", "Time", "Money", "Quantity"}
    ):
        results.append("manages_nonmanageable_object")
    if p == "FUNDS" and ("soggetto attuatore" in quote or "soggetto esecutore" in quote):
        results.append("funder_vs_implementer")
    if p in {"CAUSES", "REDUCES", "RESTORES", "REALIZES", "PREVENTS"} and any(
        token in quote for token in ("previst", "potra", "dovra", "consentira", "obiettivo", "mirano a")
    ):
        results.append("prospective_versus_completed")
    if p == "MANAGES" and c.get("object_type") in {"Location", "GPE", "Quantity", "PopulationGroup"}:
        obj = normalized(c.get("object"))
        if any(t in obj for t in ("comun", "abitant", "cittadin")):
            results.append("managed_service_misread_as_area_or_people")
    if p == "FUNDS" and c.get("object_type") == "Money":
        results.append("funding_amount_instead_of_project")
    if p == "OTHER":
        results.append("unmapped_predicate")
    return results


def parse_json(raw):
    raw = re.sub(r"^```(?:json)?\s*", "", (raw or "").strip(), flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return repair_loads(raw)


def check_relation_roles(predicate, object_type):
    """
    Controlli conservativi sulle combinazioni
    predicato / oggetto.

    Non dimostrano la correttezza semantica:
    bloccano soltanto alcune rappresentazioni
    strutturalmente sospette.
    """
    errors = []

    if predicate == "MANAGES":
        if object_type in {
            "GPE",
            "Location",
            "PopulationGroup",
            "Quantity",
            "Date",
            "Time",
        }:
            errors.append(
                "management_target_semantically_invalid"
            )

    if predicate == "IMPLEMENTS":
        if object_type in {
            "Money",
            "Quantity",
            "Date",
            "Time",
        }:
            errors.append(
                "implementation_target_semantically_invalid"
            )

    if predicate == "FUNDS":
        if object_type in {
            "Money",
            "Quantity",
            "Date",
            "Time",
        }:
            errors.append(
                "funding_amount_not_funded_object"
            )

    return errors


def validate_judgement(data, c):
    errors = []
    if not isinstance(data, dict):
        return ["not_json_object"]
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        errors.append("invalid_verdict")
    if data.get("semantic_family") not in FAMILIES:
        errors.append("invalid_semantic_family")
    if data.get("temporal_status") not in TEMPORAL:
        errors.append("invalid_temporal_status")
    for field in BOOL_FIELDS:
        if data.get(field) is not None and not isinstance(data.get(field), bool):
            errors.append("invalid_" + field)
    span = data.get("supporting_span")
    if verdict in {"supported_as_is", "correction_proposed"}:
        if not isinstance(span, str) or not span.strip():
            errors.append("missing_supporting_span")
        elif normalized(span) not in normalized(c.get("quote")) and normalized(span) not in normalized(c.get("context")):
            errors.append("ungrounded_supporting_span")
    changed = data.get("corrected_claim")
    if verdict == "correction_proposed":
        if not isinstance(changed, dict):
            errors.append("missing_corrected_claim")
        else:
            for role in ("subject", "object"):
                ent = changed.get(role)
                if not isinstance(ent, dict) or not str(ent.get("name") or "").strip():
                    errors.append("invalid_" + role)
                    continue
                if ent.get("type") not in TYPES:
                    errors.append("invalid_" + role + "_type")
                # Controllo debole: i nomi corretti devono risultare nel contesto.
                name_key = normalized(ent.get("name"))
                if name_key not in normalized(c.get("context")):
                    errors.append("new_" + role + "_not_grounded")
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", str(changed.get("predicate") or "")):
                errors.append("invalid_corrected_predicate")
            if changed.get("modality") not in MODES:
                errors.append("invalid_corrected_modality")
            attr = changed.get("attribution")
            if attr is not None and not isinstance(attr, str):
                errors.append("invalid_corrected_attribution")
            if (changed.get("subject") == {"name": c.get("subject"), "type": c.get("subject_type")}
                    and changed.get("object") == {"name": c.get("object"), "type": c.get("object_type")}
                    and changed.get("predicate") == c.get("predicate")
                    and changed.get("modality") == c.get("modality")
                    and changed.get("attribution") == c.get("attribution")):
                errors.append("correction_identical_to_original")
    elif changed is not None:
        errors.append("unexpected_corrected_claim")
    if verdict == "correction_proposed":
        if all(data.get(key) is True for key in BOOL_FIELDS):
            errors.append("correction_without_identified_error")
    if verdict == "supported_as_is":
        if any(data.get(key) is not True for key in BOOL_FIELDS):
            errors.append("supported_but_fields_incorrect")
        # Guardrail specifici: e' formalmente possibile ma semanticamente
        # falso che un gestore di servizi "gestisca" una popolazione/comune.
        f = flags(c)
        if "managed_service_misread_as_area_or_people" in f:
            errors.append("management_target_mismatch")
        if "funding_amount_instead_of_project" in f:
            errors.append("funding_amount_as_object")
            
    # Controlla la relazione effettivamente
    # proposta, non soltanto quella originale.
    if verdict == "supported_as_is":
        target_predicate = c.get("predicate")
        target_object_type = c.get("object_type")

        errors.extend(
            check_relation_roles(
                target_predicate,
                target_object_type,
            )
        )

    elif (
        verdict == "correction_proposed"
        and isinstance(changed, dict)
    ):
        corrected_object = changed.get("object")

        if isinstance(corrected_object, dict):
            errors.extend(
                check_relation_roles(
                    changed.get("predicate"),
                    corrected_object.get("type"),
                )
            )

    return sorted(set(errors))


def ask_judge(client, model, system_prompt, payload, args, c, extra_validator=None):
    """Riprova solo su errori formali, senza accettare correzioni arbitrarie."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": dump(payload)},
    ]
    best = None
    for attempt in range(args.retries + 1):
        try:
            response = client.chat.completions.create(
                model=model, temperature=0, max_tokens=args.max_tokens,
                messages=messages, extra_body={"enable_thinking": False},
                **({"response_format": {"type": "json_object"}} if CFG.json_mode else {}),
            )
            choice = response.choices[0]
            if choice.finish_reason == "length":
                raise ValueError("output_troncato")
            judgement = parse_json(choice.message.content or "")
            errors = validate_judgement(judgement, c)
            if extra_validator is not None:
                errors = sorted(set(errors + extra_validator(judgement, c)))
        except Exception as exc:
            judgement = {
                "verdict": "insufficient_context", "semantic_family": "other",
                "temporal_status": "unknown", "corrected_claim": None,
                "reason": f"model_error: {type(exc).__name__}: {exc}"[:350],
            }
            errors = ["model_or_parser_error"]
        if best is None or len(errors) < len(best[1]):
            best = (judgement, errors, attempt + 1)
        if not errors:
            return judgement, [], attempt + 1
        if attempt < args.retries:
            # Contesto iniziale conservato e precedenti errori esplicitati.
            messages.append({"role": "assistant", "content": dump(judgement)})
            messages.append({
                "role": "user",
                "content": (
                    "Il JSON precedente non supera la validazione. "
                    "Errori: " + ", ".join(errors) + ". "
                    "Correggi il VERDETTO o la proposta, non inventare dettagli. "
                    "Se proponi una correzione usa correction_proposed e "
                    "un corrected_claim verificabile; altrimenti usa "
                    "unsupported e corrected_claim=null. "
                    "Copia supporting_span letteralmente. "
                    "I nomi nuovi di subject/object devono essere presenti "
                    "nel contesto. Non confondere MANAGES-service con "
                    "MANAGES-comuni/cittadini, ne' FUNDS-progetto con "
                    "FUNDS-importo. Rispondi con un unico JSON valido."
                ),
            })
    return best


def make_row(c, model, run_hash, judgement, errors, judge_attempts=1):
    return {
        "claim_id": c["claim_id"], "article_id": c["article_id"],
        "subject": c["subject"], "predicate": c["predicate"], "object": c["object"],
        "flags": ";".join(flags(c)), "verdict": judgement.get("verdict", "insufficient_context"),
        "family": judgement.get("semantic_family", "other"),
        "temporal": judgement.get("temporal_status", "unknown"),
        "reason": str(judgement.get("reason") or "")[:600],
        "suggestion_json": dump(judgement.get("corrected_claim")),
        "judge_json": dump(judgement),
        "validation_errors": ";".join(errors),
        "claim_hash": claim_hash(c), "run_hash": run_hash, "model": model,
        "judge_attempts": judge_attempts, "review": "", "reviewer_note": ""
    }


def open_driver():
    return GraphDatabase.driver(CFG.neo4j_uri, auth=(CFG.neo4j_user, CFG.neo4j_password))


def fetch_one(session, claim_id, padding):
    rec = session.run(READ_ONE, target=claim_id, padding=padding, batch=1).single()
    return dict(rec) if rec else None


def run_generate(args):
    selected_ids = None
    if args.claim_ids_file:
        with Path(args.claim_ids_file).open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            if "claim_id" not in (reader.fieldnames or []):
                raise ValueError("--claim-ids-file deve avere una colonna claim_id")
            selected_ids = {row["claim_id"] for row in reader if row["claim_id"]}
        if not selected_ids:
            raise ValueError("--claim-ids-file senza claim_id")
    model = args.model or CFG.eval_model_id
    system_prompt = PROMPT + "\nTIPI CANONICI ENTITÀ: " + ", ".join(sorted(TYPES))
    signature = sha(dump({"version": VERSION, "model": model, "prompt": system_prompt,
                            "predicates": sorted(args.predicates), "only_flagged": args.only_flagged,
                            "padding": args.context_chars, "tokens": args.max_tokens,
                            "retries": args.retries,
                            "selected_ids": sorted(selected_ids) if selected_ids is not None else None}))[:24]
    
    if args.retry_invalid:
        retry_invalid(args, model, system_prompt, signature, selected_ids)
        return
    
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    processed = set()
    if target.exists():
        if not args.resume:
            raise SystemExit(f"File già esistente: {target}. Usa --resume o un nuovo --out")
        with target.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames != FIELDS:
                raise ValueError("Schema CSV differente")
            for line in reader:
                if line["run_hash"] != signature:
                    raise ValueError("Firma modello/prompt diversa: usa un nuovo CSV")
                processed.add(line["claim_id"])
    client = OpenAI(base_url=CFG.base_url, api_key=CFG.api_key, timeout=CFG.request_timeout, max_retries=0)
    driver = open_driver()
    counters = Counter()
    try:
        with driver.session(database=CFG.neo4j_database) as session, target.open(
            "a" if target.exists() else "w", encoding="utf-8", newline=""
        ) as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            if not processed and fh.tell() == 0:
                writer.writeheader()
            after = ""
            total = session.run(
                "MATCH (c:Claim) WHERE ($predicates = [] OR c.predicate IN $predicates) "
                "MATCH (c)-[:SUBJECT]->(s:GraphNode) "
                "MATCH (c)-[:OBJECT]->(o:GraphNode) "
                "MATCH (a:Article {id:c.article_id}) "
                "RETURN count(c) AS n",
                predicates=args.predicates).single()["n"]
            if args.limit > 0:
                total = min(total, args.limit)
            pbar = tqdm(total=total, unit="claim", desc="validazione")            

            while True:
                block = session.run(READ, after=after, predicates=args.predicates,
                                    padding=args.context_chars, batch=args.batch).data()
                if not block:
                    break
                after = block[-1]["claim_id"]
                for c in block:
                    if selected_ids is not None and c["claim_id"] not in selected_ids:
                        continue
                    if c["claim_id"] in processed:
                        counters["resumed"] += 1
                        continue
                    f = flags(c)
                    if args.only_flagged and not f:
                        continue
                    if c.get("proof") != c.get("quote") or not c.get("quote"):
                        judgement = {"verdict": "insufficient_context", "semantic_family": "other",
                                     "temporal_status": "unknown", "corrected_claim": None,
                                     "reason": "Offset o citazione non verificabili"}
                        errors = ["source_evidence_mismatch"]
                    else:
                        payload = {k: c.get(k) for k in (
                            "title", "publication_date", "subject", "subject_type", "predicate",
                            "predicate_raw", "object", "object_type", "modality", "attribution",
                            "quote", "context"
                        )}
                        payload["flags_for_priority_only"] = f
                        judgement, errors, attempts = ask_judge(
                            client, model, system_prompt, payload, args, c
                        )
                    row = make_row(c, model, signature, judgement, errors,
                                   attempts if c.get("proof") == c.get("quote") and c.get("quote") else 0)
                    writer.writerow(row)
                    fh.flush()
                    counters[row["verdict"]] += 1
                    if row["validation_errors"]:
                        counters["with_errors"] += 1
                    pbar.update(1)
                    if sum(v for k, v in counters.items() if k in VERDICTS) % 25 == 0:
                        print("Elaborati:", sum(v for k, v in counters.items() if k in VERDICTS), dict(counters), flush=True)
                    if args.limit > 0 and sum(v for k, v in counters.items() if k in VERDICTS) >= args.limit:
                        print("Generazione completata:", target, dict(counters))
                        return
        pbar.close()
        print("Generazione completata:", target, dict(counters))
    finally:
        driver.close()



def retry_invalid(args, model, system_prompt, signature, selected_ids):
    if not args.resume:
        raise ValueError(
            "--retry-invalid richiede --resume"
        )

    target = Path(args.out)

    if not target.is_file():
        raise FileNotFoundError(target)

    original = {}

    with target.open(
        encoding="utf-8", newline=""
    ) as source:
        reader = csv.DictReader(source)

        if reader.fieldnames != FIELDS:
            raise ValueError(
                "Schema CSV non compatibile"
            )

        for row in reader:
            cid = row["claim_id"]

            if not cid or cid in original:
                raise ValueError(
                    f"ID mancante o duplicato: {cid}"
                )

            if (
                row["run_hash"] != signature
                or row["model"] != model
            ):
                raise ValueError(
                    f"Firma o modello incompatibile: {cid}"
                )

            original[cid] = row

    if (
        selected_ids is not None
        and set(original) != selected_ids
    ):
        raise ValueError(
            "Gli ID del CSV non corrispondono "
            "a --claim-ids-file"
        )

    invalid = {
        cid
        for cid, row in original.items()
        if row["validation_errors"].strip()
    }

    print(
        f"Totale: {len(original)}; "
        f"da riprovare: {len(invalid)}",
        flush=True,
    )

    if not invalid:
        return

    source_digest = hashlib.sha256(
        target.read_bytes()
    ).hexdigest()

    backup = target.with_name(
        target.name
        + f".before_retry.{source_digest[:12]}.bak"
    )

    if not backup.exists():
        shutil.copy2(target, backup)

    if hashlib.sha256(
        backup.read_bytes()
    ).hexdigest() != source_digest:
        raise ValueError(
            "Backup incompatibile con il CSV originale"
        )

    staging = Path(
        str(target) + ".retrying.sqlite3"
    )

    db = sqlite3.connect(staging)

    replacements = {}

    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS results (
                claim_id TEXT PRIMARY KEY,
                row_json TEXT NOT NULL
            )
        """)

        metadata = dict(
            db.execute(
                "SELECT key, value FROM meta"
            ).fetchall()
        )

        expected_meta = {
            "source_digest": source_digest,
            "run_hash": signature,
            "model": model,
        }

        if not metadata:
            db.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                list(expected_meta.items()),
            )
            db.commit()
        elif metadata != expected_meta:
            raise ValueError(
                "Staging incompatibile con il CSV "
                "di origine"
            )

        previous = {
            cid: json.loads(payload)
            for cid, payload in db.execute(
                "SELECT claim_id, row_json FROM results"
            )
        }

        for cid, row in previous.items():
            if cid not in invalid:
                raise ValueError(
                    f"Staging con ID inatteso: {cid}"
                )

            if (
                row["claim_id"] != cid
                or row["claim_hash"]
                != original[cid]["claim_hash"]
                or row["run_hash"] != signature
                or row["model"] != model
            ):
                raise ValueError(
                    f"Staging incoerente: {cid}"
                )

        pending = [
            cid for cid in original
            if cid in invalid and cid not in previous
        ]

        if args.limit > 0:
            pending = pending[:args.limit]

        print(
            f"Già ritentati: {len(previous)}; "
            f"da elaborare ora: {len(pending)}",
            flush=True,
        )

        if pending:
            client = OpenAI(
                base_url=CFG.base_url,
                api_key=CFG.api_key,
                timeout=CFG.request_timeout,
                max_retries=0,
            )

            driver = open_driver()

            try:
                with driver.session(
                    database=CFG.neo4j_database
                ) as session:

                    for cid in tqdm(
                        pending,
                        desc="Retry secondary",
                        unit="claim",
                    ):
                        claim = fetch_one(
                            session,
                            cid,
                            args.context_chars,
                        )

                        if (
                            claim is None
                            or claim_hash(claim)
                            != original[cid]["claim_hash"]
                        ):
                            raise ValueError(
                                f"Claim modificato: {cid}"
                            )

                        if (
                            not claim.get("quote")
                            or claim.get("proof")
                            != claim.get("quote")
                        ):
                            judgement = {
                                "verdict": "insufficient_context",
                                "semantic_family": "other",
                                "temporal_status": "unknown",
                                "corrected_claim": None,
                                "reason": (
                                    "Evidence originale non verificabile"
                                ),
                            }
                            errors = [
                                "source_evidence_mismatch"
                            ]
                            attempts = 0
                        else:
                            fields = (
                                "title",
                                "publication_date",
                                "subject",
                                "subject_type",
                                "predicate",
                                "predicate_raw",
                                "object",
                                "object_type",
                                "modality",
                                "attribution",
                                "quote",
                                "context",
                            )

                            payload = {
                                key: claim.get(key)
                                for key in fields
                            }

                            payload["flags_for_priority_only"] = (
                                flags(claim)
                            )

                            judgement, errors, attempts = (
                                ask_judge(
                                    client,
                                    model,
                                    system_prompt,
                                    payload,
                                    args,
                                    claim,
                                )
                            )

                        result = make_row(
                            claim,
                            model,
                            signature,
                            judgement,
                            errors,
                            attempts,
                        )

                        if (
                            result["claim_id"] != cid
                            or result["claim_hash"]
                            != original[cid]["claim_hash"]
                        ):
                            raise ValueError(
                                f"Risultato incoerente: {cid}"
                            )

                        db.execute(
                            """
                            INSERT INTO results(
                                claim_id, row_json
                            )
                            VALUES (?, ?)
                            """,
                            (cid, dump(result)),
                        )

                        db.commit()

            finally:
                driver.close()

        replacements = {
            cid: json.loads(payload)
            for cid, payload in db.execute(
                "SELECT claim_id, row_json FROM results"
            )
        }

    finally:
        db.close()

    remaining = invalid - set(replacements)

    if remaining:
        print(
            f"Retry parziale: {len(remaining)} "
            "ancora da eseguire. "
            "CSV originale invariato.",
            flush=True,
        )
        return

    if set(replacements) != invalid:
        raise ValueError(
            "Copertura dei retry incoerente"
        )

    temporary = Path(
        str(target) + ".merged.tmp"
    )

    with temporary.open(
        "w", encoding="utf-8", newline=""
    ) as destination:
        writer = csv.DictWriter(
            destination,
            fieldnames=FIELDS,
        )

        writer.writeheader()

        for cid, old_row in original.items():
            row = replacements.get(cid, old_row)

            if (
                row["claim_id"] != cid
                or row["claim_hash"]
                != old_row["claim_hash"]
                or row["run_hash"] != signature
            ):
                raise ValueError(
                    f"Merge non valido: {cid}"
                )

            writer.writerow(row)

    with temporary.open(
        encoding="utf-8", newline=""
    ) as source:
        merged_rows = list(csv.DictReader(source))

    if (
        len(merged_rows) != len(original)
        or {
            row["claim_id"] for row in merged_rows
        } != set(original)
    ):
        raise ValueError(
            "Merge incompleto o con ID duplicati"
        )

    for row in merged_rows:
        cid = row["claim_id"]

        if cid not in invalid and row != original[cid]:
            raise ValueError(
                f"Riga valida alterata: {cid}"
            )

    remaining_errors = sum(
        bool(row["validation_errors"])
        for row in merged_rows
    )

    temporary.replace(target)
    staging.unlink(missing_ok=True)

    print(
        f"Retry completato: {len(invalid)}; "
        f"giudizi ancora invalidi: {remaining_errors}",
        flush=True,
    )
    print("Backup:", backup, flush=True)
    print("CSV aggiornato:", target, flush=True)



def store_validations(tx, records):
    tx.run("""
        UNWIND $rows AS row
        MATCH (c:Claim {id:row.claim_id})
        MERGE (v:ClaimValidation {id:row.id})
        ON CREATE SET
            v.claim_id=row.claim_id, v.claim_hash=row.claim_hash,
            v.run_hash=row.run_hash, v.model=row.model,
            v.judge_json=row.judge_json, v.verdict=row.verdict,
            v.semantic_family=row.family, v.temporal_status=row.temporal,
            v.reviewed_by=row.reviewed_by, v.reviewer_note=row.reviewer_note,
            v.approved_at=row.approved_at, v.validation_version=row.version
        MERGE (c)-[:HAS_VALIDATION]->(v)
    """, rows=records).consume()


def run_apply(args):
    with Path(args.csv).open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != FIELDS:
            raise ValueError("Schema CSV inatteso")
        rows = [r for r in reader if r["review"].strip().lower() in {"yes", "si", "true", "1"}]
    if not rows:
        print("Nessuna revisione approvata")
        return
    if not args.reviewer:
        raise ValueError("Obbligatorio --reviewer per applicare")
    driver = open_driver()
    checked = []
    seen = set()
    try:
        with driver.session(database=CFG.neo4j_database) as session:
            for row in rows:
                cid = row["claim_id"]
                if cid in seen:
                    raise ValueError("Claim duplicato: " + cid)
                seen.add(cid)
                if row["validation_errors"]:
                    raise ValueError("Review con errori di validazione: " + cid)
                c = fetch_one(session, cid, args.context_chars)
                if not c or row["claim_hash"] != claim_hash(c):
                    raise ValueError("Claim modificato o non trovato: " + cid)
                judgement = json.loads(row["judge_json"])
                if validate_judgement(judgement, c):
                    raise ValueError("Giudizio incoerente: " + cid)
                if (row["verdict"] != judgement.get("verdict")
                    or row["suggestion_json"] != dump(judgement.get("corrected_claim"))
                    or row["family"] != judgement.get("semantic_family")
                    or row["temporal"] != judgement.get("temporal_status")
                    or row["reason"] != str(judgement.get("reason") or "")[:600]):
                    raise ValueError("Campi CSV e JSON non corrispondono: " + cid)
                if judgement.get("verdict") == "insufficient_context":
                    raise ValueError("Non applicare insufficient_context: " + cid)
                checked.append({
                    "id": "cv-" + sha(cid + ":" + row["run_hash"])[:24],
                    "claim_id": cid, "claim_hash": row["claim_hash"],
                    "run_hash": row["run_hash"], "judge_json": row["judge_json"],
                    "verdict": row["verdict"], "family": row["family"],
                    "temporal": row["temporal"], "reviewed_by": args.reviewer,
                    "reviewer_note": row.get("reviewer_note") or "", "model": row["model"],
                    "approved_at": datetime.now(timezone.utc).isoformat(), "version": VERSION,
                })
            print(f"Revisioni approvate e verificate: {len(checked)}")
            if not args.commit:
                print("DRY RUN: per scrivere nodi ClaimValidation aggiungi --commit")
                return
            session.run("CREATE CONSTRAINT claim_validation_id IF NOT EXISTS FOR (v:ClaimValidation) REQUIRE v.id IS UNIQUE").consume()
            session.execute_write(store_validations, checked)
            print(f"Scritte revisioni: {len(checked)}. Nessun Claim o FACT originale modificato.")
    finally:
        driver.close()


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--out", default="output/semantic_validation/review_v1.csv")
    g.add_argument("--model", default=CFG.eval_model_id)
    g.add_argument("--predicates", nargs="*", default=[])
    g.add_argument("--only-flagged", action="store_true")
    g.add_argument("--limit", type=int, default=100)
    g.add_argument("--batch", type=int, default=250)
    g.add_argument("--context-chars", type=int, default=600)
    g.add_argument("--retries", type=int, default=1)
    g.add_argument("--max-tokens", type=int, default=1400)
    g.add_argument("--resume", action="store_true")
    g.add_argument("--retry-invalid", action="store_true",
                    help="Riprova soltanto i giudizi con validation_errors")
    g.add_argument("--claim-ids-file", default=None,
                    help="CSV con colonna claim_id per ripetere lo stesso benchmark")
    a = sub.add_parser("apply")
    a.add_argument("--csv", required=True)
    a.add_argument("--reviewer", default=None)
    a.add_argument("--context-chars", type=int, default=600)
    a.add_argument("--commit", action="store_true")
    args = p.parse_args()
    
    if args.command == "generate" and args.retry_invalid and not args.resume:
        p.error("--retry-invalid richiede --resume")
    
    if args.command == "generate":
        if args.batch < 1 or args.limit < 0 or args.retries < 0:
            p.error("--batch >=1, --limit >=0 e --retries >=0 richiesti")
        run_generate(args)
    else:
        run_apply(args)


if __name__ == "__main__":
    main()
