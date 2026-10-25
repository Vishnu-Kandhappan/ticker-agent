"""Polite news crawler: safe search, pacing, robots.txt, one retry, source log.

Rules are documented in config/crawler-rules.md.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

from financial_alert.telemetry import get_tracer, record_error

logger = logging.getLogger(__name__)
TRACER = get_tracer(__name__)

USER_AGENT = "stock-review-bot/1.0"
MAX_PAGE_BYTES = 1_500_000
BLOCK_STATUSES = {401, 403, 429, 451, 503}

TRUSTED_DOMAINS = {
    "reuters.com", "bloomberg.com", "wsj.com", "cnbc.com", "ft.com", "apnews.com",
    "barrons.com", "marketwatch.com", "nasdaq.com", "sec.gov", "nytimes.com",
    "economist.com", "federalreserve.gov", "rbi.org.in", "nseindia.com", "bseindia.com",
    "economictimes.indiatimes.com", "livemint.com", "business-standard.com", "moneycontrol.com",
}
SEMI_TRUSTED_DOMAINS = {
    "finance.yahoo.com", "yahoo.com", "seekingalpha.com", "investing.com", "fool.com",
    "benzinga.com", "marketbeat.com", "zacks.com", "thestreet.com", "businessinsider.com",
    "forbes.com", "investopedia.com", "morningstar.com", "tipranks.com", "cnn.com", "bbc.com",
    "theguardian.com", "axios.com", "techcrunch.com", "theverge.com",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _matches(host: str, domains: set[str]) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def classify_trust(url: str) -> str:
    host = domain_of(url)
    if _matches(host, TRUSTED_DOMAINS):
        return "trusted"
    if _matches(host, SEMI_TRUSTED_DOMAINS):
        return "semi-trusted"
    return "unknown"


@dataclass
class SourceLogEntry:
    url: str
    trust: str
    status: str  # used | blocked | robots_disallowed | search_failed | not_needed
    timestamp_utc: str
    attempts: int = 0
    http_status: int | None = None
    title: str = ""
    note: str = ""
    scope: str = "ticker"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NewsItem:
    id: str
    title: str
    url: str
    trust: str
    published: str
    snippet: str
    page_text: str
    fetched_at_utc: str
    scope: str = "ticker"  # ticker | macro

    @property
    def text(self) -> str:
        return (self.page_text or self.snippet)[:900]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CrawlResult:
    items: list[NewsItem] = field(default_factory=list)
    log: list[SourceLogEntry] = field(default_factory=list)
    query: str = ""


class _TextExtractor(HTMLParser):
    """Collect <title>, meta description and paragraph text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.paragraphs: list[str] = []
        self._in_title = False
        self._in_p = False
        self._skip = 0
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "p":
            self._in_p = True
            self._buffer = []
        elif tag == "meta":
            values = {key.lower(): (value or "") for key, value in attrs}
            if values.get("name", "").lower() == "description" or values.get("property", "").lower() == "og:description":
                self.description = self.description or values.get("content", "").strip()

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "p" and self._in_p:
            self._in_p = False
            text = " ".join("".join(self._buffer).split())
            if len(text) > 40:
                self.paragraphs.append(text)

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
        if self._in_p:
            self._buffer.append(data)

    def text(self, limit: int = 1500) -> str:
        body = " ".join(self.paragraphs) or self.description
        return body[:limit]


def _default_search(query: str, safesearch: str, max_results: int, timeout: float) -> list[dict[str, Any]]:
    from ddgs import DDGS

    with DDGS(timeout=int(max(1, timeout))) as search:
        return search.news(query, safesearch=safesearch, timelimit="w", max_results=max_results) or []


def _default_fetch(url: str, timeout: float) -> tuple[int, str]:
    """Return (status, body). Raise HTTPError/URLError on failure."""
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_PAGE_BYTES)
        charset = response.headers.get_content_charset() or "utf-8"
        return int(getattr(response, "status", 200)), raw.decode(charset, errors="replace")


