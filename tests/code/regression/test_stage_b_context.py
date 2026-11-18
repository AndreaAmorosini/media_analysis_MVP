"""Regression test del contratto Stadio B: StepContext / StepResult / run_steps.

Verifica che:
- ogni step della fase dichiari davvero ``run(ctx) -> StepResult``;
- il runner unificato accetti sia StepResult sia plain bool;
- StepContext forwardi gli attributi sconosciuti verso args;
- il runner sia fail-fast e raccolga gli esiti in ``state[phase]``;
- StepResult si comporti correttamente in contesto booleano.

Non tocca Neo4j né il filesystem di output: usa step finti.
"""

import inspect
from argparse import Namespace
from types import ModuleType

import pytest

from presskg.resolution import identity, predicates, projection, events
from presskg.resolution._common import run_steps
from presskg.resolution.context import StepContext, StepResult

PHASE_PACKAGES = [identity, predicates, projection, events]


def _steps_phases():
    return [(mod.__name__.rsplit(".", 1)[-1], mod) for mod in PHASE_PACKAGES]


# ============================================================
# Contratto della firma (Stadio B)
# ============================================================

@pytest.mark.parametrize("phase_name,mod", _steps_phases())
def test_step_signature_ctx_stepresult(phase_name, mod):
    """Ogni step dichiarato ha firma run(ctx) con ritorno annotato StepResult."""
    for _, step, *_ in mod.STEPS:
        params = inspect.signature(step.run).parameters
        assert list(params) == ["ctx"], (
            f"{step.__name__}.run deve avere firma (ctx), trovata {tuple(params)}"
        )
        assert "StepResult" in str(step.run.__annotations__.get("return", "")), (
            f"{step.__name__}.run deve restituire StepResult"
        )


@pytest.mark.parametrize("phase_name,mod", _steps_phases())
def test_phases_use_unified_runner(phase_name, mod):
    """I runner di fase invocano _common.run_steps invece di loop locali."""
    import presskg.resolution._common as common
    source = inspect.getsource(mod)
    assert "run_steps(" in source, (
        f"{mod.__name__} deve delegare a _common.run_steps"
    )
    assert "for name, step" not in source, (
        f"{mod.__name__} non deve contenere loop di step locali"
    )


# eccezione documentata: dispatch di sottocomandi, nessun summary in run()
STEPS_WITHOUT_SUMMARY = {"semantic_decomposition"}


@pytest.mark.parametrize("phase_name,mod", _steps_phases())
def test_steps_return_counts_from_summary(phase_name, mod):
    """Ogni step popola i counts del StepResult dal proprio summary.

    Il return conclusivo di run() passa per ``StepResult.from_summary``
    (o ``ok_result`` con counts espliciti) e il path di errore riporta
    il messaggio dell'eccezione catturata.
    """
    import re as _re
    for _, step, *_ in mod.STEPS:
        name = step.__name__.rsplit(".", 1)[-1]
        source = inspect.getsource(step.run)
        returns = _re.findall(
            r"return StepResult\.(from_summary|ok_result|fail)\(", source
        )
        if name not in STEPS_WITHOUT_SUMMARY:
            assert "from_summary" in returns, (
                f"{step.__name__} deve popolare i counts via "
                f"StepResult.from_summary"
            )
        assert "fail" in returns, (
            f"{step.__name__} deve avere un path di errore StepResult.fail()"
        )
        # il path di errore riporta l'eccezione (type + messaggio)
        assert _re.search(
            r"StepResult\.fail\(f\"\{type\((e|exc)\).__name__\}: \{\1\}\"\)",
            source,
        ), f"{step.__name__}.fail deve riportare tipo e messaggio dell'eccezione"


