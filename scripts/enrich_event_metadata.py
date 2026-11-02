
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import unicodedata

from datetime import date, timedelta
from pathlib import Path

from json_repair import loads as repair_loads
from neo4j import GraphDatabase
from openai import OpenAI
from tqdm import tqdm

from presskg.settings import CFG


VERSION = "event-metadata-v2-temporal-grounding"

STATUSES = {
    "occurred",
    "planned",
    "forecast",
    "alleged",
    "hypothetical",
    "generic",
    "uncertain",
}

DATE_METHODS = {
    "none",
    "explicit",
    "relative_to_publication",
    "contextual_year",
    "partial",
}

FIELDS = [
    "event_id",
    "event_name",
    "article_id",
    "publication_date",
    "event_type",
    "event_type_group",
    "event_type_evidence_text",
    "event_status",
    "event_date",
    "event_end_date",
    "event_date_text",
    "event_deadline_text",
    "event_date_method",
    "location",
    "date_claim_ids",
    "location_claim_ids",
    "type_claim_ids",
    "reason",
    "validation_warning",
    "model",
    "metadata_version",
    "run_fingerprint",
    "approved",
]


PROMPT = """
Sei un estrattore conservativo di metadati
per UNA EventMention tratta dalla stampa italiana.

Ricevi:
- nome dell'evento;
- data di pubblicazione;
- frammenti di testo verificati con claim_id.

Rispondi SOLO con un JSON contenente:

{
  "event_type": null,
  "event_type_group": null,
  "event_type_evidence_text": null,
  "event_status": "uncertain",
  "event_date": null,
  "event_end_date": null,
  "event_date_text": null,
  "event_deadline_text": null,
  "event_date_method": "none",
  "location": null,
  "date_claim_ids": [],
  "location_claim_ids": [],
  "type_claim_ids": [],
  "reason": ""
}

REGOLE:

1. EVENT STATUS

occurred: evento riportato come avvenuto.
planned: evento programmato da persone o organizzazioni.
forecast: fenomeno naturale futuro previsto.
alleged: episodio presunto o contestato.
hypothetical: possibilità, rischio o scenario ipotetico.
generic: non è un episodio concreto identificabile.
uncertain: evidenza insufficiente.

Un temporale previsto è forecast, NON planned.
Un lavoro programmato è planned.
Un allagamento che si vuole prevenire è hypothetical.

2. EVENT TYPE

event_type è un sottotipo descrittivo breve
in snake_case, indipendente dal dominio.

event_type_group deve essere una delle
categorie presenti nella tassonomia fornita,
oppure null.

event_type_evidence_text è una citazione BREVE
ed ESATTA dal nome dell'evento o dalle evidenze,
che giustifica la classificazione.

Se la citazione proviene dal nome dell'evento,
type_claim_ids può essere [].

Se proviene dai claim, specifica gli ID.

3. DATA

event_date usa YYYY-MM-DD soltanto se
giustificabile dalle evidenze.

event_date_text deve contenere l'espressione
temporale LETTERALE presente nel testo.

event_date_method:
- explicit: giorno, mese e anno espliciti;
- relative_to_publication: ieri, oggi, domani,
  questa mattina e altre espressioni relative
  chiaramente riferite alla cronaca dell'articolo;
- contextual_year: giorno e mese espliciti,
  anno deducibile dal contesto della notizia;
- partial: informazione temporale incompleta;
- none: nessuna informazione temporale.

Non assumere mai che la data di pubblicazione
coincida con la data dell'evento.

Per ieri/oggi/domani usa il riferimento alla
pubblicazione SOLO se la voce narrante è
l'articolo, non una dichiarazione storica.

Per eventi storici non usare automaticamente
l'anno di pubblicazione.

4. INTERVALLI E SCADENZE

event_end_date è ammessa solo per un intervallo
reale con data di inizio e fine.

"Entro il 2025" è una scadenza:
usa event_deadline_text e NON event_end_date.

"Dal 27 giugno al 2 luglio" è invece
un intervallo dell'evento.

5. LOCATION

Identifica il luogo dell'evento specifico,
non un luogo semplicemente citato.

6. EVIDENZE

date_claim_ids, location_claim_ids e
type_claim_ids devono contenere esclusivamente
ID ricevuti nell'input.

Non inventare claim ID, date, luoghi,
tipologie o informazioni mancanti.

Non trasferire metadati da altri episodi
citati nello stesso articolo.

Le evidenze possono contenere errori OCR
e frammenti di notizie adiacenti.

In caso di dubbio, restituisci null.
"""


