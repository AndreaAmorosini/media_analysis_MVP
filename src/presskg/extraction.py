"""Estrazione di triple orientate con provenance."""

from __future__ import annotations

import json
import re

from typing import Literal
from typing import get_args
from pydantic import model_validator

from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

from .articles import Article, short_hash
from .settings import CFG


VERSION = "presskg-schema-v1"


KINDS = Literal[
    "Company",
    "Person",
    "PublicAuthority",
    "Infrastructure",
    "Location",
    "Topic",
    "Event",
    "Other",
]


PREDICATES = Literal[
    "CAUSES",
    "RESTORES",
    "MANAGES",
    "INVESTS_IN",
    "AFFECTS",
    "CRITICIZES",
    "REPORTS",
    "PREVENTS",
    "MITIGATES",
    "PARTICIPATES_IN",
    "CONCERNS",
    "OPERATES_IN",
    "ANNOUNCES",
    "DENIES",
    "SUPPORTS",
    "OPPOSES",
    "COLLABORATES_WITH",
    "CONTAINS",
    "PROVIDES",
    "FUNDS",
    "LOCATED_IN",
    "ACCEPTS",
    "OPERATES",
    "IMPROVES",
    "OTHER",
]


MODES = Literal[
    "asserted",
    "reported",
    "alleged",
    "denied",
    "hypothetical",
    "opinion",
    "uncertain",
]


class ExtractedEntity(BaseModel):
    local_id: str
    name: str
    type: KINDS

    event_type: str | None = None
    event_date: str | None = None
    location: str | None = None



class ExtractedClaim(BaseModel):
    subject_id: str | None = None
    predicate: PREDICATES = "OTHER"
    object_id: str | None = None

    quote: str | None = None
    modality: MODES = "uncertain"

    attribution: str | None = None
    subject_role: str | None = None

    predicate_raw: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_predicate(cls, value):
        if not isinstance(value, dict):
            return value

        data = dict(value)

        original = str(
            data.get("predicate") or "OTHER"
        ).strip().upper().replace(" ", "_")

        allowed = set(get_args(PREDICATES))

        data["predicate_raw"] = original

        if original in allowed:
            data["predicate"] = original
        else:
            data["predicate"] = "OTHER"

        return data


class ExtractedGraph(BaseModel):
    entities: list[ExtractedEntity] = Field(default_factory=list)
    claims: list[ExtractedClaim] = Field(default_factory=list)


PROMPT = """
Sei un estrattore di Knowledge Graph
da articoli giornalistici italiani.

Restituisci SOLO JSON secondo questo schema,
SENZA markdown:

{
  "entities": [
    {
      "local_id": "e1",
      "name": "GORI",
      "type": "Company",
      "event_type": null,
      "event_date": null,
      "location": null
    },
    {
      "local_id": "e2",
      "name": "guasto a X il ...",
      "type": "Event",
      "event_type": "ServiceDisruption",
      "event_date": null,
      "location": "X"
    }
  ],
  "claims": [
    {
      "subject_id": "e1",
      "predicate": "PARTICIPATES_IN",
      "object_id": "e2",
      "quote": "citazione IDENTICA al body",
      "modality": "reported",
      "attribution": null,
      "subject_role": "operator"
    }
  ]
}

Tipi di entità:
Company, Person, PublicAuthority, Infrastructure,
Location, Topic, Event, Other.

Predicati ammessi:
CAUSES, RESTORES, MANAGES, INVESTS_IN, AFFECTS,
CRITICIZES, REPORTS, PREVENTS, MITIGATES,
PARTICIPATES_IN, CONCERNS, OPERATES_IN,
ANNOUNCES, DENIES, SUPPORTS, OPPOSES, COLLABORATES_WITH,
CONTAINS, PROVIDES, FUNDS, LOCATED_IN, ACCEPTS, OPERATES,
IMPROVES, OTHER.

Modalità ammesse:
asserted, reported, alleged, denied,
hypothetical, opinion, uncertain.

REGOLE:

1. I claim sono TRIPLE ORIENTATE:
   subject -> predicate -> object.

2. Crea una entity Event distinta per ogni
   evento concreto.

3. Un Event indica una MENZIONE nell'articolo,
   non un evento globale già deduplicato.

4. I local_id valgono soltanto nella finestra.

5. Quote deve essere una sottostringa CONTIGUA
   e VERBATIM del BODY. Non usare il titolo
   come quote.

6. Conserva l'attribuzione delle dichiarazioni.

7. Non trasformare accuse in fatti accertati.

8. La semplice menzione di un'azienda
   non implica alcuna responsabilità.

9. Usa predicati e tipi soltanto quando
   il testo li supporta.

10. Evita entità senza un nome distinguibile.

Se non emergono claim supportati, restituisci:

{"entities": [], "claims": []}
"""


