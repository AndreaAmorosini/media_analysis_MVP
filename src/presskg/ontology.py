"""Registro versionato dei predicati giornalistici."""

import re

VERSION = "news-ontology-v2"

FAMILIES = {
    "reporting": """
        REPORTS ANNOUNCES DECLARES CONFIRMS
        DENIES CRITICIZES REQUESTS EXPLAINS
        PROMOTES PRESENTS PRESENTS_AT
        SPEAKS_ABOUT INVITES AFFIRMS
        COMMENTS_ON REPORTS_ON INVESTIGATES
        OPPOSES SUPPORTS
    """,

    "coordination": """
        COORDINATES COLLABORATES_WITH
        PARTICIPATES_IN ORGANIZES PRESIDES_OVER
        MODERATES APPROVES ASSIGNS_TO
        SIGNS AWARDS ACCEPTS
    """,

    "services": """
        CAUSES RESTORES MANAGES INVESTS_IN
        AFFECTS PREVENTS MITIGATES PROVIDES
        CONTAINS FUNDS OPERATES IMPROVES
        MONITORS EXECUTES REALIZES INVOLVES
    """,

    "spatial": """
        OPERATES_IN LOCATED_IN LOCATED_AT
        WORKS_AT
    """,

    "topic": """
        CONCERNS
    """,
}

CANONICAL = {
    predicate: family
    for family, values in FAMILIES.items()
    for predicate in values.split()
}

# Solo sinonimi considerati sufficientemente sicuri.
ALIASES = {
    "FINANCES": "FUNDS",
    "FINANCES_FOR": "FUNDS",

    "COOPERATES_WITH": "COLLABORATES_WITH",
    "WORKS_WITH": "COLLABORATES_WITH",
    "PARTNERS_WITH": "COLLABORATES_WITH",

    "SIGNATURED": "SIGNS",

    "COMMENTATES": "COMMENTS_ON",
    "COMMENTED_ON": "COMMENTS_ON",
}


def canonical_predicate(raw: str | None):
    original = re.sub(r"[\s-]+", "_", str(raw or "OTHER").strip().upper())

    canonical = ALIASES.get(original, original)

    if canonical in CANONICAL:
        return canonical, CANONICAL[canonical]

    return "OTHER", "unmapped"