PROMPT += """

CONTROLLO DELL'IDENTITÀ DELL'EVENTO

event_name identifica l'UNICO evento da
arricchire.

Prima di compilare i campi, verifica che
ogni informazione si riferisca proprio
a event_name.

Un procedimento giudiziario nato da
infiltrazioni d'acqua NON trasforma
l'evento "infiltrazioni d'acqua"
in un procedimento giudiziario.

La revoca di un divieto e l'imposizione
originale del divieto sono due eventi
differenti, eventualmente con date diverse.

Non usare date, luoghi o tipologie
appartenenti a eventi secondari.

CONTROLLO DELLO STATO

- Se l'evento è descritto come già
  realizzato, concluso, approvato,
  inaugurato o avvenuto, usa occurred.
- Se deve ancora svolgersi, usa planned.
- Se è un fenomeno naturale previsto,
  usa forecast.
- Se è solo una possibilità, usa hypothetical.

Non classificare planned soltanto perché
l'evento ha conseguenze o attività future.

TIPOLOGIA

event_type_group è la categoria generale
presa dalla tassonomia.

event_type è un sottotipo PIÙ SPECIFICO,
quando ricavabile.

NON copiare event_type_group in event_type.
Se non puoi determinare un sottotipo
specifico, usa event_type=null.

DATE

Se trovi "ieri", "oggi", "domani" o altre
espressioni relative:

- copia in event_date_text la formulazione
  LETTERALE presente nell'evidenza;
- usa event_date_method=relative_to_publication;
- fornisci il claim_id dell'evidenza;
- puoi lasciare event_date=null:
  Python risolverà la data.

Se trovi una data completa che si riferisce
all'evento target, restituisci anche
event_date in formato YYYY-MM-DD.

Non confondere la data di un provvedimento
con quella del provvedimento che lo revoca.

Se hai un'espressione temporale ma non
riesci a determinare la data completa,
usa event_date_method=partial.

Per scadenze come "entro il 2025",
compila event_deadline_text, non
event_end_date.

Non inventare evidenze o citazioni:
event_date_text deve corrispondere
a testo realmente presente nell'input.
"""


READ_EVENT = """
MATCH (e:EventMention {id:$id})
MATCH (a:Article {id:e.article_id})

OPTIONAL MATCH
    (c:Claim)-[:SUBJECT|OBJECT]->(e)

WHERE c.article_id = a.id

RETURN
    e.id AS id,
    e.name AS name,
    a.id AS article_id,
    a.title AS title,
    a.publication_date AS publication_date,
    a.body AS body,
    collect(
        DISTINCT CASE
            WHEN c IS NULL THEN null
            ELSE {
                id:c.id,
                quote:c.quote,
                start:c.body_start,
                end:c.body_end
            }
        END
    ) AS claims
"""


def clean(value, limit=200):
    if not isinstance(value, str):
        return None

    value = " ".join(value.split())

    return value[:limit] if value else None


def norm(value):
    text = unicodedata.normalize(
        "NFKD",
        str(value or "").casefold(),
    )

    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )

    return " ".join(
        re.sub(r"[^\w]+", " ", text).split()
    )


def found(fragment, sources):
    value = norm(fragment)

    return bool(value) and any(
        value in norm(source)
        for source in sources
    )


def parse_iso(value):
    if not value:
        return None

    try:
        return date.fromisoformat(
            str(value)
        ).isoformat()
    except ValueError:
        return None


def relative_date(expression, publication_date):
    """
    Risolve solo espressioni semplici,
    non date naturali ambigue.

    Il collegamento semantico con l'evento
    rimane responsabilità del modello
    e della successiva revisione.
    """

    base = parse_iso(publication_date)

    if not base:
        return None

    text = norm(expression)

    offsets = {
        "l altro ieri": -2,
        "ieri": -1,
        "oggi": 0,
        "quest oggi": 0,
        "stamattina": 0,
        "questa mattina": 0,
        "domani": 1,
        "dopodomani": 2,
    }

    for key, offset in offsets.items():
        if (
            text == key
            or text.startswith(key + " ")
        ):
            suffix = text[len(key):].strip()

            if suffix in {
                "",
                "mattina",
                "pomeriggio",
                "sera",
            }:
                return (
                    date.fromisoformat(base)
                    + timedelta(days=offset)
                ).isoformat()

    return None


