"""Vieta la reintroduzione di utility IO/hashing duplicate nei moduli.

Dal P2 presskg.common è l'unica fonte delle utility condivise: ogni modulo
deve importarle, non ridefinirle. Se un nuovo modulo sente il bisogno di una
copia locale, la variante va promossa in presskg.common (se utile a più
moduli) oppure rinominata con un nome specifico (come già fanno
read_jsonl_or_empty, load_indexed_jsonl, write_json_safe, write_jsonl_safe,
latest_records con history).

Restano deliberatamente locali, in quanto euristiche specifiche del modulo o
legate al formato di identità persistite:
- normalize (e derivate normalize_*/normalized*): heuristics di testo diverse
- fingerprint, stable_id: formati che alimentano ID persistiti
"""

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
SRC = ROOT / "src" / "presskg"

# definizioni vietate a livello di modulo fuori da presskg/common
BANNED_DEFS = [
    "read_json",
    "read_jsonl",
    "write_json",
    "write_jsonl",
    "require",
    "batches",
    "sha256",
    "sha256_file",
    "file_sha256",
    "open_driver",
    "digest",
]


class TestNoDuplicateHelpers:
    def _module_level_defs(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return [
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        ]

    def test_no_banned_local_defs(self):
        offenders = []
        for path in SRC.rglob("*.py"):
            rel = str(path.relative_to(ROOT))
            if rel.startswith("src/presskg/common/"):
                continue
            try:
                names = self._module_level_defs(path)
            except SyntaxError:
                offenders.append(f"{rel}: SINTASSI NON VALIDE")
                continue
            bad = sorted(set(names) & set(BANNED_DEFS))
            if bad:
                offenders.append(f"{rel}: {bad}")
        assert not offenders, (
            "Utility duplicate reintrodotte a livello di modulo "
            "(importare da presskg.common invece):\n" + "\n".join(offenders)
        )

    def test_common_exports_all_helpers(self):
        from presskg.common import (
            require, read_json, read_jsonl, iter_jsonl,
            write_json, write_json_stable, write_jsonl,
            latest_records, batches,
        )
        from presskg.common.hashing import (
            sha256, sha256_file, canonical_digest, normalize, fingerprint, stable_id,
        )
        from presskg.common.db import open_driver
        assert all(callable(f) for f in [
            require, read_json, read_jsonl, iter_jsonl,
            write_json, write_json_stable, write_jsonl,
            latest_records, batches,
            sha256, sha256_file, canonical_digest, normalize, fingerprint, stable_id,
            open_driver,
        ])


class TestCommonHelpersBehavior:
    """Contratti comportamentali minimi delle utility canoniche."""

    def test_read_write_roundtrip(self, tmp_path):
        from presskg.common import read_json, read_jsonl, write_json, write_jsonl

        write_json(tmp_path / "a.json", {"k": "và"})
        assert read_json(tmp_path / "a.json") == {"k": "và"}

        rows = [{"id": 1}, {"id": 2}]
        write_jsonl(tmp_path / "a.jsonl", rows)
        assert read_jsonl(tmp_path / "a.jsonl") == rows

    def test_iter_jsonl_reports_bad_lines(self, tmp_path):
        from presskg.common import iter_jsonl

        f = tmp_path / "bad.jsonl"
        f.write_text('{"a": 1}\nnot json\n', encoding="utf-8")
        gen = iter_jsonl(f)
        assert next(gen) == {"a": 1}
        with pytest.raises(ValueError, match="2"):
            list(gen)

    def test_write_json_stable_sorted_and_missing_dirs(self, tmp_path):
        import json as jsonlib

        from presskg.common import write_json_stable

        target = tmp_path / "nested" / "dir" / "s.json"
        write_json_stable(target, {"b": 1, "a": 2})
        text = target.read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert jsonlib.loads(text) == {"b": 1, "a": 2}
        assert text.index('"a"') < text.index('"b"')
        assert not list(tmp_path.rglob("*.tmp"))

    def test_sha256_file_chunked_matches_reference(self, tmp_path):
        from presskg.common.hashing import sha256, sha256_file

        f = tmp_path / "big.bin"
        f.write_bytes(b"x" * (1024 * 1024 * 2) + b"tail")
        direct = sha256((b"x" * (1024 * 1024 * 2) + b"tail").hex())
        assert sha256_file(f) != direct  # non stiamo hashando l'hex: sanity
        import hashlib
        assert sha256_file(f) == hashlib.sha256(b"x" * (1024 * 1024 * 2) + b"tail").hexdigest()

    def test_canonical_digest_is_order_insensitive(self):
        from presskg.common.hashing import canonical_digest

        assert canonical_digest({"a": 1, "b": 2}) == canonical_digest({"b": 2, "a": 1})
        assert canonical_digest(["a"]) != canonical_digest(["b"])
