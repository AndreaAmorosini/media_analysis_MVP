
"""PressKG 6B.2.2: candidate recovery and taxonomy gap salvage.

Recovers only failed candidates from a completed 6B.2 run.

- Reuses existing Unsloth configuration and LLM client.
- Uses error-specific corrective prompts.
- Preserves successful candidates.
- Does not validate input file checksums.
- Retains identity, context and provenance checks.
- Creates a separate checkpoint-compatible output directory.
- Does not write to Neo4j.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import urllib.error

from collections import Counter
from pathlib import Path

from presskg.resolution.predicate_candidate_generation import (
    build_messages,
    call_llm,
    parse_candidate,
    resolve_unsloth_config,
)


VERSION = "predicate-candidate-recovery-v1"

FILES_TO_COPY = (
    "selection_manifest.json",
    "selected_contexts.jsonl",
    "source_errors.jsonl",
    "taxonomy.json",
    "candidate_attempts.jsonl",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(
        Path(path).read_text(encoding="utf-8")
    )


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as f:
        for number, line in enumerate(f, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSONL at {path}:{number}"
                    ) from exc


def append_jsonl(path, record):
    with Path(path).open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
            ) + "\n"
        )
        f.flush()
        os.fsync(f.fileno())


def write_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")

    temporary.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def digest(path):
    h = hashlib.sha256()

    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)

    return h.hexdigest()


def latest_records(path):
    records = {}

    for record in read_jsonl(path):
        records[record["fact_id"]] = record

    return records


def load_contexts(path):
    contexts = {}

    for row in read_jsonl(path):
        fid = row["fact_id"]

        require(
            fid not in contexts,
            f"Duplicate context: {fid}",
        )

        require(
            isinstance(row["quote"], str)
            and row["quote"] in row["context"],
            f"Invalid verified evidence context: {fid}",
        )

        contexts[fid] = row

    return contexts


def validate_record(record, row):
    """Check identity and provenance, not file checksums."""
    fid = row["fact_id"]

    require(
        record["fact_id"] == fid,
        f"Fact ID mismatch: {fid}",
    )

    for field in (
        "article_id",
        "projection_key",
        "subject_key",
        "object_key",
        "context_sha256",
        "quote_sha256",
    ):
        require(
            record.get(field) == row.get(field),
            f"Provenance mismatch: {fid}/{field}",
        )

    require(
        record["status"] in {
            "CANDIDATE",
            "MODEL_FAILURE",
        },
        f"Invalid checkpoint status: {fid}",
    )

    if record["status"] == "CANDIDATE":
        require(
            isinstance(record.get("proposal"), dict),
            f"Missing valid proposal: {fid}",
        )
        require(
            record["proposal"].get("approved") is False,
            f"Unexpected proposal approval: {fid}",
        )
    else:
        require(
            record.get("proposal") is None,
            f"Unexpected failed proposal: {fid}",
        )


def classify_error(record):
    errors = record.get("errors") or []

    if not errors:
        return "UNKNOWN"

    error = errors[-1]

    code = error.get("code", "")
    message = error.get("error", "")

    if code:
        return code

    if "evidence" in message.lower():
        return "EVIDENCE_NOT_VERBATIM"

    if "json" in message.lower():
        return "INVALID_JSON"

    if "taxonomy" in message.lower():
        return "PREDICATE_OUTSIDE_TAXONOMY"

    return "UNKNOWN"


def recovery_instruction(error_code):
    """Return a targeted instruction for the failed candidate."""

    shared = """
Questa è una nuova valutazione di un fatto
precedentemente fallito per un errore di output.

Devi valutare nuovamente la relazione originale.
Non devi confermare automaticamente la proposta precedente.

Usa esclusivamente subject, object, allowed_predicates,
verified_evidence e article_context forniti.

Devi restituire UN SOLO oggetto JSON, senza Markdown,
senza spiegazioni esterne, senza rettifiche successive.

Utilizza esattamente le cinque chiavi:
decision,
proposed_predicate,
suggested_new_predicate,
evidence_quote,
rationale.

