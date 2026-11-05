
"""CLI della pipeline di validazione PressKG."""

from __future__ import annotations

import argparse
import sys

from . import primary
from . import corrections
from . import decisions
from . import materialization


COMMANDS = {
    "primary": primary.main,
    "corrections": corrections.main,
    "decisions": decisions.main,
    "materialize": materialization.main,
}


def main():
    parser = argparse.ArgumentParser(
        prog="presskg.validation",
        description=(
            "Validazione semantica, correzioni, "
            "decisioni e materializzazione dei fatti"
        ),
    )

    parser.add_argument(
        "component",
        choices=sorted(COMMANDS),
        help="Componente della validazione",
    )

    parser.add_argument(
        "arguments",
        nargs=argparse.REMAINDER,
    )

    args = parser.parse_args()

    component = args.component
    component_args = args.arguments

    # Le CLI esistenti utilizzano argparse.
    # Manteniamo i loro parser e argomenti invariati.
    sys.argv = [
        f"presskg.validation {component}",
        *component_args,
    ]

    COMMANDS[component]()


if __name__ == "__main__":
    main()
