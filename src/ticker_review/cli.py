"""Command line: python ticker-review.py [TICKER ...] [--slack] [--no-llm]."""

from __future__ import annotations

import argparse
import logging
import os

from financial_alert.config import load_portfolio
from financial_alert.telemetry import initialize_telemetry, shutdown_telemetry
from ticker_review.pipeline import TickerReviewPipeline, portfolio_symbols
from ticker_review.settings import ReviewSettings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Three-agent ticker review (ASD-STE100 report).")
    parser.add_argument("tickers", nargs="*", help="Tickers to review. Default: asset + watch_list in portfolio.json.")
    parser.add_argument("--slack", action="store_true", help="Also post each report to SLACK_WEBHOOK_URL.")
    parser.add_argument("--no-llm", action="store_true", help="Skip the local model; use rule-based text only.")
    parser.add_argument("--quiet", action="store_true", help="Do not print the reports.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("ticker_review")
    try:
        settings = ReviewSettings.from_env()
        initialize_telemetry(os.getenv("TICKER_REVIEW_SERVICE_NAME", "ticker-review"))
        if args.tickers:
            symbols = [t.strip().upper() for t in args.tickers if t.strip()]
            try:
                portfolio = load_portfolio(settings.portfolio_path)
            except ValueError:
                portfolio = None
        else:
            symbols, portfolio = portfolio_symbols(settings)
        if not symbols:
            log.error("No tickers given and none found in %s.", settings.portfolio_path)
            return 1
        pipeline = TickerReviewPipeline(settings, llm=not args.no_llm)
        failures = 0
        for symbol in symbols:
            try:
                output = pipeline.review(symbol, portfolio)
            except Exception:  # keep going with the next ticker
                failures += 1
                log.exception("Review failed for %s.", symbol)
                continue
            if not args.quiet:
                print(output.report)
            if args.slack and settings.slack_webhook_url:
                from financial_alert.slack import post_to_slack

                post_to_slack(settings.slack_webhook_url, output.report)
        return 1 if failures == len(symbols) else 0
    except (RuntimeError, ValueError) as exc:
        log.error("Ticker review failed: %s", exc)
        return 1
    finally:
        shutdown_telemetry()
