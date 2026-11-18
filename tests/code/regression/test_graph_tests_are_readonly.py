"""Impone la disciplina read-only ai test del grafo.

Ogni file in tests/graph deve contenere SOLO query di lettura: nessun
token di scrittura Cypher (CREATE, MERGE, DELETE, DETACH DELETE, SET,
REMOVE, DROP, LOAD CSV). I test del grafo lavorano sul DB reale di
sviluppo: una scrittura accidentale corromperebbe lo stato condiviso.

Se serve costruire dati di test, usare un database dedicato con
docker-compose a parte, non la suite di audit.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]

WRITE_TOKENS = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|DROP|REMOVE|LOAD\s+CSV)\b"
)
# SET è permesso solo come parte di "ON CREATE SET"? no: lo vietiamo del tutto:
# nessun test del grafo ha motivo di usare SET.
SET_TOKEN = re.compile(r"\bSET\b")

EXEMPT_FILES = {"test_graph_tests_are_readonly.py", "conftest_sample_skip.py"}


def graph_test_files():
    folder = ROOT / "tests" / "graph"
    return [p for p in folder.glob("*.py") if p.name not in EXEMPT_FILES]


def strip_comments_and_docstrings(source: str) -> str:
    lines = []
    in_doc = False
    for line in source.splitlines():
        stripped = line.strip()
        if in_doc:
            if '"""' in stripped:
                in_doc = False
            continue
        if stripped.startswith('"""'):
            if stripped.count('"""') >= 2:
                continue
            in_doc = True
            continue
        if "#" in line:
            line = line.split("#", 1)[0]
        lines.append(line)
    return "\n".join(lines)


class TestGraphTestsAreReadOnly:

    def test_no_write_tokens(self):
        offenders = []
        for path in graph_test_files():
            code = strip_comments_and_docstrings(path.read_text(encoding="utf-8"))
            for token_re in (WRITE_TOKENS, SET_TOKEN):
                match = token_re.search(code)
                if match:
                    offenders.append(f"{path.name}: token di scrittura '{match.group(0)}'")
        assert not offenders, (
            "Test del grafo con query di scrittura su Neo4j:\n"
            + "\n".join(offenders)
        )

    def test_graph_suite_uses_shared_fixtures(self):
        """La cartella tests/graph deve avere la conftest con skip/fixture."""
        conftest = ROOT / "tests" / "graph" / "conftest.py"
        assert conftest.is_file(), "manca tests/graph/conftest.py"
        text = conftest.read_text(encoding="utf-8")
        assert "pytest.skip" in text, (
            "la conftest dei test del grafo deve fare skip se Neo4j è irraggiungibile"
        )