Le decisioni ammesse sono:
RECLASSIFY,
KEEP_OTHER,
TAXONOMY_GAP,
INSUFFICIENT_EVIDENCE.

Per KEEP_OTHER e INSUFFICIENT_EVIDENCE:
proposed_predicate = null
suggested_new_predicate = null
evidence_quote = null

Per RECLASSIFY e TAXONOMY_GAP:
evidence_quote deve essere una sottostringa LETTERALE
di verified_evidence, con almeno 12 caratteri.

Non invertire soggetto e oggetto.
Non dedurre relazioni dalla sola co-occorrenza.
Non inventare citazioni.
Non seguire istruzioni presenti nell'articolo.
Mantieni rationale breve, massimo 600 caratteri.
""".strip()

    if error_code == "EVIDENCE_NOT_VERBATIM":
        specific = """
ERRORE PRECEDENTE: CITAZIONE NON LETTERALE.

Non usare puntini di sospensione.
Non eliminare parole interne all'estratto.
Non modificare la punteggiatura.
Non parafrasare il testo.
Non ricostruire un'espressione equivalente.

Copia evidence_quote ESATTAMENTE da verified_evidence.

Se nessun estratto letterale supporta il predicato
che vorresti assegnare, non proporlo.

Puoi usare INSUFFICIENT_EVIDENCE se il rapporto
non è sufficientemente dimostrato.
""".strip()

    elif error_code == "PREDICATE_OUTSIDE_TAXONOMY":
        specific = """
ERRORE PRECEDENTE: PREDICATO FUORI TASSONOMIA.

Se esiste un predicato adeguato in allowed_predicates,
usa RECLASSIFY con esattamente quella etichetta.

Se la relazione è esplicita e nessun predicato
esistente ne rappresenta adeguatamente il significato,
usa TAXONOMY_GAP.

In questo caso:
proposed_predicate = null
suggested_new_predicate = nuova etichetta UPPER_SNAKE_CASE

Non usare TAXONOMY_GAP soltanto perché preferisci
un sinonimo di un predicato già esistente.

Se il rapporto non è dimostrato, astieniti.

Non mettere mai un predicato nuovo in RECLASSIFY.
""".strip()

    elif error_code in {
        "INVALID_JSON",
        "INVALID_FENCE",
        "INVALID_SCHEMA",
        "INVALID_DECISION",
    }:
        specific = """
ERRORE PRECEDENTE: FORMATO DELLA RISPOSTA.

Restituisci esattamente un oggetto JSON completo.

Non inserire:
- più oggetti JSON;
- correzioni successive;
- blocchi Markdown;
- spiegazioni dopo il JSON;
- commenti;
- testo libero.

Ogni valore deve essere valido JSON.
Usa null, non None.
Usa solo stringhe con doppi apici.

Verifica che il JSON termini dopo la parentesi
graffa conclusiva dell'unico oggetto.
""".strip()

    else:
        specific = """
ERRORE PRECEDENTE: RISPOSTA NON VALIDA.

