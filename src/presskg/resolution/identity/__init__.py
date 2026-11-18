"""Fase identity: profili, alias, clustering, occurrence e gate di identità.

Esegue in sequenza gli step del package; ogni step espone il contratto
``run(ctx) -> StepResult`` (Stadio B, con retrocompatibilità bool gestita da
``presskg.resolution._common.run_steps``) e consuma/produce artefatti nelle
directory dichiarate in ``presskg.resolution.paths`` (Stadio A). Fail-fast:
il primo step in errore interrompe la fase, i successivi dipendono dai suoi
output.

L'ordine degli step è topologico rispetto alle dipendenze degli artifact
(verificato da tests/code/regression/test_stage_a_paths.py).
"""

from argparse import Namespace

from presskg.resolution._common import phase_reusable, run_steps

from . import (
    alias_index, candidate_generation, clustering, occurrences,
    contextual_refinement, contextual_evidence, contextual_alias,
    coreference, evidence_matching, identity_gate,
    readiness, readiness_refinement, profiles, attributes,
    attribute_grounding, referent_eligibility, relation_scope,
    semantic_decomposition, splitting, baseline,
)

# (nome snapshot, modulo, reads). reads = chiavi paths.Step_DIRS degli
# artefatti in ingresso (step precedenti della fase o fasi antecedenti);
# l'output di uno step è la sua stessa chiave nel registry.
STEPS = [
    ("00_baseline", baseline, []),
    ("01_splitting", splitting, ["baseline"]),
    ("02_contextual_evidence", contextual_evidence, ["splitting"]),
    ("03_semantic_decomposition", semantic_decomposition, ["contextual_evidence"]),
    ("04_referent_eligibility", referent_eligibility, ["semantic_decomposition"]),
    ("05_profiles", profiles, ["baseline", "referent_eligibility"]),
    ("06_attributes", attributes, ["profiles"]),
    ("07_attribute_grounding", attribute_grounding, ["attributes"]),
    ("08_relation_scope", relation_scope, ["attribute_grounding"]),
    ("09_candidate_generation", candidate_generation, ["profiles"]),
    ("10_evidence_matching", evidence_matching,
     ["candidate_generation", "relation_scope", "profiles"]),
    ("11_contextual_alias", contextual_alias,
     ["candidate_generation", "evidence_matching", "profiles"]),
    ("12_alias_index", alias_index,
     ["candidate_generation", "contextual_alias", "profiles"]),
    ("13_clustering", clustering,
     ["alias_index", "candidate_generation", "contextual_alias"]),
    ("14_occurrences", occurrences,
     ["clustering", "candidate_generation", "profiles"]),
    ("15_contextual_refinement", contextual_refinement, ["occurrences", "profiles"]),
    ("16_coreference", coreference,
     ["occurrences", "contextual_refinement", "profiles"]),
    ("17_identity_gate", identity_gate,
     ["occurrences", "coreference", "contextual_refinement", "profiles"]),
    ("18_readiness", readiness, ["identity_gate", "profiles"]),
    ("19_readiness_refinement", readiness_refinement,
     ["identity_gate", "readiness", "profiles"]),
]


# Marker di riuso: artefatti finali della fase (tutti obbligatori per
# dichiararla completa e coerente col batch atteso)
REUSE_MARKERS = [("readiness_refinement", "summary.json")]


def run(args: Namespace, state: dict) -> dict:
    """Esegue gli step della fase identity in sequenza.

    args: apply, out, verbose, save_state, driver (condiviso dalla pipeline)
    state: dizionario con lo stato corrente della pipeline

    Riuso: se gli artefatti finali della fase esistono gia' nel ``--out``
    e sono coerenti col batch atteso, la fase viene saltata: gli step sono
    write-once e i risultati sono pienamente riusabili (la fase identity
    non scrive sul grafo).
    """
    if phase_reusable(args, REUSE_MARKERS):
        print(
            "[identity] artefatti completi e coerenti col batch nel --out: "
            "fase saltata (riuso del run precedente)"
        )
        state["identity"] = {"reused_artifacts": True}
        return state
    return run_steps("identity", STEPS, args, state)
