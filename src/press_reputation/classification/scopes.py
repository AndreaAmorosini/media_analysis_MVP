# Content Scope constants for all the pipeline

BLOCKED_SCOPES: frozenset[str] = frozenset(
    {"related", "advertisement", "boilerplate", "non_main", "navigation"}
)


def is_blocked_scope(region) -> bool:
    return region.metadata.get("content_scope") in BLOCKED_SCOPES
