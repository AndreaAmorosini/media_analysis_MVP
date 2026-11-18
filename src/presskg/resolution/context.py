"""Context e risultato tipizzato degli step di resolution (Stadio B).

Contratto nuovo, introdotto fase per fase a fianco del vecchio
``run(args) -> bool``:

- lo step riceve un solo argomento ``ctx: StepContext`` e restituisce un
  ``StepResult``;
- ``StepContext`` incapsula gli argomenti della pipeline (retecompat:
  ogni attributo non definito viene forwardato verso ``args``) e offre
  accessi tipizzati: driver condiviso, directory artefatti, out root;
- ``StepResult`` raccolta esito, conteggi, warning e check: entra nello
  stato della pipeline e nel debug dump invece del solo booleano.

La retrocompatibilità del vecchio contratto resta gestita dal runner
(``_common.run_steps``): uno step può ancora restituire un plain bool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepContext:
    """Argomenti di esecuzione di un singolo step."""

    args: Any                      # argparse.Namespace della pipeline
    phase: str                      # es. "identity"
    step: str = ""                  # es. "00_profiles"
    step_module: str = ""           # es. "presskg.resolution.identity.profiles"

    @property
    def apply(self) -> bool:
        """True solo con --apply: le scritture su Neo4j sono opt-in."""
        return bool(getattr(self.args, "apply", False))

    @property
    def verbose(self) -> bool:
        return bool(getattr(self.args, "verbose", False))

    def out_root(self):
        """Root degli artefatti della pipeline (--out)."""
        from presskg.resolution import paths
        return paths.root(self.args)

    def step_dir(self, step_key: str):
        """Directory dell'artefatto di uno step (registry paths)."""
        from presskg.resolution import paths
        return paths.step_dir(self.args, step_key)

    def step_file(self, step_key: str, name: str):
        from presskg.resolution import paths
        return paths.step_file(self.args, step_key, name)

    def driver(self):
        """Driver Neo4j condiviso (o proprio, se lo step gira da solo)."""
        from presskg.resolution._common import step_driver
        return step_driver(self.args)

    def __getattr__(self, item):
        # retecompat: tutto ciò che non è del context arriva dagli args
        return getattr(self.args, item)


@dataclass
class StepResult:
    """Esito tipizzato di uno step."""

    ok: bool = True
    error: str | None = None
    counts: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)

    @classmethod
    def ok_result(cls, **counts) -> "StepResult":
        return cls(ok=True, counts=dict(counts))

    @classmethod
    def from_summary(cls, summary=None, **counts) -> "StepResult":
        """StepResult di successo con counts estratti da un summary step.

        I summary degli step già contano tutto (``graph_writes``,
        ``llm_calls``, entità, warnings, ...): le voci scalari
        (int, float, bool) entrano in ``counts``, stringhe, checksum e
        strutture annidate restano nel summary.json su disco. Le ``counts``
        esplicite vincono su quelle derivate dal summary.
        """
        derived = dict(counts)
        if summary:
            for key, value in summary.items():
                if key not in derived and isinstance(value, (int, float, bool)):
                    derived[key] = value
        return cls(ok=True, counts=derived)

    @classmethod
    def fail(cls, error: str = "step fallito (vedere output diagnostico)") -> "StepResult":
        return cls(ok=False, error=str(error))

    def __bool__(self) -> bool:
        return self.ok