def has_day_month(expression):
    months = (
        "gennaio febbraio marzo aprile "
        "maggio giugno luglio agosto "
        "settembre ottobre novembre dicembre"
    ).split()

    pattern = (
        r"\b(?:[1-9]|[12][0-9]|3[01])\s+(?:"
        + "|".join(months)
        + r")\b"
    )

    return bool(
        re.search(pattern, norm(expression))
    )


def date_matches_day_month(value, expression):
    if not value or not has_day_month(expression):
        return False

    d = date.fromisoformat(value)

    months = (
        "gennaio febbraio marzo aprile "
        "maggio giugno luglio agosto "
        "settembre ottobre novembre dicembre"
    ).split()

    pattern = (
        r"\b"
        + str(d.day)
        + r"\s+"
        + months[d.month - 1]
        + r"\b"
    )

    return bool(
        re.search(pattern, norm(expression))
    )


def parse_model_json(text):
    text = re.sub(
        r"^```(?:json)?\s*",
        "",
        (text or "").strip(),
        flags=re.I,
    )

    text = re.sub(
        r"\s*```$",
        "",
        text,
    )

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return repair_loads(text)


def prepare_evidence(record, max_claims=12):
    """
    Recupera soltanto claim con offset verificati.

    Privilegia i frammenti in cui il nome
    dell'evento compare maggiormente.
    """

    body = record.get("body") or ""
    ranked = []

    for claim in record.get("claims") or []:
        if not claim:
            continue

        cid = claim.get("id")
        start = claim.get("start")
        end = claim.get("end")
        quote = claim.get("quote")

        if not isinstance(cid, str):
            continue

        if not (
            isinstance(start, int)
            and isinstance(end, int)
            and isinstance(quote, str)
            and 0 <= start < end <= len(body)
            and body[start:end] == quote
        ):
            continue

        name_tokens = {
            word
            for word in norm(
                record.get("name")
            ).split()
            if len(word) >= 4
        }

        quote_tokens = set(
            norm(quote).split()
        )

        relevance = len(
            name_tokens & quote_tokens
        )

        ranked.append(
            (-relevance, start, claim)
        )

    ranked.sort(
        key=lambda item: (
            item[0],
            item[1],
        )
    )

    snippets = []

    for _, _, claim in ranked[:max_claims]:
        start = claim["start"]
        end = claim["end"]

        snippets.append({
            "claim_id": claim["id"],
            "predicate_evidence": claim["quote"],
            "context": body[
                max(0, start - 220):
                min(len(body), end + 220)
            ],
        })

    return snippets


