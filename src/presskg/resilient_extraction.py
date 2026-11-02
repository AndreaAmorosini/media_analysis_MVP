"""Estrazione news-KG con evidence span, ontologia estesa e cache per finestra."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import logging
import re
import os
import uuid
from urllib.request import Request, urlopen

from openai import OpenAI
from pydantic import BaseModel, ValidationError, model_validator
from json_repair import loads as repair_loads


from .articles import Article, short_hash
from .entity_ontology import (
    TYPES, VERSION as ENTITY_ONTOLOGY_VERSION,
    normalize_type, prompt_catalog, type_family,
)
from .ontology import canonical_predicate, VERSION as PREDICATE_ONTOLOGY_VERSION
from .extraction import windows
from .settings import CFG
from .predicate_catalog import (
    canonical_news_predicate,
    VERSION as NEWS_PREDICATE_VERSION,
    preferred_predicates,
    needs_predicate_review,
)

from .extraction_rules import (
    EXTRACTION_GUARDRAILS,
    VERSION as EXTRACTION_RULES_VERSION,
)

VERSION = "span-grounding-v4-news-roles"
MODES = {"asserted", "reported", "alleged", "denied", "hypothetical", "opinion", "uncertain"}

PROMPT = f"""Sei un estrattore di relazioni da articoli giornalistici italiani.
Rispondi SOLO con un JSON valido: {{"claims": [...]}}.
Per ogni claim produci:
- subject: {{"name": string, "type": tipo canonico, "subtype": facoltativo}}
- predicate: relazione ORIENTATA, in MAIUSCOLO_CON_UNDERSCORE
- object: {{"name": string, "type": tipo canonico, "subtype": facoltativo}}
- evidence_id: un ID S001, S002, ... realmente presente nell'input
- modality: asserted|reported|alleged|denied|hypothetical|opinion|uncertain
Facoltativi: attribution, attribution_role, subject_role, context_evidence_id.
Solo per Event: event_type, event_date, location, se deducibili dal testo.

ONTOLOGIA DELLE ENTITÀ:
{prompt_catalog()}

REGOLE:
1. Usa soltanto tipi canonici del catalogo. Non inventare categorie.
2. Non creare ID per le entità e NON copiare citazioni: Python ricostruisce
   gli offset dal segmento evidence_id.
3. evidence_id identifica il segmento contenente la relazione principale.
   Se un referente è identificato soltanto in un altro segmento della stessa finestra, utilizza anche context_evidence_id.
   context_evidence_id deve identificare un segmento realmente presente nell'input.
   Il contesto serve esclusivamente a chiarire soggetti, coreferenze e attribuzioni: non può giustificare relazioni non espresse dall'evidenza.
4. Conserva il verso subject -> predicate -> object.
5. Non dedurre responsabilità dalla semplice menzione di un'azienda.
6. Accuse, ipotesi, smentite e dichiarazioni non sono fatti accertati:
   usa correttamente modality e attribution.
7. Event = episodio concreto; Topic = argomento astratto.
8. Facility = singolo impianto; Infrastructure = rete o sistema.
9. Service = servizio erogato; Project = iniziativa pianificata.
10. GPE = territorio politico-amministrativo; PublicAuthority = ente pubblico.
11. MediaOutlet = testata; MediaWork = contenuto giornalistico specifico.
12. Non inventare nomi, eventi, esempi o valori segnaposto.
13. Non assegnare sentiment o punteggi reputazionali.
14. Non duplicare claim; se non ne trovi, restituisci {{"claims": []}}.

PREDICATI PREFERITI:
CAUSES, RESTORES, MANAGES, AFFECTS, INVESTS_IN, PREVENTS, MITIGATES,
CRITICIZES, REPORTS, ANNOUNCES, DENIES, REQUESTS, FUNDS, ORGANIZES,
COORDINATES, COLLABORATES_WITH, CONCERNS, PROVIDES, OPERATES_IN,
SUPPORTS, OPPOSES, DENOUNCES, FILES_COMPLAINT, IMPLEMENTS,
EXECUTES, ACCUSES, PROMISES, ISSUES,
REVOKES, LAUNCHES.
Puoi utilizzare altri predicati descrittivi, che verranno conservati come predicate_raw e normalizzati da Python.

