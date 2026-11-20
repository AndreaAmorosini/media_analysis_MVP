"""Label secondarie di classificazione su nodi canonici: la classe
semantica come label (Person, Organization, ... su CanonicalEntity;
NaturalHazard, PublicDemonstration, ... su CanonicalEvent) per la
visualizzazione per colore nel browser.

- facet_label mappa SOLO facet noti (set chiuso di occurrences.FACETS);
- event_group_label mappa SOLO gruppi del catalogo config;
- su valori sconosciuti/assenti nessuna label (skipped nel report).
"""

from presskg.resolution.projection.entity_materialization import (
    facet_label,
    SET_FACET_LABEL_QUERY,
)
from presskg.resolution.events.event_projection import (
    event_group_label,
    known_event_groups,
    SET_EVENT_GROUP_LABEL_QUERY,
)
from presskg.resolution.identity.occurrences import FACETS


def test_label_queries_format_to_valid_cypher():
    # regressione: le graffe del pattern Cypher NON sono placeholder di
    # str.format() (KeyError '\n    projection_key'): vanno raddoppiate
    q = SET_FACET_LABEL_QUERY.format(label="Person")
    assert "MATCH (c:CanonicalEntity {" in q
    assert "projection_key: k" in q
    assert "SET c:Person" in q

    q = SET_EVENT_GROUP_LABEL_QUERY.format(label="Accident")
    assert "MATCH (c:CanonicalEvent {" in q
    assert "event_key: k" in q
    assert "SET c:Accident" in q


def test_facet_label_known_facets():
    out = {f: facet_label(f) for f in FACETS}
    assert all(v for v in out.values())
    assert out["PERSON"] == "Person"
    assert out["ORGANIZATION"] == "Organization"
    assert out["PRODUCT_SERVICE"] == "ProductService"


def test_facet_label_rejects_unknown_or_missing():
    assert facet_label("NOT_A_FACET") is None
    assert facet_label(None) is None
    assert facet_label("") is None
    assert facet_label("person") == "Person"  # normalizzato


def test_event_group_label_from_catalog():
    groups = known_event_groups()
    assert "public_demonstration" in groups

    assert event_group_label("public_demonstration") == (
        "PublicDemonstration"
    )
    assert event_group_label("natural_hazard") == "NaturalHazard"


def test_event_group_label_rejects_unknown_or_missing():
    assert event_group_label("not_in_catalog") is None
    assert event_group_label(None) is None
    assert event_group_label("") is None
