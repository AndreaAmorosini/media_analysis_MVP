"""Fixture sintetiche condivise: costruzione di Region e PageRecord senza PDF.

I test operano su pagine artificiali (bbox su griglia A4-like 1000x1400 pt)
cosi' ogni resolver e' esercitato senza Docling, OCR o file reali.
"""

from __future__ import annotations

import pytest

from press_reputation.core.models.page import (
    PageRecord,
    PageType,
    Region,
    RegionType,
    SourceInfo,
)


def pytest_addoption(parser):
    parser.addoption(
        "--data-root",
        action="store",
        default=None,
        help="Cartella dei PDF per i test corpus (default: env PR_TEST_DATA_ROOT o 'Data')",
    )


def region(
    type: RegionType = RegionType.ARTICLE_BODY,
    text: str | None = "Testo articolo",
    bbox: list[float] | None = None,
    region_id: str = "r1",
    article_id: str | None = None,
    metadata: dict | None = None,
    style: dict | None = None,
    page: int = 1,
    **extra,
) -> Region:
    bbox = bbox if bbox is not None else [100.0, 200.0, 500.0, 1200.0]
    meta = dict(metadata or {})
    meta.setdefault("region_id", region_id)
    if extra:
        meta.update(extra)
    return Region(
        type=type,
        text=text,
        bbox=bbox,
        extraction_method="pdf_text",
        article_id=article_id,
        provenance=[{"page": page, "content_type": "text"}],
        metadata=meta,
        style=style or {},
    )


def page(
    pdf_page: int = 1,
    regions: list[Region] | None = None,
    page_type: PageType = PageType.CLIPPING,
    document_id: str = "test.pdf",
    width: float = 1000.0,
    height: float = 1400.0,
    source_name: str | None = None,
    publication_date=None,
    original_page: int | None = None,
    **extra,
) -> PageRecord:
    return PageRecord(
        document_id=document_id,
        pdf_page=pdf_page,
        page_type=page_type,
        page_width=width,
        page_height=height,
        source=SourceInfo(
            name=source_name,
            publication_date=publication_date,
            original_page=original_page,
        ),
        regions=regions if regions is not None else [],
        **extra,
    )


def page_with_article(
    pdf_page: int = 1,
    title: str = "Il comune ripristina la fognaria",
    body_lines: int = 6,
    document_id: str = "test.pdf",
    article_id: str = "a1",
) -> PageRecord:
    """Pagina minima completa: titolo, sottotitolo e corpo in colonna singola."""
    regions = [
        region(
            RegionType.ARTICLE_TITLE,
            text=title.upper(),
            bbox=[100.0, 80.0, 900.0, 130.0],
            region_id=f"p{pdf_page}_title",
            article_id=article_id,
            page=pdf_page,
        ),
        region(
            RegionType.ARTICLE_SUBTITLE,
            text="Lavori conclusi nel centro storico",
            bbox=[100.0, 150.0, 900.0, 190.0],
            region_id=f"p{pdf_page}_sub",
            article_id=article_id,
            page=pdf_page,
        ),
    ]
    for index in range(body_lines):
        regions.append(
            region(
                RegionType.ARTICLE_BODY,
                text=f"Linea {index} del testo del body dell'articolo di prova.",
                bbox=[100.0, 220.0 + index * 30, 900.0, 245.0 + index * 30],
                region_id=f"p{pdf_page}_body_{index}",
                article_id=article_id,
                page=pdf_page,
            )
        )
    return page(pdf_page, regions, document_id=document_id)


def two_column_page(
    pdf_page: int = 1,
    article_id: str = "a1",
    document_id: str = "test.pdf",
) -> PageRecord:
    """Due colonne di body affiancate: serve per i test di reading order."""
    left = [
        region(
            RegionType.ARTICLE_BODY,
            text=f"Sinistra {index}",
            bbox=[100.0, 220.0 + index * 30, 500.0, 245.0 + index * 30],
            region_id=f"p{pdf_page}_L{index}",
            article_id=article_id,
            page=pdf_page,
        )
        for index in range(4)
    ]
    right = [
        region(
            RegionType.ARTICLE_BODY,
            text=f"Destra {index}",
            bbox=[520.0, 220.0 + index * 30, 900.0, 245.0 + index * 30],
            region_id=f"p{pdf_page}_R{index}",
            article_id=article_id,
            page=pdf_page,
        )
        for index in range(4)
    ]
    return page(pdf_page, left + right, document_id=document_id)


# ---------------------------------------------------------------------------
# pytest fixtures esposte ai moduli di test
# ---------------------------------------------------------------------------


@pytest.fixture
def make_region():
    return region


@pytest.fixture
def make_page():
    return page


@pytest.fixture
def make_page_with_article():
    return page_with_article


@pytest.fixture
def make_two_column_page():
    return two_column_page


@pytest.fixture
def single_article_document() -> list[PageRecord]:
    """Documento di due pagine: prima con articolo, seconda con continuazione."""
    first = page_with_article(pdf_page=1)
    continuation = [
        region(
            RegionType.ARTICLE_BODY,
            text=f"Continuazione {index}",
            bbox=[100.0, 220.0 + index * 30, 900.0, 245.0 + index * 30],
            region_id=f"p2_body_{index}",
            article_id="a1",
            page=2,
        )
        for index in range(3)
    ]
    return [first, page(pdf_page=2, regions=continuation)]