def validate_proposal(
    proposal,
    record,
    snippets,
    model,
    groups,
):
    if not isinstance(proposal, dict):
        raise ValueError(
            "Risposta JSON non valida"
        )

    warnings = []

    by_id = {
        snippet["claim_id"]: snippet
        for snippet in snippets
    }

    def references(field):
        values = proposal.get(field, [])

        if not isinstance(values, list):
            warnings.append(
                field + "_not_list"
            )
            return []

        result = []

        for value in values:
            if (
                isinstance(value, str)
                and value in by_id
            ):
                if value not in result:
                    result.append(value)
            else:
                warnings.append(
                    field + "_unknown_ref"
                )

        return result

    date_ids = references("date_claim_ids")
    location_ids = references("location_claim_ids")
    type_ids = references("type_claim_ids")

    def sources(ids):
        return [
            text
            for cid in ids
            for text in (
                by_id[cid]["predicate_evidence"],
                by_id[cid]["context"],
            )
        ]

    name = record.get("name") or ""

    # Event status
    status = proposal.get(
        "event_status",
        "uncertain",
    )

    if status not in STATUSES:
        status = "uncertain"
        warnings.append(
            "invalid_event_status"
        )

    # Tipologia generale
    group = clean(
        proposal.get("event_type_group"),
        80,
    )

    if group and group not in groups:
        warnings.append(
            "invalid_type_group"
        )
        group = None

    # Sottotipo descrittivo
    subtype = clean(
        proposal.get("event_type"),
        80,
    )

    if subtype:
        subtype = re.sub(
            r"[^a-z0-9_]+",
            "_",
            subtype.casefold(),
        ).strip("_")
        
    if group and subtype == group:
        subtype = None

    type_quote = clean(
        proposal.get("event_type_evidence_text"),
        160,
    )

    type_supported = bool(
        type_quote
        and (
            found(type_quote, [name])
            or (
                type_ids
                and found(
                    type_quote,
                    sources(type_ids),
                )
            )
        )
    )

    if (
        (group or subtype)
        and not type_supported
    ):
        warnings.append(
            "type_not_grounded"
        )

        group = None
        subtype = None
        type_quote = None

    # Localizzazione
    location = clean(
        proposal.get("location"),
        160,
    )

    if location and not (
        found(location, [name])
        or (
            location_ids
            and found(
                location,
                sources(location_ids),
            )
        )
    ):
        warnings.append(
            "location_not_grounded"
        )
        location = None

    # Date e scadenze
    date_text = clean(
        proposal.get("event_date_text"),
        200,
    )

    deadline = clean(
        proposal.get("event_deadline_text"),
        200,
    )

    event_date = parse_iso(
        proposal.get("event_date")
    )

    event_end = parse_iso(
        proposal.get("event_end_date")
    )

    method = proposal.get(
        "event_date_method",
        "none",
    )

    if method not in DATE_METHODS:
        warnings.append(
            "invalid_date_method"
        )
        method = "none"

    if proposal.get("event_date") and not event_date:
        warnings.append(
            "invalid_event_date"
        )

    if proposal.get("event_end_date") and not event_end:
        warnings.append(
            "invalid_event_end_date"
        )

    # Controllo delle scadenze
    if deadline and not (
        found(deadline, [name])
        or (
            date_ids
            and found(
                deadline,
                sources(date_ids),
            )
        )
    ):
        warnings.append(
            "deadline_not_grounded"
        )
        deadline = None

    # Le espressioni temporali devono comparire
    # esattamente nel nome o nelle evidenze.
    if date_text and not (
        found(date_text, [name])
        or (
            date_ids
            and found(
                date_text,
                sources(date_ids),
            )
        )
    ):
        warnings.append(
            "date_text_not_grounded"
        )

        date_text = None
        event_date = None
        event_end = None
        method = "none"

    if (
        (event_date or event_end)
        and not date_text
    ):
        warnings.append(
            "iso_date_without_text"
        )

        event_date = None
        event_end = None
        method = "none"

    if date_text:
        normalized = norm(date_text)

        # Una scadenza non è una data di fine.
        if normalized.startswith((
            "entro ",
            "non oltre ",
            "al piu tardi ",
        )):
            if not deadline:
                deadline = date_text

            date_text = None
            event_date = None
            event_end = None
            method = "none"

        else:
            resolved = relative_date(
                date_text,
                record.get("publication_date"),
            )

            if resolved:
                if (
                    event_date
                    and event_date != resolved
                ):
                    warnings.append(
                        "relative_date_corrected"
                    )

                event_date = resolved
                method = "relative_to_publication"

                if event_end:
                    warnings.append(
                        "end_date_for_relative_single_day"
                    )
                    event_end = None

            elif method == "relative_to_publication":
                warnings.append(
                    "relative_date_unresolved"
                )

                event_date = None
                event_end = None
                method = "partial"

            elif event_date and method == "explicit":
                year = str(
                    date.fromisoformat(
                        event_date
                    ).year
                )

                if year not in norm(date_text):
                    if (
                        has_day_month(date_text)
                        and date_matches_day_month(
                            event_date,
                            date_text,
                        )
                    ):
                        method = "contextual_year"
                    else:
                        warnings.append(
                            "explicit_date_not_proven"
                        )
                        event_date = None
                        event_end = None
                        method = "partial"

            if (
                event_date
                and method == "contextual_year"
            ):
                pub = parse_iso(
                    record.get("publication_date")
                )

                close_to_publication = bool(
                    pub
                    and abs((
                        date.fromisoformat(event_date)
                        - date.fromisoformat(pub)
                    ).days) <= 180
                )

                if not (
                    close_to_publication
                    and date_matches_day_month(
                        event_date,
                        date_text,
                    )
                ):
                    warnings.append(
                        "contextual_year_not_proven"
                    )

                    event_date = None
                    event_end = None
                    method = "partial"

            if (
                method in {"none", "partial"}
                and event_date
            ):
                warnings.append(
                    "date_method_inconsistent"
                )

                event_date = None
                event_end = None
                method = "partial"

            if method == "none":
                method = "partial"

    if event_end and (
        not event_date
        or event_end < event_date
    ):
        warnings.append(
            "invalid_date_interval"
        )
        event_end = None

    if not event_date:
        if date_text:
            method = "partial"
        else:
            method = "none"

    # Un fenomeno naturale futuro non può
    # essere un evento programmato dall'uomo.
    if (
        status == "planned"
        and group == "natural_hazard"
    ):
        status = "forecast"

    return {
        "event_id": record["id"],
        "event_name": name,
        "article_id": record["article_id"],
        "publication_date": str(
            record.get("publication_date") or ""
        ),
        "event_type": subtype or "",
        "event_type_group": group or "",
        "event_type_evidence_text": (
            type_quote or ""
        ),
        "event_status": status,
        "event_date": event_date or "",
        "event_end_date": event_end or "",
        "event_date_text": date_text or "",
        "event_deadline_text": deadline or "",
        "event_date_method": method,
        "location": location or "",
        "date_claim_ids": json.dumps(date_ids),
        "location_claim_ids": json.dumps(
            location_ids
        ),
        "type_claim_ids": json.dumps(
            type_ids
        ),
        "reason": clean(
            proposal.get("reason"),
            700,
        ) or "",
        "validation_warning": "; ".join(
            sorted(set(warnings))
        ),
        "model": model,
        "metadata_version": VERSION,
        "run_fingerprint": "",
        "approved": "",
    }


