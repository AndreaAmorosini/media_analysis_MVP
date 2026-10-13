from __future__ import annotations

from collections import defaultdict
from urllib.parse import urlsplit

from press_reputation.features import PageFeatureExtractor, PageFeatures
from press_reputation.models.page import PageRecord, PageType, RegionType, SourceType


class PageClassifier:
    """Conservative per-page classification plus a document-level continuation pass.

    IMPORTANT: a first page with several blocks is NOT necessarily an index.
    Classification evidence is stored without overwriting source fields.
    """

    def __init__(self) -> None:
        self.feature_extractor = PageFeatureExtractor()

    @staticmethod
    def _text(page: PageRecord) -> str:
        return " ".join((r.text or "") for r in page.regions).casefold()

    @staticmethod
    def _has_editorial_url(page: PageRecord) -> bool:
        if page.source.url and urlsplit(page.source.url).netloc:
            return True
        for region in page.regions:
            content = (region.text or "").casefold()
            if region.metadata.get("article_url_candidate"):
                return True
            if "http://" in content or "https://" in content:
                # A browser-print footer, canonical article URL, or press review link.
                return True
        return False

    @staticmethod
    def _index_evidence(page: PageRecord, features: PageFeatures) -> bool:
        if page.pdf_page != 1 or features.clipping_marker_count:
            return False
        table = any(r.type == RegionType.TABLE for r in page.regions)
        title = any("rassegna del" in (r.text or "").casefold()
                    or "indice" == (r.text or "").strip().casefold()
                    for r in page.regions)
        # Tables may be empty in PageRecord: table + review heading is evidence.
        # A page containing merely 'pag.' and navigation is NOT evidence.
        return table and (title or features.index_entry_count >= 3)

    def classify(self, page: PageRecord) -> PageType:
        f = self.feature_extractor.extract(page)
        txt = self._text(page)
        section = " ".join((page.section or "").replace("_", " ").upper().split())
        has_web_footer = any(r.type == RegionType.FOOTER and
                                (r.text or "").strip().casefold() == "web"
                                for r in page.regions)
        web_url = self._has_editorial_url(page)
        web_signals = (
            int(page.source.type == SourceType.WEB)
            + int(section == "WEB")
            + int(has_web_footer)
            + int(web_url)
            + int(f.web_marker_count >= 2)
            + int("visitatori unici giornalieri" in txt)
        )
        is_index = self._index_evidence(page, f)
        if is_index:
            result = PageType.INDEX
        elif f.clipping_marker_count >= 1 and (
            f.header_metadata_count > 0 or f.clipping_marker_count >= 2
        ):
            result = PageType.CLIPPING
        elif web_signals >= 2 or (web_url and f.clipping_marker_count == 0):
            result = PageType.WEB
        elif f.text_char_count >= 500 and f.image_region_ratio <= 0.4 and not f.web_marker_count:
            result = PageType.PURE_TEXT
        else:
            result = PageType.UNKNOWN
        return result

    def refine_document(self, pages: list[PageRecord]) -> None:
        """Fill UNKNOWN continuation pages without changing reliable INDEX/CLIPPING.

        Does not infer same *article*, only probable page layout.
        Only adjacent pages within the same document and contiguous section.
        """
        by_doc: dict[str, list[PageRecord]] = defaultdict(list)
        for page in pages:
            by_doc[page.document_id].append(page)
        for sequence in by_doc.values():
            sequence.sort(key=lambda p: p.pdf_page)
            for i, page in enumerate(sequence):
                if page.page_type != PageType.UNKNOWN:
                    continue
                neighbours = [p for p in sequence[max(0, i - 1):i + 2]
                                if p is not page and abs(p.pdf_page - page.pdf_page) == 1]
                web_neighbours = [p for p in neighbours if p.page_type == PageType.WEB
                                    and ((p.section or "") == (page.section or "")
                                        or not p.section or not page.section)]
                if not web_neighbours:
                    continue
                features = self.feature_extractor.extract(page)
                if features.clipping_marker_count >= 1 or self._index_evidence(page, features):
                    continue
                text = self._text(page)
                supporting = (
                    self._has_editorial_url(page)
                    or page.source.type == SourceType.WEB
                    or any(token in text for token in (
                        "pagina", "visitatori unici", "condividi", "newsletter", "cookie"
                    ))
                    or any(r.type == RegionType.ARTICLE_BODY and len(r.text or "") > 180
                            for r in page.regions)
                )
                if supporting:
                    page.page_type = PageType.WEB