class SafeCrawler:
    """Search news, then read each page within the crawler rules."""

    def __init__(
        self,
        max_items: int = 2,
        sleep_ms: int = 1500,
        safe_search: bool = True,
        timeout_ms: int = 20000,
        search: Callable[[str, str, int, float], list[dict[str, Any]]] = _default_search,
        fetch: Callable[[str, float], tuple[int, str]] = _default_fetch,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_items = max(1, max_items)
        self.sleep_seconds = max(0, sleep_ms) / 1000.0
        self.safesearch = "on" if safe_search else "moderate"
        self.timeout = max(1, timeout_ms) / 1000.0
        self._search = search
        self._fetch = fetch
        self._sleep = sleeper
        self._clock = clock
        self._last_request: float | None = None
        self._robots: dict[str, RobotFileParser | None] = {}
        self._crawl_delay: dict[str, float] = {}
        self._item_counter = 0

    # -- pacing -----------------------------------------------------------
    def _pace(self, host: str | None = None) -> None:
        delay = max(self.sleep_seconds, self._crawl_delay.get(host or "", 0.0))
        if self._last_request is not None:
            wait = delay - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    # -- robots.txt -------------------------------------------------------
    def _robots_for(self, url: str) -> RobotFileParser | None:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host in self._robots:
            return self._robots[host]
        robots_url = f"{parsed.scheme or 'https'}://{parsed.netloc}/robots.txt"
        parser = RobotFileParser()
        self._pace(host)
        try:
            _, body = self._fetch(robots_url, self.timeout)
            parser.parse(body.splitlines())
        except HTTPError as exc:
            if exc.code in (401, 403) or exc.code >= 500:
                parser.disallow_all = True
            else:
                parser.allow_all = True
        except (URLError, TimeoutError, OSError, ValueError):
            parser.disallow_all = True  # unknown policy: do not crawl
        delay = parser.crawl_delay(USER_AGENT)
        if delay:
            self._crawl_delay[host] = min(10.0, float(delay))
        self._robots[host] = parser
        return parser

    def _allowed(self, url: str) -> bool:
        parser = self._robots_for(url)
        return bool(parser and parser.can_fetch(USER_AGENT, url))

    # -- crawl ------------------------------------------------------------
    def collect(self, query: str, scope: str = "ticker") -> CrawlResult:
        result = CrawlResult(query=query)
        with TRACER.start_as_current_span("crawler_collect") as span:
            span.set_attribute("crawler.query", query)
            span.set_attribute("crawler.scope", scope)
            candidates = self._search_with_retry(query, result)
            for candidate in candidates:
                url = str(candidate.get("url") or candidate.get("href") or "").strip()
                title = str(candidate.get("title") or "").strip()
                if not url.startswith(("http://", "https://")):
                    continue
                trust = classify_trust(url)
                if len(result.items) >= self.max_items:
                    result.log.append(SourceLogEntry(url, trust, "not_needed", utc_now(), title=title,
                                                     note="MAX_NEWS_ITEMS reached; not crawled."))
                    continue
                self._read_page(candidate, url, title, trust, scope, result)
            for entry in result.log:
                entry.scope = scope
            span.set_attribute("crawler.items", len(result.items))
            span.set_attribute("crawler.blocked", sum(e.status == "blocked" for e in result.log))
        return result

    def _search_with_retry(self, query: str, result: CrawlResult) -> list[dict[str, Any]]:
        for attempt in (1, 2):
            self._pace()
            try:
                return list(self._search(query, self.safesearch, self.max_items * 4, self.timeout))
            except Exception as exc:  # provider raises many exception types
                logger.warning("News search failed (attempt %d/2): %s", attempt, type(exc).__name__)
                if attempt == 2:
                    result.log.append(SourceLogEntry(f"search: {query}", "n/a", "search_failed", utc_now(),
                                                     attempts=2, note=type(exc).__name__))
        return []

    def _read_page(self, candidate: dict[str, Any], url: str, title: str, trust: str,
                   scope: str, result: CrawlResult) -> None:
        host = urlparse(url).hostname or ""
        if not self._allowed(url):
            result.log.append(SourceLogEntry(url, trust, "robots_disallowed", utc_now(), title=title))
            return
        status: int | None = None
        for attempt in (1, 2):
            self._pace(host)
            with TRACER.start_as_current_span("crawler_request") as span:
                span.set_attribute("crawler.host", host)
                span.set_attribute("crawler.attempt", attempt)
                try:
                    status, body = self._fetch(url, self.timeout)
                    span.set_attribute("http.status_code", status)
                    if status in BLOCK_STATUSES:
                        raise HTTPError(url, status, "blocked", None, None)  # type: ignore[arg-type]
                    extractor = _TextExtractor()
                    extractor.feed(body)
                    self._item_counter += 1
                    prefix = "M" if scope == "macro" else "N"
                    item = NewsItem(
                        id=f"{prefix}{self._item_counter}",
                        title=title or " ".join(extractor.title.split()) or "Untitled",
                        url=url,
                        trust=trust,
                        published=str(candidate.get("date") or "").strip(),
                        snippet=str(candidate.get("body") or "").strip(),
                        page_text=extractor.text(),
                        fetched_at_utc=utc_now(),
                        scope=scope,
                    )
                    result.items.append(item)
                    result.log.append(SourceLogEntry(url, trust, "used", item.fetched_at_utc,
                                                     attempts=attempt, http_status=status, title=item.title))
                    return
                except HTTPError as exc:
                    status = exc.code
                    record_error(span, exc)
                except (URLError, TimeoutError, OSError, ValueError) as exc:
                    status = None
                    record_error(span, exc)
        result.log.append(SourceLogEntry(url, trust, "blocked", utc_now(), attempts=2, http_status=status,
                                         title=title, note="Blocked after one retry; skipped."))
