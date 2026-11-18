"""Suite di qualità PressKG: audit read-only del grafo Neo4j.

Punto d'ingresso: `python -m presskg.quality.audit <fase>` oppure la CLI
generale `python -m presskg.cli quality <fase>`. Fasi: audit, semantic,
resolution, all. Nessuna scrittura su Neo4j.
"""

from presskg.quality.audit import *  # noqa: F401,F403
from presskg.quality.audit import main  # noqa: F401
