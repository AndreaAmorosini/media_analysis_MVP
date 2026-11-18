"""CLI della pipeline di validazione PressKG.

Ogni componente espone ``build_parser()`` (sottocomando + opzioni con
default) e ``run(args) -> bool``; questo wrapper si limita al dispatch
e a tradurre un fallimento in exit code 1 (gli script con ``set -e``
devono fermarsi).

Uso:
    python -m presskg.validation <component> [sottocomando] [--opzioni]

Esempi:
    python -m presskg.validation primary generate --out x.csv --limit 10
    python -m presskg.validation decisions select-secondary --primary p.csv --out s.csv
    python -m presskg.validation materialize --commit
"""

from __future__ import annotations

import argparse
import importlib

COMMANDS = {
    "primary": "presskg.validation.primary",
    "corrections": "presskg.validation.corrections",
    "decisions": "presskg.validation.decisions",
    "materialize": "presskg.validation.materialization",
}


def main():
    parser = argparse.ArgumentParser(
        prog="presskg.validation",
        description="Pipeline di validazione dei claim.",
    )
    parser.add_argument("component", choices=sorted(COMMANDS))
    args, rest = parser.parse_known_args()

    mod = importlib.import_module(COMMANDS[args.component])

    ns = mod.build_parser().parse_args(rest)
    ok = mod.run(ns)
    if ok is False:
        # i moduli restituiscono bool: un fallimento deve fermare anche
        # gli script (run_validation_full.sh ha set -e)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
