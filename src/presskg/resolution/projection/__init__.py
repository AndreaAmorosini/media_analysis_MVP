"""Fase projection: materializzazione del grafo canonico.

Step in sequenza con contratto ``run(ctx) -> StepResult`` (Stadio B) e
artefatti nel registry ``presskg.resolution.paths`` (Stadio A). Fail-fast.
"""

from argparse import Namespace

from presskg.resolution._common import phase_reusable, run_steps

from . import (
    dry_run, safety_gate, entity_materialization, fact_projection, fact_materialization,
)

# (nome snapshot, modulo, reads, override per-step)
# Le conferme dedicate (CONFIRM di ogni step di materializzazione) vengono
# fornite dalla pipeline: ``--apply`` della pipeline e' la conferma esplicita
# dell'utente per la scrittura del grafo; in esecuzione standalone gli step
# restano protetti dal proprio --confirm.
STEPS = [
    ("00_dry_run", dry_run, ["readiness_refinement"]),
    ("01_safety_gate", safety_gate, ["dry_run"]),
    ("02_entity_materialization", entity_materialization,
     ["dry_run", "safety_gate"],
     {"confirm": entity_materialization.CONFIRM}),
    ("03_fact_projection", fact_projection, ["dry_run", "safety_gate"]),
    ("04_fact_materialization", fact_materialization,
     ["safety_gate", "fact_projection"],
     {"confirm": fact_materialization.CONFIRM}),
]

# Artefatti di pianificazione (fase completa senza scrittura del grafo) e
# di applicazione (entita' e fact canonici materializzati). Il summary di
# materializzazione viene scritto anche quando lo step fallisce: il campo
# ``complete`` e' l'esito che distingue un apply riuscito.
PLANNING_MARKERS = [("fact_projection", "summary.json")]
APPLIED_MARKERS = [
    ("entity_materialization", "apply_summary.json", "complete"),
    ("fact_materialization", "apply_summary.json", "complete"),
]
# Sottoinsieme da eseguire in --apply quando la pianificazione e' gia'
# completa: solo i due step di materializzazione (idempotenti: MERGE)
MATERIALIZATION_STEPS = [s for s in STEPS if s[0].endswith("_materialization")]


def run(args: Namespace, state: dict) -> dict:
    """Esegue gli step della fase projection in sequenza.

    args: apply, out, verbose, save_state, driver (condiviso)
    state: dizionario con lo stato corrente della pipeline

    Riuso: con la pianificazione gia' completa (run dry-run precedente
    nello stesso ``--out``), gli step di pianificazione non vengono rifatti;
    con ``--apply`` si eseguono solo i due step di materializzazione
    (idempotenti), mentre in dry-run non c'è nulla da fare. Se anche la
    materializzazione e' gia' stata applicata, l'intera fase viene saltata.
    """
    if phase_reusable(args, PLANNING_MARKERS):
        if phase_reusable(args, APPLIED_MARKERS):
            print(
                "[projection] grafo canonico gia' materializzato con lo "
                "stesso batch nel --out: fase saltata (riuso del run precedente)"
            )
            state["projection"] = {"reused_artifacts": True}
            return state
        if getattr(args, "apply", False):
            print(
                "[projection] pianificazione gia' completa: esecuzione dei "
                "soli step di materializzazione (entita' e fact canonici)"
            )
            return run_steps("projection", MATERIALIZATION_STEPS, args, state)
        print(
            "[projection] pianificazione gia' completa nel --out: fase "
            "saltata (riuso del run precedente; --apply per materializzare)"
        )
        state["projection"] = {"reused_plan": True}
        return state
    return run_steps("projection", STEPS, args, state)
