"""Fetch trusted, dated news with request pacing, retries, and trace diagnostics."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import urlparse

from ddgs import DDGS
from opentelemetry.trace import Status, StatusCode

from financial_alert.telemetry import get_tracer, record_error

logger = logging.getLogger(__name__)
TRACER = get_tracer(__name__)
TRUSTED_PUBLISHERS = {
    "bloomberg.com": "Bloomberg",
    "reuters.com": "Reuters",
    "wsj.com": "The Wall Street Journal",
    "cnbc.com": "CNBC",
}
TRUSTED_SITE_FILTER = "(site:bloomberg.com OR site:reuters.com OR site:wsj.com OR site:cnbc.com)"


class SearchRateLimiter:
    """Enforce a minimum interval between provider requests, including retries."""

    def __init__(self, minimum_interval_seconds: float) -> None:
        self.minimum_interval_seconds = max(2.0, minimum_interval_seconds)
        self._last_request_at: float | None = None
        self._lock = threading.Lock()

    def wait(self, retry_backoff_seconds: float = 0.0) -> None:
        with self._lock:
            now = time.monotonic()
            interval_wait = (
                max(0.0, self.minimum_interval_seconds - (now - self._last_request_at))
                if self._last_request_at is not None
                else 0.0
            )
            time.sleep(max(interval_wait, retry_backoff_seconds))
            self._last_request_at = time.monotonic()


class NewsResearchAgent:
    BACKENDS = ("duckduckgo", "bing")

    def __init__(
        self,
        max_results_per_query: int = 3,
        minimum_interval_seconds: float = 2.0,
        max_retries: int = 3,
    ) -> None:
        self.max_results_per_query = max(1, max_results_per_query)
        self.max_retries = max(1, max_retries)
        self.rate_limiter = SearchRateLimiter(minimum_interval_seconds)

    def research(
        self,
        queries_by_ticker: dict[str, list[dict[str, str]]],
    ) -> dict[str, dict[str, Any]]:
        """Return trusted source articles and explicit provider coverage per asset."""
        if not queries_by_ticker:
            logger.info("No portfolio or watch-list symbols; skipping news research.")
            return {}

        output: dict[str, dict[str, Any]] = {}
        with TRACER.start_as_current_span("fetch_all_market_news") as parent:
            parent.set_attribute("search.asset_count", len(queries_by_ticker))
            try:
                with DDGS() as search:
                    for symbol, searches in queries_by_ticker.items():
                        with TRACER.start_as_current_span(f"fetch_news_{symbol}") as asset_span:
                            asset_span.set_attribute("search.symbol", symbol)
                            asset_span.set_attribute("search.planned_query_count", len(searches))
                            try:
                                result = self._search_asset(search, symbol, searches, asset_span)
                                output[symbol] = result
                                asset_span.set_attribute("search.request_count", result["request_count"])
                                asset_span.set_attribute(
                                    "search.successful_query_count", result["successful_query_count"]
                                )
                                asset_span.set_attribute("search.article_count", len(result["articles"]))
                                asset_span.set_attribute("search.failed_query_count", len(result["failed_topics"]))
                                asset_span.set_attribute("search.rate_limited_count", result["rate_limited_count"])
                                asset_span.set_attribute("search.outcome", result["status"])
                                if result["status"] == "search_failed":
                                    asset_span.set_status(Status(StatusCode.ERROR, "All search requests failed."))
                            except Exception as exc:
                                record_error(asset_span, exc)
                                logger.exception("News search failed for %s.", symbol)
                                output[symbol] = self._failed_result(searches, "search_failed")
            except Exception as exc:
                record_error(parent, exc)
                logger.exception("Could not initialize the web search provider.")
                for symbol, searches in queries_by_ticker.items():
                    output.setdefault(symbol, self._failed_result(searches, "provider_error", str(exc)))

            parent.set_attribute(
                "search.request_count", sum(result["request_count"] for result in output.values())
            )
            parent.set_attribute(
                "search.failure_count", sum(result["failed_request_count"] for result in output.values())
            )
            parent.set_attribute(
                "search.rate_limited_count", sum(result["rate_limited_count"] for result in output.values())
            )
            parent.set_attribute(
                "search.article_count", sum(len(result["articles"]) for result in output.values())
            )
        return output

    def _search_asset(
        self,
        search: DDGS,
        symbol: str,
        searches: list[dict[str, str]],
        asset_span: Any,
    ) -> dict[str, Any]:
        articles: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        failed_queries: list[str] = []
        failure_reasons: list[str] = []
        successful_queries = request_count = failed_request_count = rate_limited_count = 0

        for search_item in searches:
            topic = search_item["topic"]
            query = f"{search_item['query']} {TRUSTED_SITE_FILTER}"
            trusted_results: list[dict[str, Any]] = []
            query_succeeded = False

            for backend in self.BACKENDS:
                backend_succeeded = False
                for attempt in range(self.max_retries):
                    backoff = 0.0 if attempt == 0 else 2.0 ** (attempt - 1)
                    self.rate_limiter.wait(backoff)
                    request_count += 1
                    with TRACER.start_as_current_span("search_request") as request_span:
                        request_span.set_attribute("search.backend", backend)
                        request_span.set_attribute("search.symbol", symbol)
                        request_span.set_attribute("search.topic", topic)
                        request_span.set_attribute("search.attempt", attempt + 1)
                        request_span.set_attribute("search.query", query)
                        try:
                            results = search.text(query, max_results=self.max_results_per_query, backend=backend) or []
                            trusted_results = [
                                result for result in results
                                if self._publisher_name(str(result.get("href") or "")) is not None
                            ]
                            request_span.set_attribute("search.result_count", len(results))
                            request_span.set_attribute("search.trusted_result_count", len(trusted_results))
                            backend_succeeded = True
                            query_succeeded = True
                            break
                        except Exception as exc:
                            failed_request_count += 1
                            is_rate_limited = self._is_rate_limited(exc)
                            rate_limited_count += int(is_rate_limited)
                            request_span.set_attribute("search.rate_limited", is_rate_limited)
                            record_error(request_span, exc)
                            failure_reasons.append(f"{topic}: {backend} {type(exc).__name__}")
                            logger.warning(
                                "Search failure for %s via %s, attempt %d/%d (%s).",
                                symbol, backend, attempt + 1, self.max_retries, type(exc).__name__,
                            )

                if backend_succeeded and trusted_results:
                    break

            if query_succeeded:
                successful_queries += 1
                for result in trusted_results:
                    body = str(result.get("body") or "").strip()
                    url = str(result.get("href") or "").strip()
                    publisher = self._publisher_name(url)
                    if not body or not url or not publisher or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    articles.append(
                        {
                            "title": str(result.get("title") or "Untitled source").strip(),
                            "summary": body,
                            "url": url,
                            "publisher": publisher,
                            "published": str(result.get("date") or "").strip(),
                            "topic": topic,
                            "query": query,
                        }
                    )
            else:
                failed_queries.append(topic)
                logger.warning("All search backends failed for %s (%s).", symbol, topic)

        status = self._status(len(searches), successful_queries, len(articles))
        if status == "search_failed":
            asset_span.set_status(Status(StatusCode.ERROR, "All search requests failed."))
        return {
            "status": status,
            "query_count": len(searches),
            "successful_query_count": successful_queries,
            "failed_topics": failed_queries,
            "failure_reasons": failure_reasons,
            "request_count": request_count,
            "failed_request_count": failed_request_count,
            "rate_limited_count": rate_limited_count,
            "articles": articles,
        }

    @staticmethod
    def _publisher_name(url: str) -> str | None:
        host = urlparse(url).hostname or ""
        host = host.lower().removeprefix("www.")
        for domain, name in TRUSTED_PUBLISHERS.items():
            if host == domain or host.endswith(f".{domain}"):
                return name
        return None

    @staticmethod
    def _is_rate_limited(error: Exception) -> bool:
        message = f"{type(error).__name__} {error}".lower()
        return "429" in message or "rate limit" in message or "ratelimit" in message or "too many requests" in message

    @staticmethod
    def _failed_result(
        searches: list[dict[str, str]],
        status: str,
        error: str | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": status,
            "query_count": len(searches),
            "successful_query_count": 0,
            "failed_topics": [item["topic"] for item in searches],
            "failure_reasons": [error] if error else [],
            "request_count": 0,
            "failed_request_count": 0,
            "rate_limited_count": 0,
            "articles": [],
        }
        if error:
            result["provider_error"] = error
        return result

    @staticmethod
    def _status(total_queries: int, successful_queries: int, article_count: int) -> str:
        if successful_queries == 0:
            return "search_failed"
        if article_count == 0:
            return "no_results"
        if successful_queries < total_queries:
            return "partial"
        return "complete"