def groups_from_file(path):
    groups = json.loads(
        Path(path).read_text(
            encoding="utf-8"
        )
    )

    if (
        not isinstance(groups, dict)
        or not all(
            isinstance(value, str)
            for value in groups.values()
        )
    ):
        raise ValueError(
            "La tassonomia deve essere "
            "un dizionario {id: descrizione}"
        )

    return groups


def generate(args, driver, groups):
    output = Path(args.out)

    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    model = args.model or CFG.model_id

    system_prompt = (
        PROMPT
        + "\nTASSONOMIA event_type_group AMMESSA:\n"
        + json.dumps(
            groups,
            ensure_ascii=False,
        )
    )

    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "model": model,
                "prompt": system_prompt,
                "max_claims": args.max_claims,
                "max_tokens": args.max_tokens,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()[:24]

    exists = output.exists()

    if exists and not args.resume:
        raise FileExistsError(
            "File già esistente: usa un nuovo "
            "--out oppure --resume."
        )

    processed = set()

    if exists:
        with output.open(
            newline="",
            encoding="utf-8",
        ) as file:
            reader = csv.DictReader(file)

            if reader.fieldnames != FIELDS:
                raise ValueError(
                    "Schema CSV incompatibile"
                )

            for row in reader:
                if (
                    row["metadata_version"] != VERSION
                    or row["run_fingerprint"] != fingerprint
                ):
                    raise ValueError(
                        "Versione, modello o prompt "
                        "diversi: usa un nuovo --out."
                    )

                processed.add(
                    row["event_id"]
                )

    client = OpenAI(
        base_url=CFG.base_url,
        api_key=CFG.api_key,
        timeout=180,
    )

    with driver.session(
        database=CFG.neo4j_database
    ) as session:
        ids = [
            row["id"]
            for row in session.run(
                """
                MATCH (e:EventMention)
                RETURN e.id AS id
                ORDER BY e.id
                """
            )
        ]

        ids = ids[args.offset:]

        if args.limit > 0:
            ids = ids[:args.limit]

        mode = "a" if exists else "w"

        with output.open(
            mode,
            newline="",
            encoding="utf-8",
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=FIELDS,
            )

            if not exists:
                writer.writeheader()

            for index, event_id in tqdm(enumerate(ids, 1), total=len(ids), desc="Event metadata", unit="event"):
                if event_id in processed:
                    continue

                found_record = session.run(
                    READ_EVENT,
                    id=event_id,
                ).single()

                if found_record is None:
                    continue

                record = dict(found_record)

                snippets = prepare_evidence(
                    record,
                    args.max_claims,
                )

                if not snippets:
                    proposal = {
                        "event_status": "uncertain",
                        "reason": (
                            "Nessuna evidenza verificabile"
                        ),
                    }

                    row = validate_proposal(
                        proposal,
                        record,
                        snippets,
                        model,
                        groups,
                    )

                    row["validation_warning"] = (
                        "no_verified_evidence"
                    )

                else:
                    request = {
                        "event_name": record["name"],
                        "article_title": record["title"],
                        "article_publication_date": str(
                            record.get(
                                "publication_date"
                            ) or ""
                        ),
                        "evidence": snippets,
                    }

                    try:
                        kwargs = (
                            {
                                "response_format": {
                                    "type": "json_object"
                                }
                            }
                            if CFG.json_mode
                            else {}
                        )

                        response = (
                            client.chat.completions.create(
                                model=model,
                                temperature=0,
                                max_tokens=args.max_tokens,
                                messages=[
                                    {
                                        "role": "system",
                                        "content": system_prompt,
                                    },
                                    {
                                        "role": "user",
                                        "content": json.dumps(
                                            request,
                                            ensure_ascii=False,
                                        ),
                                    },
                                ],
                                extra_body={
                                    "enable_thinking": False
                                },
                                **kwargs,
                            )
                        )

                        choice = response.choices[0]

                        if choice.finish_reason == "length":
                            raise ValueError(
                                "output_troncato"
                            )

                        proposal = parse_model_json(
                            choice.message.content
                        )

                        row = validate_proposal(
                            proposal,
                            record,
                            snippets,
                            model,
                            groups,
                        )

                    except Exception as exc:
                        row = validate_proposal(
                            {
                                "event_status": "uncertain",
                                "reason": str(exc)[:450],
                            },
                            record,
                            snippets,
                            model,
                            groups,
                        )

                        row["validation_warning"] = (
                            "model_or_parser_error"
                        )

                row["run_fingerprint"] = fingerprint

                writer.writerow(row)
                file.flush()

                if index % 25 == 0:
                    print(
                        f"Elaborati {index}/{len(ids)}",
                        flush=True,
                    )

    print(
        "Candidati salvati:",
        output,
    )
    print(
        "Nessuna modifica a Neo4j"
    )


