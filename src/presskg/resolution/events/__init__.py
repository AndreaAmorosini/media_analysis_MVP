"""Fase events: consolidamento e proiezione degli eventi canonici.

Step in sequenza con contratto ``run(ctx) -> StepResult`` (Stadio B);
leggono e scrivono direttamente su Neo4j (driver condiviso dalla pipeline,
Stadio A). Fail-fast.
"""

from argparse import Namespace

from presskg.resolution._common import phase_reusable, run_steps

from . import consolidation, evidence_inventory, event_projection, validated_evidence, graph_audit

# (nome snapshot, modulo, reads)
STEPS = [
    ("00_consolidation", consolidation, []),
    ("01_evidence_inventory", evidence_inventory, []),
    ("02_event_projection", event_projection, []),
    ("03_validated_evidence", validated_evidence, []),
    ("04_graph_audit", graph_audit, []),
]

# Marker di riuso: graph_audit chiude la fase; consolidation riporta i
# batch consolidati per la verifica di coerenza col batch atteso
REUSE_MARKERS = [("consolidation", "summary.json"), ("graph_audit", "summary.json")]


def run(args: Namespace, state: dict) -> dict:
    """Esegue gli step della fase events in sequenza.

    args: apply, out, verbose, save_state, driver (condiviso)
    state: dizionario con lo stato corrente della pipeline

    Riuso: la fase esiste solo in --apply; se l'audit finale esiste gia'
    nel ``--out`` con gli stessi batch consolidati, la fase viene saltata.
    """
    if phase_reusable(args, REUSE_MARKERS):
        print(
            "[events] audit del grafo canonico gia' completato con gli "
            "stessi batch nel --out: fase saltata (riuso del run precedente)"
        )
        state["events"] = {"reused_artifacts": True}
        return state
    return run_steps("events", STEPS, args, state)
