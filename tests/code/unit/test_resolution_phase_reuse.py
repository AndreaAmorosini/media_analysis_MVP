"""Riuso degli artefatti di fase (semantica dry-run -> apply nello stesso --out).

La pipeline deve permettere il flusso:
    pixi run pipeline --out X --save-state        (dry-run)
    pixi run pipeline --out X --apply --save-state (riuso + materializzazione)

Ogni fase rileva i propri artefatti finali (marker) e, se completi e coerenti
col batch atteso, si salta; la fase projection in --apply esegue solo i due
step di materializzazione quando la pianificazione e' gia' completa.
Uno stato parziale o incoerente deve invece fermare subito la pipeline.
"""

import importlib
import json
from argparse import Namespace

import pytest

from presskg.resolution import paths, pipeline
from presskg.resolution._common import phase_batch_matches, phase_reusable

BATCH = "qwen35-4b-judge-test"
BATCH_KEY = f"validated-fact-view-v1:{BATCH}"
OTHER_BATCH = "other-model-batch"


def make_args(tmp_path, apply=False, batch_id=BATCH):
    return Namespace(
        phase="all",
        apply=apply,
        out=str(tmp_path),
        verbose=False,
        save_state=False,
        batch_id=batch_id,
        driver=None,
    )


def write_marker(args, step_key, filename, summary):
    path = paths.step_file(args, step_key, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary), encoding="utf-8")
    return path


# ------------------------------------------------------------------ helper

def test_phase_batch_matches_source_key():
    assert phase_batch_matches({"source_batch_key": BATCH_KEY}, BATCH)
    assert not phase_batch_matches({"source_batch_key": BATCH_KEY}, OTHER_BATCH)


def test_phase_batch_matches_source_key_list():
    summary = {"source_batch_keys": [f"view:{OTHER_BATCH}", BATCH_KEY]}
    assert phase_batch_matches(summary, BATCH)
    assert not phase_batch_matches(summary, "third-batch")


def test_phase_batch_matches_without_batch_keys():
    # summary senza chiavi di batch: vale la sola esistenza
    assert phase_batch_matches({"status": "PASS"}, BATCH)


def test_phase_reusable_missing_marker(tmp_path):
    assert not phase_reusable(make_args(tmp_path), ("readiness_refinement", "summary.json"))


def test_phase_reusable_matching_and_mismatching(tmp_path):
    args = make_args(tmp_path)
    write_marker(args, "readiness_refinement", "summary.json",
                 {"source_batch_key": BATCH_KEY})
    assert phase_reusable(args, ("readiness_refinement", "summary.json"))
    write_marker(args, "readiness_refinement", "summary.json",
                 {"source_batch_key": f"view:{OTHER_BATCH}"})
    assert not phase_reusable(args, ("readiness_refinement", "summary.json"))


def test_phase_reusable_unreadable_marker(tmp_path):
    args = make_args(tmp_path)
    marker = write_marker(args, "readiness_refinement", "summary.json",
                          {"source_batch_key": BATCH_KEY})
    marker.write_text("{non json", encoding="utf-8")
    assert not phase_reusable(args, ("readiness_refinement", "summary.json"))


def test_phase_reusable_multiple_markers(tmp_path):
    args = make_args(tmp_path)
    markers = [("entity_materialization", "apply_summary.json", "complete"),
               ("fact_materialization", "apply_summary.json", "complete")]
    write_marker(args, "entity_materialization", "apply_summary.json",
                 {"source_batch_key": BATCH_KEY, "complete": True})
    assert not phase_reusable(args, markers)
    write_marker(args, "fact_materialization", "apply_summary.json",
                 {"source_batch_key": BATCH_KEY, "complete": True})
    assert phase_reusable(args, markers)


def test_phase_reusable_requires_outcome_field(tmp_path):
    # il summary di materializzazione e' scritto nel finally anche quando
    # lo step fallisce: senza il campo esito un apply fallito sarebbe
    # scambiato per riuscito
    args = make_args(tmp_path)
    markers = [("fact_materialization", "apply_summary.json", "complete")]
    write_marker(args, "fact_materialization", "apply_summary.json",
                 {"source_batch_key": BATCH_KEY, "graph_writes": 0})
    assert not phase_reusable(args, markers)
    write_marker(args, "fact_materialization", "apply_summary.json",
                 {"source_batch_key": BATCH_KEY, "complete": True})
    assert phase_reusable(args, markers)