def parse_refs(raw):
    values = json.loads(raw or "[]")

    if not (
        isinstance(values, list)
        and all(
            isinstance(item, str)
            for item in values
        )
    ):
        raise ValueError(
            "Campo *_claim_ids non valido"
        )

    return values


def write_metadata(tx, row):
    result = tx.run(
        """
        MATCH (e:EventMention {id:$event_id})

        WHERE
            e.metadata_version IS NULL
            OR e.metadata_version = $version

        SET
            e.event_type = $event_type,
            e.event_type_group = $event_type_group,
            e.event_status = $event_status,
            e.event_date = $event_date,
            e.event_end_date = $event_end_date,
            e.event_date_text = $event_date_text,
            e.event_deadline_text = $event_deadline_text,
            e.event_date_method = $event_date_method,
            e.location = $location,
            e.metadata_date_claim_ids = $date_claim_ids,
            e.metadata_location_claim_ids = $location_claim_ids,
            e.metadata_type_claim_ids = $type_claim_ids,
            e.metadata_type_evidence_text = $type_evidence_text,
            e.metadata_model = $model,
            e.metadata_version = $version,
            e.metadata_method = 'evidence_llm_reviewed'

        RETURN e.id AS id
        """,
        **row,
    ).single()

    if result is None:
        raise ValueError(
            "Evento non aggiornato: "
            + row["event_id"]
        )


