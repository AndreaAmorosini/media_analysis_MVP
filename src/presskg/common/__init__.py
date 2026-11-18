"""Utility condivise del progetto presskg."""

from presskg.common.io import (
    read_json, read_jsonl, iter_jsonl,
    write_json, write_json_stable, write_jsonl,
    latest_records, batches,
)
from presskg.common.hashing import (
    sha256, sha256_file, canonical_digest,
    normalize, fingerprint, stable_id,
)


def require(condition: bool, message: str) -> None:
    """Solleva ValueError se la condizione non è vera."""
    if not condition:
        raise ValueError(message)
