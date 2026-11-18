"""
Vocabolario supplementare di relazioni giornalistiche.

Usato insieme alla precedente ontology.py.
Le relazioni inverse conservano il loro verso.
"""

import re


VERSION = "news-predicates-v4"


FAMILIES = {
    "causal_effect": """
        REDUCES ELIMINATES ENABLES REQUIRES
        CONTRIBUTES_TO RESOLVES AFFECTED_BY
        EXPERIENCES PRODUCES
    """,

    "communication": """
        DECLARES DISCUSSES RECOMMENDS
        PROPOSES COVERS REPRESENTS
        FOCUSES_ON OPPOSES SUPPORTS
        DENOUNCES ACCUSES PROMISES
    """,

    "legal": """
        FILES_COMPLAINT
    """,

    "participation": """
        PRESENT_AT PARTICIPATES_IN
        INVOLVED_IN COORDINATES_WITH
        WORKS_FOR WORKS_ON SERVES
    """,

    "operations": """
        RECEIVES USES INCLUDES SEIZES
        TARGETS COLLECTS COMPLETES
        ACTIVATES ADOPTS INSTALLS
        OFFERS PERFORMS IMPOSES
        IMPLEMENTS EXECUTES LAUNCHES
        ISSUES REVOKES
    """,

    "financing": """
        FINANCED_BY FUNDS
    """,

    "finding": """
        FINDS_IN FINDS
    """,

    "institutional": """
        SIGNS REQUESTS
    """,
}


CANONICAL = {
    predicate: family
    for family, values in FAMILIES.items()
    for predicate in values.split()
}


ALIASES = {
    "FINDES_IN": "FINDS_IN",
    "FINDES": "FINDS",
    "PARTICIPATED_IN": "PARTICIPATES_IN",
    "RECEIVED": "RECEIVES",
    "REQUESTED": "REQUESTS",
    "DECLARED": "DECLARES",
    "SIGNED": "SIGNS",
}
ALIASES.update({
    "FUND": "FUNDS",
    "REDUCE": "REDUCES",
    "REDUCED": "REDUCES",
    "REPORTED": "REPORTS",
    "SUBMITTED": "SUBMITS",
})


# Questi valori non vengono trasformati
# automaticamente in relazioni più specifiche.
REVIEW_ONLY = {
    "HAS",
    "IS",
    "OPINION",
    "OPINIONS",
    "PATRONIZES",
    "DISCHARGES",
}


# Il verso è diverso dall'equivalente attivo.
INVERSE_PREDICATES = {
    "FINANCED_BY",
    "AFFECTED_BY",
}


def normalize_predicate_name(raw):
    return re.sub(
        r"[\s-]+",
        "_",
        str(raw or "").strip().upper(),
    )


def canonical_news_predicate(raw):
    name = normalize_predicate_name(raw)

    if name in REVIEW_ONLY or not name:
        return "OTHER", "unmapped"

    canonical = ALIASES.get(name, name)

    if canonical in CANONICAL:
        return canonical, CANONICAL[canonical]

    return "OTHER", "unmapped"


def preferred_predicates():
    return sorted(CANONICAL)


def needs_predicate_review(raw):
    return normalize_predicate_name(raw) in REVIEW_ONLY