def test_phase_reusable_batch_id_override(tmp_path):
    # il batch atteso e' esplicito: nessuna lettura di config
    args = make_args(tmp_path, batch_id=OTHER_BATCH)
    write_marker(args, "readiness_refinement", "summary.json",
                 {"source_batch_key": f"view:{OTHER_BATCH}"})
    assert phase_reusable(args, ("readiness_refinement", "summary.json"),
                          batch_id=OTHER_BATCH)


# ------------------------------------------------------------- fasi: reuse

def test_identity_run_skips_when_reusable(tmp_path, monkeypatch):
    identity = importlib.import_module("presskg.resolution.identity")
    args = make_args(tmp_path)
    write_marker(args, "readiness_refinement", "summary.json",
                 {"source_batch_key": BATCH_KEY})

    def boom(*a, **k):
        raise AssertionError("identity riusabile: run_steps non deve girare")

    monkeypatch.setattr(identity, "run_steps", boom)
    state = identity.run(args, {})
    assert state["identity"] == {"reused_artifacts": True}


def test_identity_run_mismatched_batch_runs(tmp_path, monkeypatch):
    identity = importlib.import_module("presskg.resolution.identity")
    args = make_args(tmp_path)
    write_marker(args, "readiness_refinement", "summary.json",
                 {"source_batch_key": f"view:{OTHER_BATCH}"})
    monkeypatch.setattr(identity, "run_steps", lambda phase, steps, ns, st: st)
    identity.run(args, {})
    assert True


def test_projection_reuse_plan_dry_run(tmp_path, monkeypatch):
    projection = importlib.import_module("presskg.resolution.projection")
    args = make_args(tmp_path)  # dry-run
    write_marker(args, "fact_projection", "summary.json",
                 {"source_batch_key": BATCH_KEY})

    def boom(*a, **k):
        raise AssertionError("pianificazione completa: run_steps non deve girare")

    monkeypatch.setattr(projection, "run_steps", boom)
    state = projection.run(args, {})
    assert state["projection"] == {"reused_plan": True}


def test_projection_apply_runs_only_materialization(tmp_path, monkeypatch):
    projection = importlib.import_module("presskg.resolution.projection")
    args = make_args(tmp_path, apply=True)
    write_marker(args, "fact_projection", "summary.json",
                 {"source_batch_key": BATCH_KEY})

    captured = {}

    def fake_run_steps(phase, steps, ns, st):
        captured["steps"] = [name for name, *_ in steps]
        st[phase] = {"ok": True}
        return st

    monkeypatch.setattr(projection, "run_steps", fake_run_steps)
    projection.run(args, {})
    assert captured["steps"] == [
        "02_entity_materialization",
        "04_fact_materialization",
    ]


def test_projection_apply_skip_when_already_applied(tmp_path, monkeypatch):
    projection = importlib.import_module("presskg.resolution.projection")
    args = make_args(tmp_path, apply=True)
    write_marker(args, "fact_projection", "summary.json",
                 {"source_batch_key": BATCH_KEY})
    write_marker(args, "entity_materialization", "apply_summary.json",
                 {"source_batch_key": BATCH_KEY, "complete": True})
    write_marker(args, "fact_materialization", "apply_summary.json",
                 {"source_batch_key": BATCH_KEY, "complete": True})

    def boom(*a, **k):
        raise AssertionError("grafo gia' materializzato: run_steps non deve girare")

    monkeypatch.setattr(projection, "run_steps", boom)
    state = projection.run(args, {})
    assert state["projection"] == {"reused_artifacts": True}


def test_projection_apply_reruns_after_failed_materialization(tmp_path, monkeypatch):
    # entity_mat applicato, fact_mat fallito (summary senza complete):
    # il riuso di apply deve rieseguire ENTRAMBI gli step idempotenti
    projection = importlib.import_module("presskg.resolution.projection")
    args = make_args(tmp_path, apply=True)
    write_marker(args, "fact_projection", "summary.json",
                 {"source_batch_key": BATCH_KEY})
    write_marker(args, "entity_materialization", "apply_summary.json",
                 {"source_batch_key": BATCH_KEY, "complete": True})
    write_marker(args, "fact_materialization", "apply_summary.json",
                 {"source_batch_key": BATCH_KEY, "graph_writes": 0})

    captured = {}

    def fake_run_steps(phase, steps, ns, st):
        captured["steps"] = [name for name, *_ in steps]
        st[phase] = {"ok": True}
        return st

    monkeypatch.setattr(projection, "run_steps", fake_run_steps)
    projection.run(args, {})
    assert captured["steps"] == [
        "02_entity_materialization",
        "04_fact_materialization",
    ]