Non confondere il soggetto finanziatore
con il soggetto attuatore.

DENIES indica negare o smentire.
DENOUNCES indica denunciare pubblicamente.
FILES_COMPLAINT indica presentare una
denuncia o un esposto formale.

"Gestisce il servizio in 75 comuni"
non significa "gestisce 75 comuni".

Un beneficio previsto o annunciato non
equivale a un risultato già realizzato.

APPROVES, AUTHORIZES, FUNDS, IMPLEMENTS,
EXECUTES e MANAGES rappresentano ruoli diversi.

Un ente che approva un finanziamento non
è automaticamente il soggetto finanziatore.

Un soggetto attuatore non è automaticamente
il soggetto che esegue materialmente i lavori.

Per utilizzare FUNDS, l'evidenza deve
identificare il finanziatore e l'oggetto
finanziato.

Non considerare sufficiente una generica
associazione fra ente, progetto e importo.
"""

PROMPT += "\n\n" + EXTRACTION_GUARDRAILS


PROMPT += """

FORMATO JSON - REGOLE OBBLIGATORIE

Ogni elemento dell'array claims è una relazione
completa, con subject, predicate, object,
evidence_id e modality ALLO STESSO LIVELLO.

Esempio valido:

{
  "claims": [
    {
      "subject": {
        "name": "Organizzazione Alfa",
        "type": "Organization"
      },
      "predicate": "ANNOUNCES",
      "object": {
        "name": "Progetto Beta",
        "type": "Project"
      },
      "evidence_id": "S001",
      "modality": "reported",
      "attribution": null
    }
  ]
}

NON inserire predicate, object, evidence_id
o modality all'interno di subject.

subject e object devono sempre essere
oggetti contenenti name e type.

Il campo attribution deve essere una
stringa con il nome della fonte o null.

Se necessario, il ruolo della fonte può
essere riportato nel campo separato
attribution_role.

Il campo modality ammette soltanto:
asserted, reported, alleged, denied,
hypothetical, opinion, uncertain.

"requested" non è una modalità:
rappresenta una richiesta tramite il predicato
REQUESTS oppure una relazione più specifica.

Non creare oggetti con name o type null.
Non inventare un oggetto per verbi intransitivi
come DIED.

Se una relazione non può essere espressa
in modo completo e verificabile, omettila.

