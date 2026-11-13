"""Suite di regressione su PDF reali.

I dati (Data/, o la cartella indicata) non sono committati: un test
di questa suite salta se il PDF non esiste nella root configurata.

Configurazione:
    --data-root (flag pytest) oppure variabile d'ambiente PR_TEST_DATA_ROOT
    default: "Data" relativo alla radice del repository.

Esempi:
    pixi run pytest -m corpus                    # solo i casi reali
    pixi run pytest -k hard_1082026              # un solo caso difficile
    PR_TEST_DATA_ROOT=/percorso/altro pixi run pytest -m corpus

I valori attesi vivono in tests/corpus_manifest.json e si rigenerano con
tests/regenerate_corpus_baselines.py dopo modifiche intenzionali.
La pipeline gira UNA volta per caso: i 6 test del caso condividono
l'output tramite la fixture class-scoped `case_output`.
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

import pytest

from press_reputation.cli import parse

MANIFEST_PATH = Path(__file__).resolve().parent / "corpus_manifest.json"

VALID_FINALIZATION_STATUSES = {"ready", "review_required"}


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def case_parameters(category: str | None = None):
    markers = {
        "easy": [pytest.mark.corpus, pytest.mark.corpus_easy],
        "medium": [pytest.mark.corpus, pytest.mark.corpus_medium, pytest.mark.slow],
        "hard": [pytest.mark.corpus, pytest.mark.corpus_hard, pytest.mark.slow],
    }
    parameters = []
    for case in _manifest()["cases"]:
        if category and case["category"] != category:
            continue
        parameters.append(
            pytest.param(case, id=case["id"], marks=markers[case["category"]])
        )
    return parameters


@pytest.fixture(scope="session")
def data_root(request) -> Path | None:
    configured = (
        request.config.getoption("--data-root")
        or os.environ.get("PR_TEST_DATA_ROOT")
        or "Data"
    )
    root = Path(configured)
    if not root.is_dir():
        pytest.skip(f"cartella dati '{root}' non trovata: i test corpus saltano")
    return root


# una sola run della pipeline per caso: i 6 test del caso condividono l'output
_RUN_CACHE: dict[str, dict] = {}


@pytest.fixture
def case_output(request, case, data_root, tmp_path):
    pdf = data_root / case["path"]
    if not pdf.exists():
        pytest.skip(f"PDF non presente: {pdf}")
    if case["id"] not in _RUN_CACHE:
        parse(
            pdf_path=pdf,
            output_dir=tmp_path,
            debug_bbox=False,
            ocr_policy="auto",
            web_first=False,
            allow_untitled=False,
        )
        doc_dir = tmp_path / pdf.stem.replace(" ", "_")
        records_path = doc_dir / "flow" / "article_records.json"
        records = json.loads(records_path.read_text(encoding="utf-8")) if records_path.exists() else []
        page_files = sorted(glob.glob(str(doc_dir / "pages" / "page_*.json")))
        pages = [json.loads(Path(path).read_text(encoding="utf-8")) for path in page_files]
        drafts_path = doc_dir / "flow" / "article_drafts.json"
        drafts = json.loads(drafts_path.read_text(encoding="utf-8")) if drafts_path.exists() else []
        _RUN_CACHE[case["id"]] = dict(pdf=pdf, records=records, pages=pages, drafts=drafts)
    return _RUN_CACHE[case["id"]]


@pytest.mark.corpus
@pytest.mark.parametrize("case", case_parameters())
class TestRealPdfRegression:
    @pytest.fixture(autouse=True)
    def _load_expected(self, case):
        self.expected = case.get("expected")

    # ------------------------------------------------------------------
    # invarianti valide per ogni categoria
    # ------------------------------------------------------------------

    def test_pipeline_completes(self, case_output):
        assert case_output["pages"], "nessuna pagina prodotta"

    def test_page_records_are_valid(self, case_output):
        for page in case_output["pages"]:
            assert "pdf_page" in page and "document_id" in page
            for region in page.get("regions", []):
                bbox = region.get("bbox")
                if bbox is not None:
                    assert len(bbox) == 4

    def test_article_records_wellformed(self, case_output):
        for record in case_output["records"]:
            assert record.get("finalization_status") in VALID_FINALIZATION_STATUSES
            assert record.get("document_id")
            assert record.get("id")

    def test_ready_records_have_title_and_body(self, case_output):
        for record in case_output["records"]:
            if record.get("finalization_status") == "ready":
                assert (record.get("title") or "").strip(), f"articolo ready senza titolo: {record['id']}"
                assert (record.get("body_clean") or record.get("body") or "").strip()

    def test_pages_have_regions(self, case_output):
        for page in case_output["pages"]:
            assert page["regions"], f"pagina {page['pdf_page']} senza regioni"

    # ------------------------------------------------------------------
    # regressioni rispetto al manifest
    # ------------------------------------------------------------------

    def test_matches_baseline(self, case_output):
        if not self.expected:
            pytest.skip("baseline non presente nel manifest: rigenerare con regenerate_corpus_baselines.py")
        assert len(case_output["pages"]) >= self.expected.get("min_pages", 0)
        assert len(case_output["records"]) == self.expected.get("articles"), (
            f"numero articoli variato: attesi {self.expected.get('articles')}, "
            f"prodotti {len(case_output['records'])}"
        )
        statuses = {}
        for record in case_output["records"]:
            status = record.get("finalization_status")
            statuses[status] = statuses.get(status, 0) + 1
        assert statuses == self.expected.get("finalization_statuses", {}), (
            f"distribuzione finalization_status variata: attesa "
            f"{self.expected.get('finalization_statuses')}, trovata {statuses}"
        )