def windows(body: str, size: int, overlap: int):
    if size < 1000 or not 0 <= overlap < size // 2:
        raise ValueError("Parametri chunk non validi")

    start = 0

    while start < len(body):
        end = min(len(body), start + size)

        if end < len(body):
            boundary = max(body.rfind(sep, start + int(size * 0.7), end) for sep in ("\n\n", ". ", "\n"))

            if boundary > start + int(size * 0.7):
                end = boundary + 1

        yield start, body[start:end]

        if end == len(body):
            break

        start = end - overlap



def parse_json_object(raw: str) -> dict:
    from json_repair import loads as repair_loads

    raw = raw.strip()

    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)

    raw = re.sub(r"\s*```$", "", raw)

    start = raw.find("{")

    if start < 0:
        raise ValueError("Output privo di JSON")

    raw = raw[start:]

    try:
        obj, _ = json.JSONDecoder().raw_decode(raw)

    except json.JSONDecodeError:
        obj = repair_loads(raw)

    if not isinstance(obj, dict):
        raise ValueError("La risposta non contiene un oggetto JSON")

    return obj


def validate_partial_graph(data: dict):
    if not isinstance(data, dict):
        raise ValueError("Output non valido")

    raw_entities = data.get("entities")
    raw_claims = data.get("claims")

    if not isinstance(raw_entities, list):
        raise ValueError("entities non è una lista")

    if not isinstance(raw_claims, list):
        raise ValueError("claims non è una lista")

    entities = []
    claims = []
    errors = []

    for i, raw in enumerate(raw_entities):
        try:
            entity = ExtractedEntity.model_validate(raw)
            entities.append(entity)

        except ValidationError as exc:
            errors.append(
                f"entity[{i}]: {exc.error_count()} "
                "errori di schema"
            )

    for i, raw in enumerate(raw_claims):
        try:
            claim = ExtractedClaim.model_validate(raw)
            claims.append(claim)

        except ValidationError as exc:
            errors.append(
                f"claim[{i}]: {exc.error_count()} "
                "errori di schema"
            )

    graph = ExtractedGraph(
        entities=entities,
        claims=claims,
    )

    return graph, errors



def grounded_records(article: Article, offset: int, snippet: str, output: ExtractedGraph, chunk_index: int,):
    by_local = {entity.local_id: entity for entity in output.entities if entity.local_id and entity.name.strip()}

    entities = {}
    claims = {}
    rejected = 0

    for claim in output.claims[:60]:
        subject = by_local.get(claim.subject_id)

        obj = by_local.get(claim.object_id)

        quote = (claim.quote or "").strip()

        if (not subject or not obj or not quote or len(quote) < 12 or snippet.count(quote) != 1):
            rejected += 1
            continue

        pos = offset + snippet.find(quote)

        if (article.body[pos:pos + len(quote)] != quote):
            rejected += 1
            continue

        for entity in (subject, obj):
            # Gli Event sono locali all'articolo.
            if entity.type == "Event":
                entity_id = ("event-" + short_hash(f"{article.id}:{chunk_index}:{entity.local_id}"))
            else:
                # L'ID globale verrà assegnato
                # dal modulo Neo4j.
                entity_id = ""

            entities[entity.local_id] = {
                "local_id": entity.local_id,
                "id": entity_id,
                "name": entity.name.strip(),
                "type": entity.type,
                "event_type": entity.event_type,
                "event_date": entity.event_date,
                "location": entity.location,
            }

        claim_id = ("claim-" + short_hash(json.dumps([article.id, pos, claim.subject_id, claim.object_id, claim.predicate, subject.name, obj.name], ensure_ascii=False)))

        claims[claim_id] = {
            "id": claim_id,
            "subject_local": subject.local_id,
            "object_local": obj.local_id,
            "predicate": claim.predicate,
            "quote": quote,
            "body_start": pos,
            "body_end": pos + len(quote),
            "modality": claim.modality,
            "attribution": claim.attribution,
            "subject_role": claim.subject_role,
            "predicate_raw": claim.predicate_raw,
        }

    return (list(entities.values()), list(claims.values()), rejected)


