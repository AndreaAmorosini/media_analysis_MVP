"""Fase predicates: refining dei predicati del grafo canonico.

Catena canonica "full" per run end-to-end (decisione D2 dello Stadio A):
candidate_generation (-full) -> candidate_recovery -> semantic_verification
(legge il recovery, scrive verification-v1-full) -> consistency_audit
-> overlay. Le varianti "-sample" non esistono più come default. Step con
contratto ``run(ctx) -> StepResult`` (Stadio B). Fail-fast.
"""

from argparse import Namespace

from presskg.resolution._common import phase_reusable, run_steps

from . import (
    quality_audit, evidence_grounding, candidate_generation,
    candidate_recovery, semantic_verification, consistency_audit, overlay,
)

# (nome snapshot, modulo, reads)
STEPS = [
    ("00_quality_audit", quality_audit, []),
    ("01_evidence_grounding", evidence_grounding, ["quality_audit"]),
    ("02_candidate_generation", candidate_generation, ["evidence_grounding"]),
    ("03_candidate_recovery", candidate_recovery, ["candidate_generation"]),
    ("04_semantic_verification", semantic_verification, ["candidate_recovery"]),
    ("05_consistency_audit", consistency_audit,
     ["candidate_generation", "semantic_verification"]),
    ("06_overlay", overlay, ["candidate_generation", "semantic_verification"]),
]


# Marker di riuso: l'overlay e' l'artefatto finale della fase; il campo
# ``applied`` e' scritto solo quando l'operazione sul grafo e' stata
# eseguita fino in fondo (esito), mentre il summary senza il campo
# appartiene a un run fallito
REUSE_MARKERS = [("overlay", "summary.json", "applied")]


def run(args: Namespace, state: dict) -> dict:
    """Esegue gli step della fase predicates in sequenza.

    args: apply, out, verbose, save_state, driver (condiviso)
    state: dizionario con lo stato corrente della pipeline

    Riuso: la fase esiste solo in --apply; se l'overlay esiste gia' nel
    ``--out`` con lo stesso batch, l'applicazione e' gia' avvenuta e la
    fase viene saltata.
    """
    if phase_reusable(args, REUSE_MARKERS):
        print(
            "[predicates] overlay gia' applicato (artefatti completi "
            "nel --out): fase saltata (riuso del run precedente)"
        )
        state["predicates"] = {"reused_artifacts": True}
        return state
    return run_steps("predicates", STEPS, args, state)
