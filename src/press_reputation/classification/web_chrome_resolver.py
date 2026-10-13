
import re
import unicodedata
from urllib.parse import urlsplit

from press_reputation.models.page import (
    PageRecord,
    PageType,
    Region,
    RegionType,
    SourceType,
)


class WebChromeResolver:
    """
    Riconosce elementi tecnici e di navigazione web
    prima della selezione dei titoli editoriali.

    Utilizza soltanto evidenze ad alta precisione.
    Le regioni ambigue rimangono inalterate.
    """

    ARTICLE_LINK = re.compile(
        r"^\s*(?:vai\s+all['’]articolo|leggi\s+l['’]articolo)\b",
        re.IGNORECASE,
    )

    URL_PATTERN = re.compile(
        r"https?://[^\s<>\"']+",
        re.IGNORECASE,
    )

    NAVIGATION_TERMS = {
        "home",
        "cronaca",
        "politica",
        "economia",
        "sport",
        "cultura",
        "spettacoli",
        "attualità",
        "italia",
        "mondo",
        "ambiente",
        "salerno",
        "napoli",
        "campania",
        "provincia",
    }

    PROTECTED_TYPES = {
        RegionType.WATERMARK,
        RegionType.RIGHTS_NOTICE,
        RegionType.FOOTER,
        RegionType.PRESS_REVIEW_PROVIDER,
        RegionType.SOURCE_NAME,
        RegionType.PUBLICATION_DATE,
        RegionType.ORIGINAL_PAGE,
        RegionType.CLIPPING_SHEET,
        RegionType.IMAGE,
        RegionType.CAPTION,
        RegionType.ARTICLE_POSITION_THUMBNAIL,
    }

    def enrich_document(
        self,
        pages: list[PageRecord],
    ) -> list[PageRecord]:

        for page in pages:
            self.enrich_page(page)

        return pages

    def enrich_page(self, page: PageRecord) -> None:

        for region in page.regions:
            if not region.text:
                continue

            if region.type in self.PROTECTED_TYPES:
                continue

            text = self.normalize(region.text)

            # Marker tecnico del provider di rassegna.
            if self.ARTICLE_LINK.match(text):
                self.handle_article_link(page, region)
                continue

            # Non classificare il resto della pagina come
            # web chrome senza evidenze di origine web.
            if not self.is_web_context(page):
                continue

            if self.is_navigation(region, page):
                self.mark(
                    region,
                    RegionType.NAVIGATION,
                    "web_navigation",
                )

    @staticmethod
    def normalize(text: str) -> str:
        text = unicodedata.normalize("NFKC", text)
        return " ".join(text.split())

    @staticmethod
    def is_web_context(page: PageRecord) -> bool:
        section = (page.section or "").strip().upper()

        return (
            page.page_type == PageType.WEB
            or section == "WEB"
            or page.source.type == SourceType.WEB
        )

    def handle_article_link(
        self,
        page: PageRecord,
        region: Region,
    ) -> None:

        match = self.URL_PATTERN.search(region.text or "")

        if match:
            url = match.group(0).rstrip(".,;)")

            parsed = urlsplit(url)

            if parsed.scheme in {"http", "https"} and parsed.netloc:
                region.metadata["article_url_candidate"] = url

                if not page.source.url:
                    page.source.url = url
                    page.source.type = SourceType.WEB

        self.mark(
            region,
            RegionType.HEADER_METADATA,
            "press_review_article_link",
        )

    def is_navigation(
        self,
        region: Region,
        page: PageRecord,
    ) -> bool:

        if region.type not in {
            RegionType.UNKNOWN,
            RegionType.ARTICLE_BODY,
            RegionType.ARTICLE_SECTION_HEADER,
        }:
            return False

        if not region.bbox or not page.page_height:
            return False

        # Filtro conservativo: menu nella parte
        # superiore della pagina.
        relative_y = region.bbox[1] / page.page_height

        if relative_y > 0.45:
            return False

        text = self.normalize(region.text or "").casefold()

        words = re.findall(r"[^\W\d_]+", text, re.UNICODE)

        if not 3 <= len(words) <= 18:
            return False

        matches = sum(
            word in self.NAVIGATION_TERMS
            for word in words
        )

        # Una singola parola come "Cronaca" non
        # è sufficiente a classificare navigation.
        return matches >= 3

    @staticmethod
    def mark(
        region: Region,
        new_type: RegionType,
        reason: str,
    ) -> None:

        region.metadata["web_chrome"] = {
            "detected": True,
            "reason": reason,
            "original_type": region.type.value,
        }

        region.type = new_type
        region.exclude_from_article_text = True
