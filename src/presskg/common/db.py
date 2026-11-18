"""Driver Neo4j condiviso."""

from neo4j import GraphDatabase

from presskg.config import CFG


def open_driver(**config):
    """Driver unico dell'applicazione.

    ``**config`` passa opzioni extra al driver (es. ``connection_timeout``).

    ``notifications_min_severity="OFF"`` sopprime le notifiche del server
    (property key mancante, hint, ecc.): rumore privo di valore per la
    pipeline, il driver le loggherebbe a ogni query su DB con token store
    ancora povero (es. appena inizializzato).
    """
    config.setdefault("notifications_min_severity", "OFF")
    return GraphDatabase.driver(
        CFG.neo4j_uri,
        auth=(CFG.neo4j_user, CFG.neo4j_password),
        **config,
    )