def apply(args, driver, groups):
    with open(
        args.apply_approved,
        newline="",
        encoding="utf-8",
    ) as file:
        rows = list(
            csv.DictReader(file)
        )

    approved = [
        row
        for row in rows
        if (
            row.get("approved") or ""
        ).strip().casefold() in {
            "yes",
            "si",
            "true",
            "1",
        }
    ]

    if not approved:
        print("Nessun evento approvato")
        return

    seen = set()

    with driver.session(
        database=CFG.neo4j_database
    ) as session:
        for row in approved:
            eid = row["event_id"]

            if eid in seen:
                raise ValueError(
                    "Evento duplicato nel CSV: "
                    + eid
                )

            seen.add(eid)

            if (
                row.get("metadata_version") != VERSION
                or row.get("validation_warning")
            ):
                raise ValueError(
                    "Versione o warning incompatibile: "
                    + eid
                )

            found_record = session.run(
                READ_EVENT,
                id=eid,
            ).single()

            if found_record is None:
                raise ValueError(
                    "Evento inesistente: "
                    + eid
                )

            record = dict(found_record)

            snippets = prepare_evidence(
                record,
                args.max_claims,
            )

            proposal = {
                "event_type": row["event_type"],
                "event_type_group": (
                    row["event_type_group"]
                ),
                "event_type_evidence_text": (
                    row["event_type_evidence_text"]
                ),
                "event_status": row["event_status"],
                "event_date": row["event_date"],
                "event_end_date": row["event_end_date"],
                "event_date_text": row["event_date_text"],
                "event_deadline_text": (
                    row["event_deadline_text"]
                ),
                "event_date_method": (
                    row["event_date_method"]
                ),
                "location": row["location"],
                "date_claim_ids": parse_refs(
                    row["date_claim_ids"]
                ),
                "location_claim_ids": parse_refs(
                    row["location_claim_ids"]
                ),
                "type_claim_ids": parse_refs(
                    row["type_claim_ids"]
                ),
                "reason": row["reason"],
            }

            checked = validate_proposal(
                proposal,
                record,
                snippets,
                row["model"],
                groups,
            )

            if checked["validation_warning"]:
                raise ValueError(
                    "Evidenza non valida: "
                    + eid
                    + ": "
                    + checked["validation_warning"]
                )

            for field in (
                "event_date",
                "event_end_date",
                "event_date_text",
                "event_deadline_text",
                "event_type",
                "event_type_group",
                "event_status",
                "event_date_method",
                "location",
            ):
                if checked[field] != row[field]:
                    raise ValueError(
                        f"Campo incoerente: {field}, {eid}"
                    )

            parameters = {
                "event_id": eid,
                "version": VERSION,
                "event_type": (
                    checked["event_type"] or None
                ),
                "event_type_group": (
                    checked["event_type_group"] or None
                ),
                "event_status": checked["event_status"],
                "event_date": (
                    checked["event_date"] or None
                ),
                "event_end_date": (
                    checked["event_end_date"] or None
                ),
                "event_date_text": (
                    checked["event_date_text"] or None
                ),
                "event_deadline_text": (
                    checked["event_deadline_text"] or None
                ),
                "event_date_method": (
                    checked["event_date_method"]
                ),
                "location": (
                    checked["location"] or None
                ),
                "date_claim_ids": json.loads(
                    checked["date_claim_ids"]
                ),
                "location_claim_ids": json.loads(
                    checked["location_claim_ids"]
                ),
                "type_claim_ids": json.loads(
                    checked["type_claim_ids"]
                ),
                "type_evidence_text": (
                    checked["event_type_evidence_text"]
                    or None
                ),
                "model": checked["model"],
            }

            session.execute_write(
                write_metadata,
                parameters,
            )

    print(
        "EventMention aggiornati:",
        len(approved),
    )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--out",
        default=(
            "output/resolution/"
            "event_metadata_v2.csv"
        ),
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="0 significa tutti gli eventi",
    )

    parser.add_argument(
        "--offset",
        type=int,
        default=0,
    )

    parser.add_argument(
        "--max-claims",
        type=int,
        default=12,
    )

    parser.add_argument(
        "--max-tokens",
        type=int,
        default=1500,
    )

    parser.add_argument(
        "--model",
        default=None,
    )

    parser.add_argument(
        "--type-groups",
        default="config/event_type_groups.json",
    )

    parser.add_argument(
        "--resume",
        action="store_true",
    )

    parser.add_argument(
        "--apply-approved",
        default=None,
    )

    args = parser.parse_args()

    groups = groups_from_file(
        args.type_groups
    )

    driver = GraphDatabase.driver(
        CFG.neo4j_uri,
        auth=(
            CFG.neo4j_user,
            CFG.neo4j_password,
        ),
    )

    try:
        if args.apply_approved:
            apply(args, driver, groups)
        else:
            generate(args, driver, groups)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
