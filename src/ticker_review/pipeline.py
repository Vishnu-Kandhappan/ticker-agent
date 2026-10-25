"""Run the three agents for each ticker and save one report per ticker."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from financial_alert.config import Portfolio, load_portfolio
from financial_alert.telemetry import get_tracer
from ticker_review.agents.report_composer import ReportComposerAgent
from ticker_review.agents.research_agent import TickerResearchAgent
from ticker_review.agents.strategy_analyzer import TickerStrategyAnalyzer
from ticker_review.crawler import CrawlResult, SafeCrawler
from ticker_review.llm import LocalLLM
from ticker_review.market import MarketData
from ticker_review.settings import ReviewSettings

logger = logging.getLogger(__name__)
TRACER = get_tracer(__name__)
MACRO_QUERIES = {
    "US": "US economy inflation Federal Reserve interest rates GDP geopolitical risk stock market",
    "India": "India economy inflation RBI interest rates GDP geopolitical risk Sensex Nifty",
}


@dataclass
class ReviewOutput:
    symbol: str
    report: str
    path: Path


def is_india(symbol: str) -> bool:
    return symbol.upper().endswith((".NS", ".BO"))


def metadata_for(symbol: str, portfolio: Portfolio | None) -> dict[str, Any]:
    if portfolio is None:
        return {"role": "ad hoc"}
    role = ("holding" if symbol in portfolio.assets else
            "watch list" if symbol in portfolio.watch_list else "ad hoc")
    return {"role": role, "manual_updates": portfolio.manual_updates}


class TickerReviewPipeline:
    def __init__(self, settings: ReviewSettings, market: MarketData | None = None,
                 crawler: SafeCrawler | None = None, llm: LocalLLM | None | bool = True) -> None:
        self.settings = settings
        self.market = market or MarketData()
        self.crawler = crawler or SafeCrawler(settings.max_news_items, settings.crawler_sleep_ms,
                                              settings.safe_search, settings.crawler_timeout_ms)
        if llm is True:
            llm = LocalLLM(settings.llm_base_url, settings.llm_model, settings.llm_timeout_seconds)
        self.llm = llm or None
        self.research = TickerResearchAgent(settings.prompt("ticker-research-agent"), self.llm)
        self.analyzer = TickerStrategyAnalyzer(settings.prompt("ticker-strategy-analyzer"), settings.skill(), self.llm)
        self.composer = ReportComposerAgent()
        settings.prompt("report-composer-agent")  # fail early if the contract file is missing
        # One session per run groups the per-ticker traces (Langfuse: sessions; others: filter on session.id).
        self.run_id = f"ticker-review-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        self._macro: dict[str, CrawlResult] = {}
        self._regime: dict[str, Any] | None = None

    def _macro_crawl(self, market: str) -> CrawlResult:
        if market not in self._macro:
            self._macro[market] = self.crawler.collect(MACRO_QUERIES[market], scope="macro")
        return self._macro[market]

    def review(self, symbol: str, portfolio: Portfolio | None = None) -> ReviewOutput:
        attributes = {"ticker.symbol": symbol, "session.id": self.run_id, "langfuse.session.id": self.run_id,
                      "langfuse.trace.name": f"ticker-review {symbol}",
                      "app.llm.enabled": self.llm is not None}
        with TRACER.start_as_current_span("ticker_review", attributes=attributes) as span:
            logger.info("Reviewing %s.", symbol)
            snapshot = self.market.snapshot(symbol)
            if self._regime is None:
                self._regime = self.market.regime()
            sector = self.market.sector(snapshot["fundamentals"].get("sector"), snapshot["technical"].get("perf_1m"))
            name = snapshot["fundamentals"].get("name") or symbol
            query = f"{name} {symbol.split('.')[0]} stock" if name != symbol else f"{symbol} stock"
            ticker_crawl = self.crawler.collect(query, scope="ticker")
            macro_crawl = self._macro_crawl("India" if is_india(symbol) else "US")

            research = self.research.run(symbol, metadata_for(symbol, portfolio), snapshot, self._regime,
                                         sector, ticker_crawl, macro_crawl)
            analysis = self.analyzer.run(research)
            report = self.composer.run(research, analysis)
            best = analysis["best"]
            span.set_attributes({
                "ticker.movement": analysis["movement"]["label"],
                "ticker.movement_confidence": analysis["confidence"]["level"],
                "ticker.best_strategy": best.name if best else "none",
                "ticker.strategy_confidence": best.confidence if best else 0,
                "ticker.setup": bool(best and best.is_setup),
                "ticker.news_items": len(research["news"]),
                "ticker.blocked_sources": sum(e["status"] == "blocked" for e in research["source_log"]),
                "ticker.intuition_source": analysis["intuition_source"],
            })
            path = self._save(symbol, report)
            return ReviewOutput(symbol, report, path)

    def _save(self, symbol: str, report: str) -> Path:
        self.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", symbol)
        path = self.settings.artifacts_dir / f"ticker-review-{safe}-{date.today().isoformat()}.md"
        path.write_text(report, encoding="utf-8")
        logger.info("Saved %s.", path)
        return path


def portfolio_symbols(settings: ReviewSettings) -> tuple[list[str], Portfolio]:
    portfolio = load_portfolio(settings.portfolio_path)
    # Strategies are for equities; precious metals are skipped.
    return list(dict.fromkeys([*portfolio.assets, *portfolio.watch_list])), portfolio