def test_all_steps_have_result_import():
    """Ogni modulo step importa StepResult (usato nel contratto)."""
    import importlib
    for _, mod in _steps_phases():
        for _, step, *_ in mod.STEPS:
            m = importlib.import_module(step.__name__)
            assert hasattr(m, "StepContext"), (
                f"{step.__name__} deve importare StepContext"
            )
            assert hasattr(m, "StepResult"), (
                f"{step.__name__} deve importare StepResult"
            )


# ============================================================
# StepContext
# ============================================================

class TestStepContext:

    def test_forwarding_args(self):
        args = Namespace(out="out_root_x", verbose=True)
        ctx = StepContext(args=args, phase="identity", step="00_baseline")
        assert ctx.out == "out_root_x"
        assert ctx.verbose is True
        assert ctx.phase == "identity"
        assert ctx.step == "00_baseline"

    def test_forwarding_missing_raises(self):
        ctx = StepContext(args=Namespace(), phase="identity")
        with pytest.raises(AttributeError):
            _ = ctx.qualcosa_che_non_esiste

    def test_apply_opt_in(self):
        ctx = StepContext(args=Namespace(), phase="identity")
        assert ctx.apply is False
        ctx = StepContext(args=Namespace(apply=True), phase="identity")
        assert ctx.apply is True

    def test_step_dir_matches_registry(self, tmp_path):
        from presskg.resolution import paths
        args = Namespace(out=str(tmp_path))
        ctx = StepContext(args=args, phase="identity")
        assert ctx.step_dir("baseline") == paths.step_dir(args, "baseline")
        assert ctx.step_file("baseline", "x.json") == \
            paths.step_file(args, "baseline", "x.json")
        assert ctx.out_root() == paths.root(args)


# ============================================================
# StepResult
# ============================================================

class TestStepResult:

    def test_ok_result_counts(self):
        r = StepResult.ok_result(processati=3, scritti=1)
        assert r.ok is True
        assert r.counts == {"processati": 3, "scritti": 1}
        assert r.error is None
        assert r.warnings == []

    def test_fail_error(self):
        r = StepResult.fail("boom")
        assert r.ok is False
        assert r.error == "boom"
        assert not r

    def test_bool_truthiness(self):
        assert bool(StepResult.ok_result()) is True
        assert bool(StepResult.fail()) is False

    def test_from_summary_scalars_only(self):
        summary = {
            "version": "baseline-v1",
            "scanned_facts": 8375,
            "llm_calls": 12,
            "graph_writes": 0,
            "automatic_promotion": False,
            "checksums": {"a": "sha256"},
            "status_counts": {"PASS": 10, "HOLD": 2},
        }
        r = StepResult.from_summary(summary)
        assert r.ok is True
        assert r.counts == {
            "scanned_facts": 8375,
            "llm_calls": 12,
            "graph_writes": 0,
            "automatic_promotion": False,
        }, "solo le voci scalari entrano nei counts"

    def test_from_summary_explicit_counts_win(self):
        r = StepResult.from_summary(
            {"llm_calls": 5},
            llm_calls=99,
            extra=1,
        )
        assert r.counts == {"llm_calls": 99, "extra": 1}

    def test_from_summary_none(self):
        r = StepResult.from_summary(None)
        assert r.ok is True
        assert r.counts == {}


# ============================================================
# Runner unificato
# ============================================================