Non usare nomi di famiglie ontologiche
come valori di type.
"""

# PROMPT += (
#     "\n\nVOCABOLARIO AGGIUNTIVO DI PREDICATI:\n"
#     + ", ".join(preferred_predicates())
#     + "\n\n"
#     + "Scegli il predicato che rappresenta "
#       "esattamente la relazione nel testo. "
#       "Non scegliere un predicato soltanto "
#       "perché compare nel vocabolario."
# )


def normalize_modality(value):
    if not isinstance(value, str):
        return value
    normalized = value.strip().casefold().replace("-", "_")
    aliases = {
        "asserito": "asserted", "affermato": "asserted",
        "riportato": "reported", "citato": "reported",
        "presunto": "alleged", "accusato": "alleged",
        "negato": "denied", "smentito": "denied",
        "ipotetico": "hypothetical", "ipotizzato": "hypothetical",
        "opinione": "opinion", "incerto": "uncertain",
        "requested": "reported", "request": "reported",
        "demanded": "reported",
    }
    return aliases.get(normalized, normalized)


@dataclass(frozen=True)
class Span:
    evidence_id: str
    start: int
    end: int
    text: str


def make_spans(snippet: str, target_chars: int = 350) -> list[Span]:
    """Crea segmenti originali, senza riscrivere né correggere il testo."""
    if target_chars < 60:
        raise ValueError("target_chars deve essere >= 60")
    spans = []
    pos = 0
    while pos < len(snippet):
        while pos < len(snippet) and snippet[pos].isspace():
            pos += 1
        if pos == len(snippet):
            break
        end = min(pos + target_chars, len(snippet))
        if end < len(snippet):
            lower = pos + target_chars // 2
            candidates = [
                snippet.rfind(sep, lower, end) + len(sep)
                for sep in ("\n", ". ", "; ", ", ")
                if snippet.rfind(sep, lower, end) >= lower
            ]
            if candidates:
                end = max(candidates)
            else:
                space = snippet.rfind(" ", lower, end)
                if space > lower:
                    end = space
        if end <= pos:
            end = min(pos + target_chars, len(snippet))
        true_end = pos + len(snippet[pos:end].rstrip())
        if true_end > pos:
            spans.append(Span(
                f"S{len(spans) + 1:03d}", pos, true_end,
                snippet[pos:true_end],
            ))
        pos = end
    return spans


def parse_evidence_id(raw: str | int) -> str:
    match = re.fullmatch(r"(?:S)?0*(\d+)", str(raw).strip(), re.I)
    return f"S{int(match.group(1)):03d}" if match else "INVALID"


class InlineEntity(BaseModel):
    name: str
    type: str
    subtype: str | None = None
    type_raw: str | None = None
    event_type: str | None = None
    event_date: str | None = None
    location: str | None = None

    @model_validator(mode="before")
    @classmethod
    def adapt_entity(cls, raw):
        if not isinstance(raw, dict):
            return raw
        data = dict(raw)
        raw_type = data.get("type")
        
        if not isinstance(data.get("type"), str):
            for alternative in ("entity_type", "category", "kind"):
                value = data.get(alternative)

                if isinstance(value, str):
                    data["type"] = value
                    break

        if isinstance(data.get("type"), str):
            data["type_raw"] = (raw_type if isinstance(raw_type, str) else None)
            data["type"] = normalize_type(data["type"])
            
        return data

class InlineClaim(BaseModel):
    subject: InlineEntity
    predicate: str
    object: InlineEntity
    evidence_id: str | int
    context_evidence_id: str | int | None = None
    modality: str = "uncertain"
    attribution: str | None = None
    attribution_role: str | None = None
    subject_role: str | None = None



    @model_validator(mode="before")
    @classmethod
    def adapt_claim(cls, raw):
        if not isinstance(raw, dict):
            return raw

        data = dict(raw)

        # Caso: l'intera relazione è stata
        # erroneamente annidata dentro subject.
        nested = data.get("subject")

        if (isinstance(nested, dict) and "name" in nested and "predicate" in nested and "object" in nested and 
            ("predicate" not in data or "object" not in data)):
            for key in (
                "predicate",
                "object",
                "evidence_id",
                "context_evidence_id",
                "modality",
                "attribution",
            ):
                if (key not in data and key in nested):
                    data[key] = nested[key]

            data["subject"] = {
                key: value
                for key, value in nested.items()
                if key in {
                    "name",
                    "type",
                    "subtype",
                    "event_type",
                    "event_date",
                    "location",
                }
            }

        # Recupero delle entità solo quando
        # il tipo è già presente o verificabile.
        for role in ("subject", "object"):
            entity = data.get(role)

            flat_name = data.get(f"{role}_name")
            flat_type = data.get(f"{role}_type")

            if isinstance(entity, str):
                entity_type = flat_type if isinstance(flat_type, str) else None
                
                if entity_type:
                    data[role] = {
                        "name": entity,
                        "type": entity_type,
                    }

            elif isinstance(entity, dict):
                normalized = dict(entity)

                if (not isinstance(normalized.get("name"), str) and isinstance(flat_name, str)):
                    normalized["name"] = flat_name

                if (not isinstance(normalized.get("type"), str) and isinstance(flat_type, str)):
                    normalized["type"] = flat_type

                data[role] = normalized

            elif isinstance(flat_name, str):
                entity_type = flat_type if isinstance(flat_type, str) else None

                if entity_type:
                    data[role] = {
                        "name": flat_name,
                        "type": entity_type,
                    }

        # Alias strutturali del predicato.
        if not isinstance(data.get("predicate"), str):
            relation = data.get("relation")

            if isinstance(relation, str):
                data["predicate"] = relation

        # Recupera solo evidence ID espliciti,
        # senza inventare evidenze.
        if "evidence_id" not in data:
            for key in (
                "segment_id",
                "span_id",
                "evidence_ref",
                "evidence_span_id",
            ):
                value = data.get(key)

                if isinstance(value, (str, int)):
                    data["evidence_id"] = value
                    break

        # Attribution può arrivare come
        # oggetto invece di una stringa.
        attribution = data.get("attribution")

        if isinstance(attribution, dict):
            role = attribution.get("role")

            if isinstance(role, str):
                data["attribution_role"] = (role.strip() or None)

            for key in (
                "source",
                "speaker",
                "name",
            ):
                value = attribution.get(key)

                if (isinstance(value, str) and value.strip()):
                    data["attribution"] = value.strip()
                    break
        elif isinstance(attribution, list):
            if all(isinstance(item, str) for item in attribution):
                data["attribution"] = (
                    "; ".join(item.strip() for item in attribution if item.strip()) or None
                )
        elif isinstance(attribution, str):
            if attribution.strip().casefold() in {
                "",
                "null",
                "none",
            }:
                data["attribution"] = None

        data["modality"] = normalize_modality(data.get("modality", "uncertain"))

        return data


class TruncatedResponse(Exception):
    pass


class UnusableExtraction(Exception):
    pass


def parse_response(raw: str, issues: list | None = None):
    """Ritorna candidati validi, motivi di rifiuto, numero claim generati."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    if start < 0:
        raise ValueError("nessun_oggetto_json")
    text = text[start:]
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = repair_loads(text)
    if not isinstance(decoded, dict) or not isinstance(decoded.get("claims"), list):
        raise ValueError("claims_non_e_lista")

    valid, reasons = [], Counter()
    for index, item in enumerate(decoded["claims"]):
        try:
            claim = InlineClaim.model_validate(item)
        except ValidationError as exc:
            reasons["claim_schema"] += 1
            if issues is not None:
                issues.append({
                    "claim_index": index, "reason": "claim_schema",
                    "errors": [{"loc": list(e["loc"]), "type": e["type"], "msg": e["msg"]}
                               for e in exc.errors()], "claim": item,
                })
            continue
        invalid = [
            {"role": role, "raw_type": ent.type_raw, "normalized_type": ent.type}
            for role, ent in (("subject", claim.subject), ("object", claim.object))
            if ent.type not in TYPES
        ]
        if invalid:
            reason, detail = "invalid_type", {"details": invalid}
        elif claim.modality not in MODES:
            reason, detail = "invalid_modality", {"value": claim.modality}
        elif not claim.subject.name.strip() or not claim.object.name.strip():
            reason, detail = "empty_entity_name", {}
        elif not claim.predicate.strip():
            reason, detail = "empty_predicate", {}
        else:
            valid.append(claim)
            continue
        reasons[reason] += 1
        if issues is not None:
            issues.append({"claim_index": index, "reason": reason, **detail, "claim": item})
    return valid, reasons, len(decoded["claims"])


