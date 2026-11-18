"""Regression test del driver Neo4j condiviso (presskg.common.db).

- open_driver() e' l'unica factory del progetto (unica eccezione
  documentata: projection/dry_run.py, che apre il proprio driver con
  override di uri/credential);
- open_driver() sopprime le notifiche del server
  (notifications_min_severity="OFF"): senza questo flag il driver logga
  WARNING a ogni query su un DB con token store povero, sporcando
  l'output della pipeline.
"""

from pathlib import Path

from presskg.common import db as db_mod

SRC = Path(__file__).resolve().parents[3] / "src" / "presskg"

# moduli autorizzati ad aprire un driver direttamente
ALLOWED_DIRECT_OPEN = {
    Path("common/db.py"),
    Path("resolution/projection/dry_run.py"),
}


def test_open_driver_suppresses_server_notifications(monkeypatch):
    captured = {}

    class _FakeGraphDatabase:
        @staticmethod
        def driver(uri, auth=None, **kwargs):
            captured["uri"] = uri
            captured["auth"] = auth
            captured.update(kwargs)
            return object()

    monkeypatch.setattr(db_mod, "GraphDatabase", _FakeGraphDatabase)

    db_mod.open_driver()

    assert captured["auth"] == (
        db_mod.CFG.neo4j_user,
        db_mod.CFG.neo4j_password,
    ), "le credenziali arrivano dal CFG"
    assert captured["notifications_min_severity"] == "OFF", (
        "il driver condiviso deve sopprimere le notifiche del server"
    )


def test_driver_opened_only_via_open_driver():
    """Nessun modulo apre driver Neo4j fuori dalla factory condivisa."""
    hits = []
    for py in SRC.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "GraphDatabase.driver(" in text:
            rel = py.relative_to(SRC)
            if rel not in ALLOWED_DIRECT_OPEN:
                hits.append(str(rel))
    assert not hits, (
        f"moduli che aprono driver direttamente (usare common.db.open_driver): {hits}"
    )