class TestRunSteps:

    def _mk(self, name, fn):
        """Crea un modulo step finto con __name__ e run()."""
        mod = ModuleType(f"presskg.resolution.identity.{name}")
        mod.run = fn
        return mod

    def _args(self, tmp_path):
        return Namespace(out=str(tmp_path), verbose=False, apply=False,
                         save_state=False)

    def test_stepresult_collected(self, tmp_path):
        def step_ok(ctx):
            assert isinstance(ctx, StepContext)
            assert ctx.phase == "identity"
            assert ctx.step == "00_ok"
            return StepResult.ok_result(enti=5)

        steps = [("00_ok", self._mk("ok", step_ok), [])]
        state = {}
        out = run_steps("identity", steps, self._args(tmp_path), state)
        assert out is state
        assert state["identity"]["00_ok"] == {
            "ok": True, "error": None, "counts": {"enti": 5},
            "warnings": [], "checks": {},
        }

    def test_plain_bool_backcompat(self, tmp_path):
        def step_bool(ctx):
            return True

        steps = [("00_ok", self._mk("bool", step_bool), [])]
        state = {}
        run_steps("identity", steps, self._args(tmp_path), state)
        assert state["identity"]["00_ok"] == {"ok": True}

    def test_fail_fast(self, tmp_path):
        calls = []

        def step_bad(ctx):
            calls.append("bad")
            return StepResult.fail("errore deliberato")

        def step_never(ctx):
            calls.append("never")
            return True

        steps = [
            ("00_bad", self._mk("bad", step_bad), []),
            ("01_never", self._mk("never", step_never), []),
        ]
        state = {}
        with pytest.raises(RuntimeError, match="00_bad"):
            run_steps("identity", steps, self._args(tmp_path), state)
        assert calls == ["bad"], "il runner deve essere fail-fast"
        # come in Stadio A: la fase fallita non committa i risultati in state
        assert "identity" not in state

    def test_bool_false_fail_fast(self, tmp_path):
        def step_bad(ctx):
            return False

        steps = [("00_bad", self._mk("bad", step_bad), [])]
        with pytest.raises(RuntimeError):
            run_steps("identity", steps, self._args(tmp_path), {})


# ============================================================
# Proprieta' del driver Neo4j e uscite pulite (chiusura P7)
# ============================================================

class TestDriverOwnership:

    class _FakeDriver:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    def test_shared_driver_never_closed_by_steps(self, monkeypatch):
        """Con il driver condiviso della pipeline, close_step_driver e' no-op."""
        from presskg.resolution import _common

        shared = self._FakeDriver()
        assert shared.closed is False

        args = Namespace(driver=shared)
        d = _common.step_driver(args)
        assert d is shared, "step_driver deve riusare il driver condiviso"
        assert getattr(args, "_own_driver", False) is False

        _common.close_step_driver(args)
        assert shared.closed is False, "il driver condiviso NON va chiuso"

    def test_own_driver_lifecycle(self, monkeypatch):
        """Da soli, lo step apre, usa e chiude il proprio driver."""
        from presskg.resolution import _common

        opened = []
        monkeypatch.setattr(
            _common, "open_driver",
            lambda: (opened.append(self._FakeDriver()), opened[-1])[1],
        )

        args = Namespace()
        d = _common.step_driver(args)
        assert args.driver is d
        assert args._own_driver is True

        _common.close_step_driver(args)
        assert d.closed is True
        assert not hasattr(args, "driver"), "args.driver va rimosso dopo close"
        assert args._own_driver is False

        # il passo successivo (standalone) riapre un proprio driver
        d2 = _common.step_driver(args)
        assert d2 is not d


@pytest.mark.parametrize("phase_name,mod", _steps_phases())
def test_steps_never_raise_system_exit(phase_name, mod):
    """Un step non puo' uccidere il processo: solo StepResult fail-fast."""
    import re as _re
    for _, step, *_ in mod.STEPS:
        source = inspect.getsource(step)
        assert "raise SystemExit" not in source, (
            f"{step.__name__} deve restituire StepResult.fail, "
            f"non raise SystemExit"
        )


@pytest.mark.parametrize("phase_name,mod", _steps_phases())
def test_steps_do_not_close_shared_driver(phase_name, mod):
    """Nessuno step chiude direttamente il driver: ownership via _common."""
    import re as _re
    for _, step, *_ in mod.STEPS:
        if step.__name__.endswith("dry_run"):
            continue  # eccezione documentata: guard own_driver inline
        source = inspect.getsource(step)
        assert not _re.search(r"^\s*driver\.close\(\)", source, _re.M), (
            f"{step.__name__} deve usare _common.close_step_driver(args), "
            f"non driver.close()"
        )