def test_projection_fresh_out_runs_all_steps(tmp_path, monkeypatch):
    projection = importlib.import_module("presskg.resolution.projection")
    args = make_args(tmp_path, apply=True)

    captured = {}

    def fake_run_steps(phase, steps, ns, st):
        captured["steps"] = [name for name, *_ in steps]
        st[phase] = {"ok": True}
        return st

    monkeypatch.setattr(projection, "run_steps", fake_run_steps)
    projection.run(args, {})
    assert captured["steps"] == [name for name, *_ in projection.STEPS]


def test_predicates_run_skips_when_reusable(tmp_path, monkeypatch):
    predicates = importlib.import_module("presskg.resolution.predicates")
    args = make_args(tmp_path, apply=True)
    write_marker(args, "overlay", "summary.json",
                 {"operation": "APPLY", "applied": {"refined_relations": 1}})

    def boom(*a, **k):
        raise AssertionError("overlay gia' applicato: run_steps non deve girare")

    monkeypatch.setattr(predicates, "run_steps", boom)
    state = predicates.run(args, {})
    assert state["predicates"] == {"reused_artifacts": True}


def test_events_run_skips_when_reusable(tmp_path, monkeypatch):
    events = importlib.import_module("presskg.resolution.events")
    args = make_args(tmp_path, apply=True)
    write_marker(args, "consolidation", "summary.json",
                 {"source_batch_keys": [BATCH_KEY]})
    write_marker(args, "graph_audit", "summary.json", {"status": "PASS"})

    def boom(*a, **k):
        raise AssertionError("audit gia' completato: run_steps non deve girare")

    monkeypatch.setattr(events, "run_steps", boom)
    state = events.run(args, {})
    assert state["events"] == {"reused_artifacts": True}


# ------------------------------------------------------ pipeline: fail-fast

def test_pipeline_fail_fast_partial_identity(tmp_path):
    args = make_args(tmp_path)
    # run interrotto a meta' identity: baseline presente, marker finale no
    paths.step_dir(args, "baseline").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="identity"):
        pipeline.run(args)


def test_pipeline_fail_fast_partial_projection(tmp_path):
    args = make_args(tmp_path)
    # identity riusabile ma pianificazione interrotta
    write_marker(args, "readiness_refinement", "summary.json",
                 {"source_batch_key": BATCH_KEY})
    paths.step_dir(args, "dry_run").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="projection"):
        pipeline.run(args)


def test_pipeline_fail_fast_batch_mismatch(tmp_path):
    args = make_args(tmp_path)
    write_marker(args, "readiness_refinement", "summary.json",
                 {"source_batch_key": f"view:{OTHER_BATCH}"})
    with pytest.raises(RuntimeError, match="batch"):
        pipeline.run(args)


def test_pipeline_dry_run_reuses_identity_and_plan(tmp_path):
    args = make_args(tmp_path)  # dry-run, save_state=False
    write_marker(args, "readiness_refinement", "summary.json",
                 {"source_batch_key": BATCH_KEY})
    write_marker(args, "fact_projection", "summary.json",
                 {"source_batch_key": BATCH_KEY})
    state = pipeline.run(args)
    assert state["identity"] == {"reused_artifacts": True}
    assert state["projection"] == {"reused_plan": True}
    assert state["predicates"] == {"skipped_dry_run": True}
    assert state["events"] == {"skipped_dry_run": True}


def test_pipeline_dry_run_skips_predicates_events_stale_check(tmp_path):
    # le fasi saltate in dry-run non vanno in fail-fast per artefatti stale
    args = make_args(tmp_path)
    write_marker(args, "readiness_refinement", "summary.json",
                 {"source_batch_key": BATCH_KEY})
    write_marker(args, "fact_projection", "summary.json",
                 {"source_batch_key": BATCH_KEY})
    paths.step_dir(args, "overlay").mkdir(parents=True)  # stale predicates
    state = pipeline.run(args)
    assert state["predicates"] == {"skipped_dry_run": True}
