"""Hashing, normalizzazione testo e identità stabili."""

import hashlib
import json
import re
from pathlib import Path


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path) -> str:
    """Hash SHA-256 di un file, letto a blocchi per file di grandi dimensioni."""
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def canonical_digest(value) -> str:
    """Digest canonico di un valore JSON: sorting delle chiavi e separatori compatti.

    `default=str` rende la serializzazione tollerante verso tipi non nativi,
    preservando il digest per tutti gli input serializzabili.
    """
    raw = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize(value: str) -> str:
    return " ".join(re.sub(r"[^\w ]", " ", str(value).casefold()).split())


def fingerprint(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str))


def stable_id(prefix: str, *parts) -> str:
    return f"{prefix}:{sha256('|'.join(map(str, parts)))[:12]}"