def ground(article: Article, offset: int, snippet: str,
           candidates: list[InlineClaim], spans: list[Span], issues: list | None = None):
    """Ricostruisce citazioni e offset esclusivamente dal body originale."""
    lookup = {span.evidence_id: span for span in spans}
    entities, claims, reasons = {}, {}, Counter()
    for index, candidate in enumerate(candidates):
        evidence_id = parse_evidence_id(candidate.evidence_id)
        span = lookup.get(evidence_id)
        if span is None:
            reason = "invalid_evidence_id"
        else:
            start, end = offset + span.start, offset + span.end
            quote = article.body[start:end]
            reason = "invalid_span" if quote != span.text or len(quote.strip()) < 12 else None
        if reason is not None:
            reasons[reason] += 1
            if issues is not None:
                issues.append({
                    "candidate_index": index, "reason": reason,
                    "evidence_id": str(candidate.evidence_id),
                })
            continue
        
        
        # Evidenza aggiuntiva per risolvere
        # eventuali riferimenti impliciti.
        context_span = None

        if candidate.context_evidence_id is not None:
            context_id = parse_evidence_id(
                candidate.context_evidence_id
            )

            context_span = lookup.get(context_id)

            if context_span is None:
                reasons["invalid_context_evidence_id"] += 1

                if issues is not None:
                    issues.append({
                        "candidate_index": index,
                        "reason": "invalid_context_evidence_id",
                        "evidence_id": str(
                            candidate.context_evidence_id
                        ),
                    })

                continue

            # Anche il contesto deve appartenere
            # al body originale, senza alterazioni.
            context_start = offset + context_span.start
            context_end = offset + context_span.end

            if (article.body[context_start:context_end] != context_span.text):
                reasons["invalid_context_span"] += 1
                continue

        
        local_refs = []
        for ent in (candidate.subject, candidate.object):
            key = (ent.type, ent.name.strip().casefold())
            if key not in entities:
                local_id = f"e{len(entities) + 1}"
                entity_id = (
                    "event-" + short_hash(f"{article.id}:{offset}:{local_id}")
                    if ent.type == "Event" else ""
                )
                entities[key] = {
                    "local_id": local_id, "id": entity_id,
                    "name": ent.name.strip(), "type": ent.type,
                    "type_raw": ent.type_raw,
                    "type_group": type_family(ent.type), "subtype": ent.subtype,
                    "event_type": ent.event_type, "event_date": ent.event_date,
                    "location": ent.location,
                }
            local_refs.append(entities[key]["local_id"])
            
        raw_predicate = re.sub(r"[\s-]+", "_", candidate.predicate.strip().upper())
        predicate, family = canonical_predicate(raw_predicate)
        if predicate == "OTHER":
            predicate, family = canonical_news_predicate(raw_predicate)
            
        
        claim_id = "claim-" + short_hash(json.dumps([
            article.id, start, end,
            candidate.subject.type, candidate.subject.name.strip().casefold(),
            raw_predicate,
            candidate.object.type, candidate.object.name.strip().casefold(),
            candidate.modality, candidate.attribution,
        ], ensure_ascii=False))
        
        
        # Non blocchiamo la persistenza:
        # segnaliamo claim potenzialmente rischiosi.
        attribution_required = (
            candidate.modality in {
                "alleged",
                "denied",
                "opinion",
            }
            or raw_predicate in {
                "CRITICIZES",
                "DENIES",
                "DECLARES",
                "REPORTS",
                "ANNOUNCES",
                "OPPOSES",
            }
        )

        needs_attribution_review = (attribution_required and not (candidate.attribution and candidate.attribution.strip()))

        needs_semantic_review = (needs_predicate_review(raw_predicate) or needs_attribution_review)

        
        claims[claim_id] = {
            "id": claim_id, "subject_local": local_refs[0],
            "object_local": local_refs[1], "predicate": predicate,
            "predicate_raw": raw_predicate, "predicate_family": family,
            "quote": quote, "body_start": start, "body_end": end,
            "modality": candidate.modality, "attribution": candidate.attribution,
            "subject_role": candidate.subject_role,
            "attribution_role": candidate.attribution_role,
            "context_evidence_id": (context_span.evidence_id if context_span is not None else None),
            "context_quote": (article.body[(offset + context_span.start):(offset + context_span.end)] if context_span is not None else None),
            "context_body_start": (offset + context_span.start if context_span is not None else None),
            "context_body_end": (offset + context_span.end if context_span is not None else None),
            "needs_attribution_review": (needs_attribution_review),
            "needs_semantic_review": (needs_semantic_review),
        }
    return list(entities.values()), list(claims.values()), reasons


