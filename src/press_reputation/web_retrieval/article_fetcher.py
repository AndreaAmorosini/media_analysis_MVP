"""Safe-ish, bounded unauthenticated HTML fetch plus main-article extraction.

No login, no cookies, no anti-paywall bypass, no article text from metadata-only
JSON-LD. Production deployments should also restrict outbound egress to public IPs.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup


@dataclass
class FetchOutcome:
    status: str
    url: str
    resolved_url: str | None = None
    title: str | None = None
    text: str = ""
    authors: list[str] = field(default_factory=list)
    published: str | None = None
    http_status: int | None = None
    reason: str | None = None
    signals: list[str] = field(default_factory=list)


class PublicArticleFetcher:
    MAX_HTML_BYTES = 4 * 1024 * 1024
    MAX_REDIRECTS = 3
    TIMEOUT_SECONDS = 12.0
    USER_AGENT = "MediaAnalysisMVP/0.1 (public article retrieval; no authentication)"

    def __init__(self, client_factory=None, public_host_checker=None):
        self.client_factory = client_factory or (
            lambda: httpx.Client(
                timeout=httpx.Timeout(self.TIMEOUT_SECONDS),
                follow_redirects=False,
                trust_env=False,  # no proxy / credentials from environment
                headers={
                    "User-Agent": self.USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "it,en;q=0.8",
                },
            )
        )
        self.public_host_checker = public_host_checker or self._public_host

    @staticmethod
    def normalized_url(url: str) -> str | None:
        try:
            parts = urlsplit(url.strip())
            if parts.scheme.lower() not in {"https", "http"}:
                return None
            if not parts.hostname or parts.username or parts.password:
                return None
            if parts.port not in {None, 80, 443}:
                return None
            host = parts.hostname.lower().rstrip(".")
            if (host in {"localhost", "localhost.localdomain"}
                    or host.endswith((".local", ".internal", ".localhost", ".test"))
                    or "." not in host or len(url) > 2048):
                return None
            # IP literals are deliberately disallowed, even if public.
            try:
                ipaddress.ip_address(host)
                return None
            except ValueError:
                pass
            return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _public_host(host: str) -> bool:
        """DNS guard; enforce public-only outbound networking at deployment too."""
        try:
            addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            if not addresses:
                return False
            return all(ipaddress.ip_address(entry[4][0]).is_global for entry in addresses)
        except (OSError, ValueError):
            return False

    @staticmethod
    def _same_site_redirect(source: str, destination: str) -> bool:
        def normalized_host(value):
            h = urlsplit(value).hostname or ""
            return h[4:] if h.startswith("www.") else h
        # Do not downgrade a TLS URL to plain HTTP.
        if urlsplit(source).scheme == "https" and urlsplit(destination).scheme != "https":
            return False
        return normalized_host(source) == normalized_host(destination)

    @staticmethod
    def _paywall_signals(soup: BeautifulSoup) -> list[str]:
        found: list[str] = []
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or script.get_text() or "null")
            except (ValueError, TypeError):
                continue
            stack = [data]
            while stack:
                value = stack.pop()
                if isinstance(value, list):
                    stack.extend(value)
                elif isinstance(value, dict):
                    tp = value.get("@type")
                    tp_list = tp if isinstance(tp, list) else [tp]
                    if (any(isinstance(t, str) and
                            t.split("/")[-1] in {"Article", "NewsArticle", "BlogPosting", "WebPage", "WebPageElement"}
                            for t in tp_list)
                            and str(value.get("isAccessibleForFree", "")).lower() == "false"):
                        found.append("jsonld_isAccessibleForFree_false")
                    stack.extend(value.values())
        for tag in soup.select('[itemprop="isAccessibleForFree"]'):
            if str(tag.get("content", tag.get_text(strip=True))).lower() == "false":
                found.append("microdata_isAccessibleForFree_false")
        for meta in soup.select('meta[name="article:content_tier"], meta[property="article:content_tier"]'):
            if (meta.get("content") or "").lower() in {"locked", "premium", "paid", "subscriber"}:
                found.append("content_tier_locked")
        return list(dict.fromkeys(found))

    @staticmethod
    def _blocked_page(soup: BeautifulSoup) -> bool:
        text = " ".join(soup.stripped_strings).lower()[:8000]
        return bool(re.search(
            r"(?:abbonati|sottoscrivi|effettua l.accesso|accedi)\s+"
            r"(?:per|e)\s+(?:continuare|leggere|visualizzare)|"
            r"contenuto\s+(?:riservato|esclusivo)\s+(?:agli\s+)?abbonati|"
            r"subscribe\s+to\s+(?:continue|read)|sign in to continue reading",
            text,
        ))

    def fetch(self, url: str) -> FetchOutcome:
        clean = self.normalized_url(url)
        if not clean:
            return FetchOutcome("invalid_url", url, reason="non_public_http_url")
        last_status = None
        visited = set()
        current = clean
        try:
            with self.client_factory() as client:
                for _ in range(self.MAX_REDIRECTS + 1):
                    if current in visited:
                        return FetchOutcome("unavailable", clean, current, reason="redirect_loop")
                    visited.add(current)
                    parts = urlsplit(current)
                    if not self.public_host_checker(parts.hostname):
                        return FetchOutcome("invalid_url", clean, current, reason="non_public_dns")
                    with client.stream("GET", current) as response:
                        last_status = response.status_code
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                return FetchOutcome("unavailable", clean, current, http_status=last_status, reason="empty_redirect")
                            nxt = self.normalized_url(urljoin(current, location))
                            if not nxt or not self._same_site_redirect(current, nxt):
                                return FetchOutcome("unavailable", clean, current, http_status=last_status, reason="unsafe_redirect")
                            current = nxt
                            continue
                        if response.status_code in {401, 402}:
                            return FetchOutcome("restricted", clean, current, http_status=last_status, reason="login_or_payment_required")
                        if response.status_code in {403, 429}:
                            return FetchOutcome("blocked", clean, current, http_status=last_status, reason="access_denied_or_rate_limited")
                        if response.status_code != 200:
                            return FetchOutcome("unavailable", clean, current, http_status=last_status, reason="http_error")
                        content_type = response.headers.get("content-type", "").lower()
                        if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                            return FetchOutcome("unavailable", clean, current, http_status=200, reason="non_html_response")
                        body = bytearray()
                        for part in response.iter_bytes():
                            body.extend(part)
                            if len(body) > self.MAX_HTML_BYTES:
                                return FetchOutcome("unavailable", clean, current, http_status=200, reason="html_too_large")
                        html = bytes(body).decode(response.encoding or "utf-8", errors="replace")
                        return self.extract_html(html, clean, current, http_status=200)
            return FetchOutcome("unavailable", clean, current, http_status=last_status, reason="too_many_redirects")
        except (httpx.HTTPError, OSError, UnicodeError) as exc:
            return FetchOutcome("unavailable", clean, current, http_status=last_status, reason=f"network_error:{type(exc).__name__}")

    def extract_html(self, html: str, requested_url: str, resolved_url: str, http_status: int = 200) -> FetchOutcome:
        """Extract ONLY rendered public HTML content, never bypass paywall tags."""
        soup = BeautifulSoup(html, "lxml")
        signals = self._paywall_signals(soup)
        if signals:
            return FetchOutcome("paywall", requested_url, resolved_url, http_status=http_status, reason="explicit_paywall", signals=signals)
        try:
            import trafilatura
            result = trafilatura.extract(
                html,
                url=resolved_url,
                output_format="json",
                with_metadata=True,
                favor_precision=True,
                include_comments=False,
                include_tables=False,
            )
            payload = json.loads(result) if result else {}
        except (ImportError, ValueError, TypeError) as exc:
            return FetchOutcome("unavailable", requested_url, resolved_url, http_status=http_status, reason=f"extractor_error:{type(exc).__name__}")
        text = (payload.get("text") or "").strip()
        title = (payload.get("title") or "").strip() or None
        if not text:
            status = "restricted" if self._blocked_page(soup) else "insufficient"
            return FetchOutcome(status, requested_url, resolved_url, title, http_status=http_status, reason="no_main_article_text")
        if self._blocked_page(soup) and len(text) < 1800:
            return FetchOutcome("paywall", requested_url, resolved_url, title, text, http_status=http_status, reason="likely_teaser_or_login_wall", signals=["subscription_prompt_and_short_text"])
        authors = payload.get("author") or ""
        authors = [a.strip() for a in re.split(r"[,;]", authors) if a.strip()] if isinstance(authors, str) else []
        return FetchOutcome("extracted", requested_url, resolved_url, title=title, text=text, authors=authors, published=payload.get("date"), http_status=http_status, signals=signals)