"""Utility condivise per i moduli di resolution"""

import json
from argparse import Namespace
from pathlib import Path

from presskg.common import require, read_json, read_jsonl, write_json, write_jsonl, sha256, sha256_file, normalize, fingerprint, stable_id, batches
from presskg.common.db import open_driver

__all__ = [
    "require", "read_json", "read_jsonl", "write_json", "write_jsonl", "sha256", "sha256_file", "normalize", "fingerprint", "stable_id", "batches", "open_driver", "step_driver", "close_step_driver", "debug_dump", "phase_reusable"
]


def step_driver(args: Namespace):
    """Driver condiviso dalla pipeline (Stadio A): riusa ``args.driver``
    quando la pipeline ha già aperto la connessione, altrimenti apre il
    proprio driver e lo attacca ad ``args`` insieme a ``_own_driver``.
    Gli step restano eseguibili da soli."""
    driver = getattr(args, "driver", None)
    if driver is None:
        driver = open_driver()
        args.driver = driver
        args._own_driver = True
    return driver


def close_step_driver(args: Namespace) -> None:
    """Chiude il driver solo se lo step lo ha aperto da solo.

    Il driver condiviso della pipeline (``presskg.resolution.pipeline.run``)
    resta aperto: lo chiude il ``finally`` della pipeline stessa."""
    if getattr(args, "_own_driver", False):
        driver = getattr(args, "driver", None)
        try:
            if driver is not None:
                driver.close()
        finally:
            args._own_driver = False
            if hasattr(args, "driver"):
                del args.driver


def run_steps(phase: str, steps, args: Namespace, state: dict) -> dict:
    """Runner unificato degli step di una fase (Stadio B).

    Costruisce un StepContext per step e accetta in uscita sia StepResult
    (contratto nuovo) sia plain bool (retrocompatibilità). Raccoglie gli
    esiti in ``state[phase]`` con conteggi e warning quando disponibili.
    Fail-fast: il primo step in errore interrompe la fase.

    Il 4o elemento opzionale del tuple e' un dict di override
    per-step del namespace: gli attributi vengono impostati prima
    dell'esecuzione dello step e ripristinati subito dopo (la pipeline
    cosi' fornisce a ogni step le proprie conferme/parametri dedicati
    senza inquinare il namespace condiviso).
    """
    from presskg.resolution.context import StepContext, StepResult

    _MISSING = object()
    results = {}
    for entry in steps:
        name, step, *_rest = entry
        overrides = _rest[1] if len(_rest) > 1 and isinstance(_rest[1], dict) else {}
        ctx = StepContext(
            args=args,
            phase=phase,
            step=name,
            step_module=step.__name__,
        )
        print(f"  [{name}] {step.__name__.rsplit('.', 1)[-1]} ...")
        saved = {
            key: getattr(args, key, _MISSING)
            for key in overrides
        }
        for key, value in overrides.items():
            setattr(args, key, value)
        try:
            result = step.run(ctx)
        finally:
            for key, old in saved.items():
                if old is _MISSING:
                    if hasattr(args, key):
                        delattr(args, key)
                else:
                    setattr(args, key, old)
        if isinstance(result, StepResult):
            ok = result.ok
            results[name] = {
                "ok": ok,
                "error": result.error,
                "counts": result.counts,
                "warnings": result.warnings,
                "checks": result.checks,
            }
        else:
            ok = bool(result)
            results[name] = {"ok": ok}
        debug_dump(args, phase, name, results[name])
        if not ok:
            raise RuntimeError(
                f"{phase}: step {name} fallito ({step.__name__}) "
                f"{results[name].get('error') or ''}".strip()
            )
    state[phase] = results
    return state

def debug_dump(args: Namespace, phase: str, step: str, data) -> None:
    """Salva uno snapshot di debug su disco"""
    
    if not getattr(args, "verbose", False):
        return
    
    out = Path(getattr(args, "out", "output/resolution")) / "debug" / phase
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{step}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[debug] salvato {path}")


def phase_batch_matches(summary: dict, expected_batch: str) -> bool:
    """True se il summary e' coerente col batch atteso.

    Un summary con ``source_batch_key`` ("`<view>:<batch_id>`") deve
    corrispondere al batch; un summary con ``source_batch_keys`` (lista,
    es. consolidation) deve contenerlo; senza chiavi di batch vale la sola
    esistenza (il marker e' prosecutions di fasi che operano sul grafo).
    """
    key = summary.get("source_batch_key")
    if isinstance(key, str):
        return key.rsplit(":", 1)[-1] == expected_batch
    keys = summary.get("source_batch_keys")
    if isinstance(keys, list):
        return any(
            isinstance(k, str) and k.rsplit(":", 1)[-1] == expected_batch
            for k in keys
        )
    return True


def phase_reusable(args: Namespace, markers, batch_id: str | None = None) -> bool:
    """True se gli artefatti marker di una fase sono completi e coerenti.

    ``markers``: ``(step_key, filename)`` oppure ``(step_key, filename,
    required_field)`` con un campo del summary che deve essere truthy (per
    es. ``complete``: gli step di materializzazione scrivono il summary
    anche quando falliscono, il campo esito è quello che distingue). Una
    fase e' riutilizzabile quando TUTTI i marker esistono, sono leggibili,
    hanno i campi esito richiesti e il loro batch corrisponde a quello
    atteso: il batch esplicito (``--batch-id``), altrimenti quello derivato
    dai modelli configurati
    (``presskg.validation.batch.default_batch_id``). Un summary illeggibile
    o incoerente non e' riutilizzabile: la fase viene rieseguita (o il
    fail-fast della pipeline segnala il run parziale).
    """
    from presskg.resolution import paths

    if isinstance(markers, tuple):
        markers = [markers]

    if batch_id is None:
        batch_id = getattr(args, "batch_id", None)
    if batch_id is None:
        from presskg.validation.batch import default_batch_id
        batch_id = default_batch_id()

    for marker in markers:
        step_key, filename, *required = marker
        path = paths.step_file(args, step_key, filename)
        if not path.exists():
            return False
        try:
            summary = read_json(path)
        except Exception:
            return False
        if not isinstance(summary, dict):
            return False
        if not phase_batch_matches(summary, batch_id):
            return False
        for field in required:
            if not summary.get(field):
                return False
    return True