"""Raccolta document-level degli URL stampati nelle pagine web.

Le stampe di pagine web mostrano l'URL dell'articolo in posizioni che i
classificatori marcano come navigation/footer/unknown; il solo
web_chrome_resolver lo perde. Questo passaggio NON riclassifica le regioni:
registra article_url_candidate nei metadata e valorizza page.source.url
quando manca, con filtro di junk e URL spezzati su più righe.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

from press_reputation.models.page import (PageRecord, Region, RegionType,
                                          SourceType)

URL_START = re.compile(
    r"(?<![\w@.])((?:https?://|www\.)[^\s<>\"]+)", re.IGNORECASE
)
URL_CHARS = re.compile(r"^[\w\-._~:/?#\[\]@!$&'()*+,;=%]+")
TRAILING = ".,;)]}\"'"

JUNK_HOSTS = (
    "youtube.com", "youtu.be", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "linkedin.com", "google.", "gstatic.", "doubleclick.",
    "cedsdigital.", "adnkr.", "cloudflare.", "jsdelivr.", "w3.org",
)
JUNK_HINTS = (
    "/img/", "placeholder", ".css", ".js", ".png", ".jpg", ".jpeg",
    ".gif", ".svg", ".ico", ".woff", "javascript:", "mailto:", "tel:",
)
MAX_REGION_CHARS = 500


def unwrap_urls(text: str) -> list[str]:
    """URL spezzati a fine riga dalla stampa: riga successiva senza spazi."""
    results: list[str] = []
    lines = text.splitlines()

    for index, line in enumerate(lines):
        for match in URL_START.finditer(line):
            url = match.group(1)
            # continua solo se il match arriva a fine riga
            if match.end() == len(line.rstrip(TRAILING)):
                continuation = index + 1
                while continuation < len(lines):
                    nxt = lines[continuation].lstrip()
                    part = URL_CHARS.match(nxt)
                    if not part:
                        break
                    url += part.group(0)
                    if part.end() != len(nxt):
                        break
                    continuation += 1
            results.append(url.rstrip(TRAILING))

    return results


def normalize_candidate(raw: str) -> str | None:
    url = unicodedata.normalize("NFKC", raw).strip().rstrip(TRAILING)
    if not url:
        return None
    if url.lower().startswith("www."):
        url = "https://" + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    lowered = url.casefold()
    if any(junk in lowered for junk in JUNK_HOSTS):
        return None
    if any(hint in lowered for hint in JUNK_HINTS):
        return None
    return url


def has_path(url: str) -> bool:
    try:
        return urlsplit(url).path not in {"", "/"}
    except ValueError:
        return False


class UrlHarvester:
    def enrich_document(self, pages: list[PageRecord]) -> None:
        for page in sorted(pages, key=lambda item: (item.document_id, item.pdf_page)):
            self.enrich_page(page)

    def enrich_page(self, page: PageRecord) -> None:
        article_urls: list[str] = []
        seen: set[str] = set()

        for region in page.regions:
            if not self._eligible(region):
                continue
            region_urls = []
            for raw in unwrap_urls(region.text or ""):
                url = normalize_candidate(raw)
                if url and url not in seen:
                    seen.add(url)
                    region_urls.append(url)
            if not region_urls:
                continue

            path_urls = [url for url in region_urls if has_path(url)]
            if path_urls:
                region.metadata["article_url_candidate"] = path_urls[0]
                article_urls.extend(url for url in path_urls if url not in article_urls)
            else:
                region.metadata["homepage_url_candidate"] = region_urls[0]

        if not seen:
            return

        if article_urls and not page.source.url:
            page.source.url = article_urls[0]
            if page.source.type == SourceType.UNKNOWN:
                page.source.type = SourceType.WEB
        # Una homepage (es. il portale della rassegna, datastampa.it) non è
        # l'URL dell'articolo: se diventasse page.source.url inquina
        # record.url e il rilevamento dei duplicati. Resta solo nei
        # metadata della regione come homepage_url_candidate.

    @staticmethod
    def _eligible(region: Region) -> bool:
        if (not region.text or len(region.text) > MAX_REGION_CHARS or
                region.type in {RegionType.ADVERTISEMENT, RegionType.RELATED_CONTENT,
                                RegionType.WATERMARK, RegionType.RIGHTS_NOTICE} or
                region.metadata.get("inside_article_position_thumbnail")):
            return False
        return True