class Extractor:
    def __init__(self, model_id: str = CFG.model_id):
        self.model_id = model_id

        self.client = OpenAI(
            base_url=CFG.base_url,
            api_key=CFG.api_key,
            timeout=180,
        )


    def extract(self, article: Article) -> dict:
        import logging

        chunks = []
        total_rejected = 0
        total_schema_errors = 0

        for i, (offset, snippet) in enumerate(
            windows(
                article.body,
                CFG.window_chars,
                CFG.window_overlap,
            )
        ):
            request = (
                f"DOCUMENTO: {article.rel_path}\n"
                f"TITOLO: {article.title}\n"
                f"SOTTOTITOLO: {article.subtitle}\n"
                f"DATA PUBBLICAZIONE: {article.publication_date}\n"
                f"BODY:\n{snippet}"
            )

            success = False
            last_error = None

            for attempt in range(2):
                response_text = ""
                finish_reason = None

                try:
                    kwargs = {}

                    if CFG.json_mode:
                        kwargs["response_format"] = {
                            "type": "json_object"
                        }

                    response = (
                        self.client.chat.completions.create(
                            model=self.model_id,
                            temperature=0,
                            max_tokens=CFG.max_output_tokens,
                            messages=[
                                {
                                    "role": "system",
                                    "content": PROMPT,
                                },
                                {
                                    "role": "user",
                                    "content": request,
                                },
                            ],
                            extra_body={
                                "enable_thinking": False,
                            },
                            **kwargs,
                        )
                    )

                    choice = response.choices[0]
                    finish_reason = choice.finish_reason
                    response_text = (
                        choice.message.content or ""
                    )

                    if finish_reason == "length":
                        raise ValueError(
                            "Output troncato per limite token. "
                            "Ridurre WINDOW_CHARS oppure "
                            "aumentare MAX_OUTPUT_TOKENS."
                        )

                    if not response_text.strip():
                        raise ValueError(
                            "Risposta vuota del modello"
                        )

                    decoded = parse_json_object(
                        response_text
                    )

                    result, schema_errors = (
                        validate_partial_graph(decoded)
                    )

                    ents, claims, rejected = (
                        grounded_records(
                            article,
                            offset,
                            snippet,
                            result,
                            i,
                        )
                    )

                    # Se il modello ha prodotto claim ma
                    # nessuno è utilizzabile, ritentiamo.
                    if (
                        decoded.get("claims")
                        and not claims
                    ):
                        raise ValueError(
                            "Nessun claim verificabile "
                            "nella finestra"
                        )

                    total_rejected += rejected
                    total_schema_errors += len(schema_errors)

                    if schema_errors or rejected:
                        logging.warning(
                            "%s finestra %d: "
                            "%d errori schema, "
                            "%d claim non verificabili",
                            article.rel_path,
                            i,
                            len(schema_errors),
                            rejected,
                        )

                    chunks.append({
                        "index": i,
                        "start": offset,
                        "end": offset + len(snippet),
                        "text": snippet,
                        "entities": ents,
                        "claims": claims,
                        "schema_errors": schema_errors,
                        "rejected_claims": rejected,
                    })

                    success = True
                    break

                except (ValueError, ValidationError) as exc:
                    last_error = exc

                    # Salviamo l'output problematico
                    # per poterlo analizzare dopo.
                    debug_dir = (
                        CFG.output_dir / "_diagnostics"
                    )

                    debug_dir.mkdir(
                        parents=True,
                        exist_ok=True,
                    )

                    debug_file = debug_dir / (
                        f"{article.id}_"
                        f"chunk{i}_attempt{attempt + 1}.json"
                    )

                    debug_file.write_text(
                        json.dumps(
                            {
                                "article": article.rel_path,
                                "chunk_index": i,
                                "attempt": attempt + 1,
                                "finish_reason": finish_reason,
                                "error": str(exc),
                                "raw_response": response_text,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )

            if not success:
                raise ValueError(
                    f"Finestra {i}: "
                    f"estrazione fallita dopo due tentativi. "
                    f"Ultimo errore: {last_error}"
                )

        return {
            "article_id": article.id,
            "content_hash": article.content_hash,
            "model_id": self.model_id,
            "extract_version": VERSION,
            "rejected_claims": total_rejected,
            "schema_errors": total_schema_errors,
            "chunks": chunks,
        }
