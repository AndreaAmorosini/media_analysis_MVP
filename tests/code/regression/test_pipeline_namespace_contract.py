"""Contratto: gli step non leggono attributi fuori dal namespace della pipeline.

La pipeline costruisce il namespace in ``presskg.resolution.pipeline.build_parser``
(più ``driver`` condiviso e ``_own_driver``): ogni step che legge direttamente
``args.<attributo>`` senza un default ``getattr`` esplode con AttributeError
quando viene eseguito in pipeline (bug ripetuto con args.out_dir, args.batch ,
args.mention_label, ...).

Questo test guarda il sorgente di ogni step: le letture dirette ``args.X`` sono
vietate per X fuori dal namespace della pipeline, salva l'allowlist documentata
(Step che costruiscono un namespace dedicato e lo passano ai loro helper).
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src" / "presskg" / "resolution"

# namespace fornito da pipeline.build_parser + driver condiviso (Stadio A/B).
# NOTA su "command": esiste nel namespace, ma e' del DISPATCH della CLI
# (cli.py: args.command == 'pipeline'): gli step NON devono mai
# leggerlo -- semantic_decomposition e' inciampato esattamente qui
# (comandi['pipeline'] -> KeyError). Se serve un sottocomando di step,
# usare un nome specifico (es. semantic_command).
PIPELINE_ATTRS = {
    "phase", "apply", "out", "verbose", "save_state", "batch_id",
    "driver", "_own_driver",
}
RESERVED_CLI_ATTRS = {"command"}

# semantic_decomposition costruisce in run() un Namespace dedicato
# (source_dir, out_dir, model, limit, max_tokens) per i sottocomandi:
# letture legittime, non toccano il namespace della pipeline
ALLOWLIST = {
    "semantic_decomposition.py": {
        "source_dir", "out_dir", "model", "limit", "max_tokens",
    },
}


def test_steps_do_not_read_out_of_pipeline_namespace():
    offenders = {}
    for phase in ("identity", "projection", "predicates", "events"):
        for py in sorted((SRC / phase).glob("*.py")):
            if py.name == "__init__.py":
                continue
            allowed = ALLOWLIST.get(py.name, set())
            text = py.read_text(encoding="utf-8")
            bad = {
                attr
                for attr in re.findall(
                    r"args\.([a-zA-Z_][a-zA-Z0-9_]*)", text
                )
                if (
                    attr not in PIPELINE_ATTRS
                    and attr not in allowed
                    and attr not in RESERVED_CLI_ATTRS
                )
            }
            # args.command e' riservato alla CLI: nessuno step lo legge
            if re.search(r"args\.command\b", text):
                bad = sorted(bad | {"command"})
            else:
                bad = sorted(bad)
            if bad:
                offenders[f"{phase}/{py.name}"] = sorted(bad)

    assert offenders == {}, (
        "step che leggono args.* fuori dal namespace della pipeline "
        "(usare getattr(args, ..., default) o passare il valore risolto): "
        f"{offenders}"
    )
