"""Registry centrale dei path degli artefatti di resolution.

Unica fonte delle directory di step (Stadio A): il layout su disco resta
identico a quello storico (`{out}/{nome-step})`, ma derivato da `--out`
della pipeline invece che hardcodato nei singoli moduli. Ogni step deve
usare ``paths.step_dir/step_file`` al posto dei literal
``"output/resolution/..."`` (imposto da
``tests/code/regression/test_stage_a_paths.py``).

Scelte deliberate (sessione P5):
- [D1] il producer mantiene il nome storico: ``splitting`` scrive
  ``mention-decomposition-v1`` ed è il consumer ``contextual_evidence``
  ad allinearsi;
- [D2] la fase predicates è cablata per run end-to-end complete: catena
  canonica ``full``; le varianti ``-sample*`` non esistono più come default.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

ROOT_DEFAULT = "output/resolution"

# step key -> nome della sottodirectory degli artefatti
STEP_DIRS = {
    # identity
    "baseline": "baseline-v1",
    "profiles": "identity-profiles-v1",
    "referent_eligibility": "referent-eligibility-v1",
    "attributes": "identity-attributes-v1",
    "attribute_grounding": "attribute-grounding-v1",
    "relation_scope": "relation-type-scope-v1",
    "splitting": "mention-decomposition-v1",
    "contextual_evidence": "mention-contextual-evidence-v1",
    "semantic_decomposition": "mention-semantic-decomposition-v1",
    "candidate_generation": "candidate-generation-v1",
    "evidence_matching": "evidence-matching-v1",
    "contextual_alias": "contextual-alias-recovery-v1",
    "alias_index": "global-alias-index-v1",
    "clustering": "constrained-clustering-v1",
    "occurrences": "occurrence-assignment-v1",
    "contextual_refinement": "contextual-identity-refinement-v2",
    "coreference": "coreference-audit-v1",
    "identity_gate": "identity-integrity-gate-v1",
    "readiness": "projection-readiness-v1",
    "readiness_refinement": "projection-readiness-refinement-v1",
    # projection
    "dry_run": "canonical-projection-dry-run-v1",
    "safety_gate": "materialization-safety-gate-v1",
    "entity_materialization": "canonical-materialization-v1",
    "fact_projection": "canonical-fact-projection-v1",
    "fact_materialization": "canonical-fact-materialization-v1",
    # predicates (catena canonica "full", Stadio A / D2)
    "quality_audit": "predicate-quality-audit-v1",
    "evidence_grounding": "predicate-evidence-grounding-v1",
    "candidate_generation": "predicate-candidates-v1-full",
    "candidate_recovery": "predicate-candidate-recovery-v1-full-recovered",
    "semantic_verification": "predicate-verification-v1-full",
    "consistency_audit": "predicate-consistency-audit-v1-full",
    "overlay": "canonical-predicate-overlay-v1",
    # events
    "consolidation": "canonical-graph-consolidation-v1",
    "evidence_inventory": "event-evidence-inventory-v1",
    "event_projection": "canonical-event-projection-v1",
    "validated_evidence": "validated-event-evidence-v1",
    "graph_audit": "canonical-graph-audit-v1",
}


def root(args: Namespace | None = None) -> Path:
    if args is None:
        return Path(ROOT_DEFAULT)
    return Path(getattr(args, "out", ROOT_DEFAULT))


def step_dir(args: Namespace, step: str) -> Path:
    return root(args) / STEP_DIRS[step]


def step_file(args: Namespace, step: str, name: str) -> Path:
    return step_dir(args, step) / name


def baseline_summary(args: Namespace) -> Path:
    """Percorso del baseline usato da profiles e splitting."""
    return step_file(args, "baseline", "baseline_summary.json")