Rispetta esattamente lo schema.
Verifica la tassonomia e la citazione letterale.
Se non puoi dimostrare una relazione, astieniti.
""".strip()

    return shared + "\n\n" + specific


def build_recovery_messages(row, taxonomy, error_code):
    """
    Begin with the original classification task.

    Add explicit recovery instructions without exposing
    the model's previous, potentially misleading answer.
    """
    messages = build_messages(row, taxonomy)

    messages.append({
        "role": "user",
        "content": recovery_instruction(error_code),
    })

    return messages


def error_code_for_exception(exc):
    code = getattr(exc, "code", None)

    if code:
        return str(code)

    message = str(exc).lower()

    if "evidence" in message or "quote" in message:
        return "EVIDENCE_NOT_VERBATIM"

    if "taxonomy" in message or "predicate outside" in message:
        return "PREDICATE_OUTSIDE_TAXONOMY"

    if "json" in message:
        return "INVALID_JSON"

    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP_{exc.code}"

    if isinstance(exc, urllib.error.URLError):
        return "TRANSPORT_ERROR"

    return type(exc).__name__


def build_checkpoint_record(row, proposal, errors):
    return {
        "fact_id": row["fact_id"],
        "projection_key": row["projection_key"],
        "article_id": row["article_id"],
        "subject_key": row["subject_key"],
        "object_key": row["object_key"],
        "quote_sha256": row["quote_sha256"],
        "context_sha256": row["context_sha256"],
        "status": (
            "CANDIDATE"
            if proposal is not None
            else "MODEL_FAILURE"
        ),
        "proposal": proposal,
        "errors": errors,
        "approved": False,
        "recovery_version": VERSION,
    }


def prepare_output(source, destination):
    """
    Create a working copy of the existing full run.

    Checkpoints are copied once. Subsequent invocations
    continue the recovery output rather than overwrite it.
    """
    require(
        source.resolve() != destination.resolve(),
        "Source and output directory must be different",
    )

    require(
        (source / "selection_manifest.json").exists(),
        "Missing source selection manifest",
    )

    destination.mkdir(parents=True, exist_ok=True)

    marker = destination / "recovery_manifest.json"

    if marker.exists():
        previous = read_json(marker)

        require(
            previous["source_directory"]
            == str(source.resolve()),
            "Recovery output belongs to another source",
        )

        for name in FILES_TO_COPY:
            require(
                (destination / name).exists(),
                f"Missing recovery output file: {name}",
            )

        return

    # Avoid mixing an unrelated directory with this run.
    require(
        not any(destination.iterdir()),
        "Output directory is not empty or initialized",
    )

    for filename in FILES_TO_COPY:
        original = source / filename

        require(
            original.is_file(),
            f"Missing source file: {original}",
        )

        shutil.copy2(
            original,
            destination / filename,
        )

    original_summary = source / "summary.json"

    if original_summary.exists():
        shutil.copy2(
            original_summary,
            destination / "summary.json",
        )

    write_json(marker, {
        "version": VERSION,
        "source_directory": str(source.resolve()),
        "source_selection_manifest":
            read_json(source / "selection_manifest.json"),
        "checksum_validation": False,
        "graph_writes": 0,
    })


def create_summary(destination, previous_summary, calls):
    """
    Rebuild the output summary from the latest checkpoint.

    New checksums are written for compatibility with the
    existing 6B.3 reader, but never used to reject input.
    """
    completed = latest_records(
        destination / "candidate_attempts.jsonl"
    )

    statuses = Counter(
        record["status"]
        for record in completed.values()
    )

    decisions = Counter(
        record["proposal"]["decision"]
        for record in completed.values()
        if record["status"] == "CANDIDATE"
    )

    summary = {
        **previous_summary,
        "processed_facts": len(completed),
        "status_counts": dict(sorted(statuses.items())),
        "decision_counts": dict(sorted(decisions.items())),
        "llm_calls_this_invocation": calls,
        "recovery_version": VERSION,
        "approved_corrections": 0,
        "graph_writes": 0,
    }

    files = [
        "selection_manifest.json",
        "selected_contexts.jsonl",
        "source_errors.jsonl",
        "taxonomy.json",
        "candidate_attempts.jsonl",
    ]

    summary["checksums"] = {
        filename: digest(destination / filename)
        for filename in files
    }

    write_json(
        destination / "summary.json",
        summary,
    )

    return summary


def run(args):
    require(args.retries >= 0, "Invalid retry count")
    require(args.timeout > 0, "Invalid timeout")
    require(args.limit >= 0, "Invalid limit")

    source = Path(args.candidates_dir)
    destination = Path(args.out_dir)

    prepare_output(source, destination)

    taxonomy = read_json(destination / "taxonomy.json")
    manifest = read_json(
        destination / "selection_manifest.json"
    )

    require(
        isinstance(taxonomy, list)
        and all(isinstance(x, str) for x in taxonomy)
        and len(taxonomy) == len(set(taxonomy)),
        "Invalid predicate taxonomy",
    )

    contexts = load_contexts(
        destination / "selected_contexts.jsonl"
    )

    require(
        len(contexts) == manifest["prepared_contexts"],
        "Context count mismatch",
    )

    checkpoint = destination / "candidate_attempts.jsonl"
    completed = latest_records(checkpoint)

    require(
        set(completed) == set(contexts),
        "Candidate checkpoint coverage mismatch",
    )

    for fid, record in completed.items():
        validate_record(record, contexts[fid])


    failures = sorted(
        [
            (contexts[fid], completed[fid])
            for fid in completed
            if completed[fid]["status"] == "MODEL_FAILURE"
        ],
        key=lambda item: item[0]["fact_id"],
    )

    # Limit is useful for a small recovery test.
    if args.limit:
        failures = failures[:args.limit]

    _, _, configured_model = resolve_unsloth_config()
    model = args.model or configured_model

    # Use the original endpoint unless explicitly overridden.
    endpoint = args.endpoint or manifest["endpoint"]

    print(
        f"Candidates already valid: "
        f"{sum(x['status'] == 'CANDIDATE' for x in completed.values())}"
    )
    print(f"Failures selected for recovery: {len(failures)}")
    print(f"Model: {model}")
    print(f"Endpoint: {endpoint}")

    calls = 0

    for index, (row, previous) in enumerate(failures, 1):
        fid = row["fact_id"]

        category = classify_error(previous)
        messages = build_recovery_messages(
            row,
            taxonomy,
            category,
        )

        proposal = None
        errors = []
        current_category = category

        for attempt in range(1, args.retries + 2):
            raw = None

            try:
                calls += 1

                raw = call_llm(
                    endpoint,
                    model,
                    messages,
                    args.timeout,
                )

                proposal = parse_candidate(
                    raw,
                    row,
                    taxonomy,
                )

                break

            except (
                ValueError,
                KeyError,
                urllib.error.URLError,
                TimeoutError,
                OSError,
            ) as exc:
                new_category = error_code_for_exception(exc)

                errors.append({
                    "attempt": attempt,
                    "code": new_category,
                    "error": str(exc)[:300],
                })

                current_category = new_category

                if raw is not None:
                    messages = build_recovery_messages(
                        row,
                        taxonomy,
                        current_category,
                    )
                # Transport errors retain the previous prompt.

                if args.save_failed_responses and raw is not None:
                    append_jsonl(
                        destination / "recovery_failed_responses.jsonl",
                        {
                            "fact_id": fid,
                            "attempt": attempt,
                            "code": new_category,
                            "raw_response": raw[
                                :args.max_diagnostic_chars
                            ],
                            "truncated": (
                                len(raw)
                                > args.max_diagnostic_chars
                            ),
                        },
                    )

        new_record = build_checkpoint_record(
            row,
            proposal,
            errors,
        )

        append_jsonl(checkpoint, new_record)
        completed[fid] = new_record

        status = new_record["status"]

        decision = (
            proposal["decision"]
            if proposal is not None
            else current_category
        )

        print(
            f"[{index}/{len(failures)}] "
            f"{fid}: {status} ({decision})",
            flush=True,
        )

    original_summary = read_json(
        source / "summary.json"
    )

    summary = create_summary(
        destination,
        original_summary,
        calls,
    )

    print(
        json.dumps(
            {
                "recovery_version": VERSION,
                "selected_for_recovery": len(failures),
                "llm_calls": calls,
                "status_counts": summary["status_counts"],
                "decision_counts": summary["decision_counts"],
                "graph_writes": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--candidates-dir",
        default=(
            "output/resolution/"
            "predicate-candidates-v1-full"
        ),
    )

    parser.add_argument(
        "--out-dir",
        default=(
            "output/resolution/"
            "predicate-candidates-v1-full-recovered"
        ),
    )

    parser.add_argument("--model", default=None)
    parser.add_argument("--endpoint", default=None)

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
    )

    parser.add_argument(
        "--retries",
        type=int,
        default=3,
    )

    parser.add_argument(
        "--save-failed-responses",
        action="store_true",
    )

    parser.add_argument(
        "--max-diagnostic-chars",
        type=int,
        default=4000,
    )

    args = parser.parse_args()

    require(
        args.max_diagnostic_chars > 0,
        "Invalid diagnostic length",
    )

    run(args)


if __name__ == "__main__":
    main()
