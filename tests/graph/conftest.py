"""Fixture condivise per i test del grafo su Neo4j reale (read-only).

Questi test sono audit di integrità del grafo effettivo: richiedono un Neo4j
raggiungibile (vedi CFG.neo4j_uri / .env). Se il database non risponde,
l'intera suite viene saltata con un messaggio esplicito invece di fallire:
`pixi run test` resta verde senza database, `pixi run test-graph` esegue
l'audit quando il database è attivo.

Regola vincolante per ogni nuovo test in questa cartella: SOLO query
read-only (MATCH/RETURN/OPTIONAL/UNWIND/WITH/COUNT...). Nessuna scrittura.
La regola è imposta staticamente da
tests/code/regression/test_graph_tests_are_readonly.py.
"""

import pytest

from presskg.config import CFG


@pytest.fixture(scope="session")
def driver():
    from presskg.common.db import open_driver

    try:
        d = open_driver()
        d.verify_connectivity()
    except Exception as exc:  # noqa: BLE001 - motivo dello skip nel messaggio
        pytest.skip(f"Neo4j non raggiungibile su {CFG.neo4j_uri}: {exc}")
    yield d
    d.close()


@pytest.fixture()
def session(driver):
    with driver.session(database=CFG.neo4j_database) as s:
        yield s


@pytest.fixture()
def quality_store():
    from presskg.quality.audit import Store

    try:
        store = Store()  # verify_connectivity() inclusa
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j non raggiungibile su {CFG.neo4j_uri}: {exc}")
    yield store
    store.close()
