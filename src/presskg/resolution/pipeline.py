"""Pipeline completa per Entity ed Event Resolution.

Fasi (ordine di esecuzione):
  identity   — profili, alias, clustering, gate e readiness delle identità
  projection — dry-run, safety gate, materializzazione entità e fact canonici
  predicates — audit di qualità, grounding e overlay dei predicati canonici
  events     — consolidamento, inventario evidenze, proiezione e audit eventi

Ogni fase è un subpackage di ``presskg.resolution`` ed espone il contratto
``run(args, state) -> state``; internamente esegue in sequenza gli step del
package, ognuno con contratto ``run(ctx) -> StepResult`` (Stadio B). Gli step
si scambiano i dati tramite file nella directory ``--out``
(default ``output/resolution``), come da convenzione dei moduli
preesistenti.

Semantica del dry-run e del riuso: gli step sono write-once; ``identity``
e ``projection`` girano in dry-run e ``projection`` produce il piano di
materializzazione verificato (i controlli identita' dipendenti dal grafo
sono saltati e rieseguiti con ``--apply``); ``predicates`` ed ``events``
operano sul grafo canonico MATERIALIZZATO e vengono pertanto saltate in
dry-run: per eseguirle serve ``--apply``.

Riuso su un ``--out`` gia' popolato: ogni fase rileva i propri artefatti
finali (marker) e li confronta col batch atteso (``--batch-id`` o derivato
dai modelli); se completi e coerenti la fase si salta riusando i risultati
 gia' prodotti. In particolare ``--apply`` dopo un dry-run nello stesso
``--out`` esegue solo i due step di materializzazione (idempotenti) e
prosegue con ``predicates`` ed ``events``. Uno stato PARZIALE (fasi
incomplete o batch incoerente) non e' riutilizzabile e la pipeline si
ferma subito (fail-fast iniziale): usare un ``--out`` nuovo.

Uso:
    python -m presskg.resolution.pipeline --phase all
    python -m presskg.resolution.pipeline --phase identity --apply
    python -m presskg.resolution.pipeline --phase predicates --verbose --save-state

oppure tramite CLI generale:

    python -m presskg.cli pipeline --phase identity
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path

PHASES = ["identity", "projection", "predicates", "events"]


def run_phase(phase: str, args: argparse.Namespace, state: dict) -> dict:
    """Importa il package della fase e ne esegue ``run(args, state)``.

    Fail-fast: un'eccezione in una fase interrompe la pipeline, le fasi
    successive dipendono dagli output di quella fallita.
    """
    print(f"\n=== PHASE {phase.upper()} ({'APPLY' if getattr(args, 'apply', False) else 'DRY-RUN'}) ===")
    mod = importlib.import_module(f"presskg.resolution.{phase}")
    run_fn = getattr(mod, "run", None)
    if not callable(run_fn):
        raise RuntimeError(f"SKIP: {phase} (il package non espone run(args, state))")
    return run_fn(args, state)


def run(args: argparse.Namespace, state: dict | None = None) -> dict:
    """Esegue le fasi richieste e restituisce lo stato finale della pipeline.

    Stadio A: la pipeline apre un unico driver Neo4j condiviso e lo attacca
    ad ``args``; gli step lo riusano via ``_common.step_driver(args)`` e
    aprono il proprio solo quando eseguiti da soli.
    """
    state = {} if state is None else state
    Path(args.out).mkdir(parents=True, exist_ok=True)
    phases = PHASES if args.phase == "all" else [args.phase]

    # fail-fast per fase: gli step sono write-once; artefatti incompleti o
    # incoerenti nel --out farebbero fallire la fase a meta' corsa (dopo
    # aver gia' speso le chiamate LLM della fase identity). Uno stato
    # COMPLETO e coerente col batch e' invece riusabile: le fasi lo
    # rilevano e si saltano (riuso), vedi i runner di fase.
    from presskg.resolution import paths as resolution_paths
    from presskg.resolution import events, identity, predicates, projection
    from presskg.resolution._common import phase_reusable

    phase_markers = {
        "identity": identity.REUSE_MARKERS,
        # in dry-run e' sufficiente la pianificazione completa: gli step
        # di materializzazione riscrivono solo i propri summary ({mode})
        "projection": projection.PLANNING_MARKERS,
        "predicates": predicates.REUSE_MARKERS,
        "events": events.REUSE_MARKERS,
    }
    for phase in phases:
        if (
            not getattr(args, "apply", False)
            and phase in ("predicates", "events")
        ):
            continue  # saltate in dry-run: i loro artefatti non verranno toccati
        pkg = {
            "identity": identity,
            "projection": projection,
            "predicates": predicates,
            "events": events,
        }[phase]
        stale = sorted(
            key
            for key in (
                step.__name__.rsplit(".", 1)[-1]
                for _, step, *_ in pkg.STEPS
            )
            if resolution_paths.step_dir(args, key).exists()
        )
        if stale and not phase_reusable(args, phase_markers[phase]):
            raise RuntimeError(
                f"Artefatti della fase {phase} in {args.out!r} "
                f"({', '.join(stale[:6])}"
                f"{' ...' if len(stale) > 6 else ''}) non riusabili: batch "
                f"diverso o catena interrotta; usare un --out nuovo "
                f"oppure rimuoverli prima di rieseguire"
            )

    driver = None
    try:
        from presskg.common.db import open_driver
        driver = open_driver()
        args.driver = driver
    except Exception:
        print("[pipeline] Neo4j non raggiungibile in pipeline.run: "
              "gli step apriranno il driver autonomamente")

    try:
        for phase in phases:
            # predicates ed events operano sul grafo canonico MATERIALIZZATO:
            # in dry-run non esiste, e la fase non ha data-plane. Il dry-run
            # verifica fino al piano di materializzazione (projection),
            # per proseguire serve --apply.
            if (
                phase in ("predicates", "events")
                and not getattr(args, "apply", False)
            ):
                print(
                    f"\n=== PHASE {phase.upper()} SALTATA (DRY-RUN) ===\n"
                    f"[pipeline] la fase {phase} opera sul grafo canonico "
                    f"materializzato: rieseguire con --apply"
                )
                state[phase] = {"skipped_dry_run": True}
                continue

            state = run_phase(phase, args, state)
            if getattr(args, "save_state", False):
                path = Path(args.out) / f"{phase}_state.json"
                path.write_text(
                    json.dumps(state, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
                print(f"[state] salvato {path}")
    finally:
        if driver is not None:
            driver.close()

    print(f"\nPipeline completata. State keys: {sorted(state.keys())}")
    return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pipeline di Entity ed Event Resolution (dry-run di default)"
    )
    parser.add_argument(
        "--phase",
        choices=["all"] + PHASES,
        default="all",
        help="Fase da eseguire, o 'all' per l'intera pipeline",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Applica le modifiche al database (default: dry-run)",
    )
    parser.add_argument(
        "--out",
        default="output/resolution",
        help="Directory di output della pipeline (default: output/resolution)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Output verboso e snapshot di debug per fase e step",
    )
    parser.add_argument(
        "--save-state",
        action="store_true",
        help="Salva lo stato JSON dopo ogni fase (default: disattivo)",
    )
    parser.add_argument(
        "--batch-id",
        default=None,
        help=(
            "Batch ValidatedFact della resolution "
            "(default: derivato dai modelli configurati)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> dict:
    args = build_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    main()