class Extractor:
    def __init__(self, use_window_cache: bool = True):
        self.model_id = CFG.model_id
        if not self.model_id or self.model_id == "INSERISCI_ID_REALE_DEL_MODELLO":
            raise ValueError("Impostare UNSLOTH_MODEL")
        self.client = OpenAI(base_url=CFG.base_url, api_key=CFG.api_key, timeout=CFG.request_timeout, max_retries=0)
        self.use_window_cache = use_window_cache
        self.active_cancel_id = None
        params = {
            "model": self.model_id, "version": VERSION,
            "entity_ontology": ENTITY_ONTOLOGY_VERSION,
            "predicate_ontology": PREDICATE_ONTOLOGY_VERSION,
            "news_predicate_ontology": NEWS_PREDICATE_VERSION,
            "semantic_rules": EXTRACTION_RULES_VERSION,
            "prompt_sha256": hashlib.sha256(PROMPT.encode("utf-8")).hexdigest(),
            "window_chars": CFG.window_chars, "window_overlap": CFG.window_overlap,
            "max_output_tokens": CFG.max_output_tokens, "json_mode": CFG.json_mode,
        }
        self.key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:20]

    def _cache_path(self, article, offset, snippet) -> Path:
        h = hashlib.sha256(f"{offset}:{snippet}".encode()).hexdigest()[:20]
        return (Path(CFG.output_dir) / "_windows" / article.id /
                article.content_hash[:16] / self.key / f"{h}.json")

    @staticmethod
    def _write_json(path: Path, payload: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def _diagnostic(self, article, offset, length, attempt, raw, error):
        path = (Path(CFG.output_dir) / "_diagnostics" / article.id /
                f"{offset}_{length}_try{attempt + 1}.json")
        self._write_json(path, {"error": error, "raw_response": raw})

    def _rejection_diagnostic(self, article, offset, length, attempt, issues, reasons):
        path = (Path(CFG.output_dir) / "_diagnostics" / article.id /
                f"{offset}_{length}_try{attempt + 1}_rejections.json")
        invalid_types = Counter(
            str(detail.get("raw_type"))
            for issue in issues if issue.get("reason") == "invalid_type"
            for detail in issue.get("details", [])
        )
        self._write_json(path, {
            "article_id": article.id, "article_path": article.rel_path,
            "offset": offset, "window_length": length,
            "version": VERSION, "entity_ontology": ENTITY_ONTOLOGY_VERSION,
            "reasons": dict(reasons), "invalid_raw_types": dict(invalid_types),
            "issues": issues[:100], "total_issues": len(issues),
        })
        
    def cancel_active(self):
        if not self.active_cancel_id:
            return

        base = CFG.base_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]

        url = f"{base}/api/inference/cancel"

        payload = json.dumps({
            "cancel_id": self.active_cancel_id,
        }).encode("utf-8")

        request = Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {CFG.api_key}",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=3) as response:
                print(
                    "Unsloth cancellation:",
                    response.status,
                    response.read().decode("utf-8"),
                    flush=True,
                )
        except Exception as exc:
            print(
                f"Impossibile annullare la generazione: {exc}",
                flush=True,
            )

    def _call(self, article, offset, snippet):
        spans = make_spans(snippet)
        lines = "\n".join(f"[{span.evidence_id}] {span.text}" for span in spans)
        request = "SEGMENTI ORIGINALI DELL'ARTICOLO:\n" + lines
        last_error = ""
        for attempt in range(2):
            raw = ""
            try:
                kwargs = ({"response_format": {"type": "json_object"}}
                          if CFG.json_mode else {})
                prompt = request
                if attempt:
                    prompt += (
                        "\n\nCORREGGI il tentativo precedente: " + last_error[:140] +
                        ". Usa subject e object inline con tipi canonici, "
                        "evidence_id esistenti e nessuna relazione inventata. "
                        "Se non ci sono claim verificabili, rispondi con claims vuoti."
                    )
                self.active_cancel_id = uuid.uuid4().hex

                try:
                    response = self.client.chat.completions.create(
                        model=self.model_id,
                        temperature=0,
                        max_tokens=CFG.max_output_tokens,
                        messages=[
                            {"role": "system", "content": PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        extra_body={
                            "enable_thinking": False,
                            "cancel_id": self.active_cancel_id,
                        },
                        **kwargs,
                    )
                finally:
                    self.active_cancel_id = None

                choice = response.choices[0]
                raw = choice.message.content or ""
                if choice.finish_reason == "length":
                    raise TruncatedResponse("output_troncato")
                issues = []
                candidates, reasons, generated = parse_response(raw, issues)
                entities, claims, ground_reasons = ground(
                    article, offset, snippet, candidates, spans, issues,
                )
                reasons.update(ground_reasons)
                if reasons:
                    self._rejection_diagnostic(
                        article, offset, len(snippet), attempt, issues, reasons,
                    )
                    logging.warning(
                        "%s offset=%d: claim scartati=%s",
                        article.rel_path, offset, dict(reasons),
                    )
                if generated > 0 and not claims:
                    raise UnusableExtraction(f"zero_claim_verificabili: {dict(reasons)}")
                return {
                    "entities": entities, "claims": claims,
                    "rejection_reasons": dict(reasons),
                    "schema_errors": reasons["claim_schema"],
                    "raw_claim_count": generated,
                }
            except TruncatedResponse as exc:
                self._diagnostic(article, offset, len(snippet), attempt, raw, str(exc))
                raise
            except (ValueError, UnusableExtraction) as exc:
                last_error = str(exc)
                self._diagnostic(article, offset, len(snippet), attempt, raw, last_error)
        raise UnusableExtraction(last_error)

    def _piece(self, article, offset, snippet, depth=0):
        path = self._cache_path(article, offset, snippet)
        if self.use_window_cache and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if (isinstance(data, dict) and isinstance(data.get("entities"), list)
                        and isinstance(data.get("claims"), list)):
                    return [(offset, snippet, data)]
            except (OSError, ValueError):
                pass
            logging.warning("Cache finestra ignorata: %s", path)
        try:
            data = self._call(article, offset, snippet)
        except TruncatedResponse as exc:
            if depth >= 4 or len(snippet) < 900:
                raise UnusableExtraction(
                    f"offset={offset}, chars={len(snippet)}: output ancora troncato"
                ) from exc
            middle = len(snippet) // 2
            overlap = min(100, len(snippet) // 12)
            left_end, right_start = middle + overlap, middle - overlap
            logging.warning(
                "Split per output troncato %s offset=%d depth=%d",
                article.rel_path, offset, depth,
            )
            return (
                self._piece(article, offset, snippet[:left_end], depth + 1) +
                self._piece(article, offset + right_start,
                            snippet[right_start:], depth + 1)
            )
        if self.use_window_cache:
            self._write_json(path, data)
        return [(offset, snippet, data)]

    def extract(self, article):
        pieces = []
        for offset, snippet in windows(article.body, CFG.window_chars, CFG.window_overlap):
            pieces.extend(self._piece(article, offset, snippet))
        chunks, seen_ids, rejected = [], set(), Counter()
        schema_errors = 0
        for index, (offset, snippet, data) in enumerate(pieces):
            fresh = []
            for claim in data["claims"]:
                if claim["id"] in seen_ids:
                    rejected["duplicate_overlap"] += 1
                else:
                    fresh.append(claim)
                    seen_ids.add(claim["id"])
            used = {local for claim in fresh
                    for local in (claim["subject_local"], claim["object_local"])}
            entities = [ent for ent in data["entities"] if ent["local_id"] in used]
            rejected.update(data.get("rejection_reasons", {}))
            schema_errors += data.get("schema_errors", 0)
            chunks.append({
                "index": index, "start": offset, "end": offset + len(snippet),
                "text": snippet, "entities": entities, "claims": fresh,
                "rejection_reasons": data.get("rejection_reasons", {}),
                "schema_errors": [],
            })
        return {
            "article_id": article.id, "content_hash": article.content_hash,
            "model_id": self.model_id, "extract_version": VERSION,
            "chunks": chunks, "rejected_claims": sum(rejected.values()),
            "rejection_reasons": dict(rejected), "schema_errors": schema_errors,
        }
