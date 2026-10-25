"""Compile verified workflow state into the portfolio briefing Markdown contract."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from financial_alert.config import Portfolio


class AgentOrchestrator:
    """Render the final report; keep metrics and source diagnostics out of prose."""

    CURRENCY_SYMBOLS = {"USD": "$", "INR": "₹", "GBP": "£", "EUR": "€"}

    def compile_report(
        self,
        portfolio: Portfolio,
        market_data: dict[str, dict[str, Any]],
        research: dict[str, dict[str, Any]],
        catalysts: dict[str, str],
        as_of: date | None = None,
    ) -> str:
        symbols = portfolio.symbols
        report_date = (as_of or date.today()).isoformat()
        successful_count = sum(market_data.get(symbol, {}).get("status") == "available" for symbol in symbols)
        return "\n\n".join(
            [
                f"# 📊 Daily Portfolio Briefing\n**Date:** {report_date} | **Coverage:** "
                f"{successful_count} of {len(symbols)} assets successfully analyzed.",
                "## 📝 Executive Summary\n" + self._executive_summary(
                    symbols, market_data, research, portfolio.manual_updates, successful_count
                ),
                "## 📈 Metrics Dashboard\n" + self._metrics_table(symbols, market_data),
                "## 🔍 Catalyst Analysis (Why it moved)\n" + self._catalyst_section(symbols, market_data, catalysts),
                "## 🛠️ Portfolio Strategy\n" + self._strategy(portfolio.manual_updates),
                "---\n\n## ⚙️ Appendix: Diagnostics & Sources\n" + self._appendix(
                    symbols, market_data, research, portfolio.manual_updates
                ),
            ]
        )

    def _executive_summary(
        self,
        symbols: list[str],
        market_data: dict[str, dict[str, Any]],
        research: dict[str, dict[str, Any]],
        manual_note: str,
        successful_count: int,
    ) -> str:
        available = [
            (symbol, market_data[symbol])
            for symbol in symbols
            if market_data.get(symbol, {}).get("status") == "available"
        ]
        if available:
            changes = [
                (symbol, snapshot.get("change_5_sessions_percent"))
                for symbol, snapshot in available
                if isinstance(snapshot.get("change_5_sessions_percent"), (int, float))
            ]
            if changes:
                strongest = max(changes, key=lambda item: item[1])
                weakest = min(changes, key=lambda item: item[1])
                if len(changes) == 1:
                    performance = f"{strongest[0]} changed {strongest[1]:+.2f}% over five sessions."
                else:
                    performance = (
                        f"Over five sessions, {strongest[0]} had the largest gain ({strongest[1]:+.2f}%) "
                        f"and {weakest[0]} had the largest decline ({weakest[1]:+.2f}%)."
                    )
            else:
                performance = "The available prices do not include enough history to compare five-session performance."
        else:
            performance = "No configured asset returned usable price data for this briefing."

        macro_topics = {"GDP, inflation, and interest rates", "Industry trends and geopolitical risk"}
        macro_articles_by_url = {
            article.get("url"): article
            for result in research.values()
            for article in result.get("articles", [])
            if article.get("topic") in macro_topics and article.get("url")
        }
        macro_articles = list(macro_articles_by_url.values())
        coverage_and_performance = (
            f"Price data is available for {successful_count} of {len(symbols)} configured assets; {performance}"
        )
        macro_and_note = "Macro and geopolitical searches returned no trusted source reports"
        if macro_articles:
            headlines = "; ".join(
                f"{article.get('publisher', 'Publisher not supplied')} reported “{self._safe_headline(article.get('title', 'Untitled source'))}”"
                for article in macro_articles[:2]
            )
            macro_and_note = f"Macro and geopolitical coverage included: {headlines}"
        note = self._safe_note(manual_note)
        if note:
            macro_and_note += "; your manual note is user-provided and is not independently verified"
        else:
            macro_and_note += "; no manual portfolio note was provided"
        return f"{coverage_and_performance} {macro_and_note}."

    def _metrics_table(self, symbols: list[str], market_data: dict[str, dict[str, Any]]) -> str:
        rows = [
            "| Ticker | Close Price | 5-Day Trend | 20-Day Trend | Range Position | Momentum Status |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
        for symbol in symbols:
            snapshot = market_data.get(symbol, {})
            if snapshot.get("status") != "available":
                rows.append(f"| **{symbol}** | Unavailable | N/A | N/A | N/A | ⚪ Unavailable |")
                continue
            currency_code = snapshot.get("quote_currency")
            currency = self.CURRENCY_SYMBOLS.get(currency_code)
            close = snapshot.get("latest_close")
            if isinstance(close, (int, float)):
                close_text = (
                    f"{currency}{close:,.2f}"
                    if currency
                    else f"{currency_code} {close:,.2f}" if currency_code else f"{close:,.2f} (currency unavailable)"
                )
            else:
                close_text = "N/A"
            five_day = self._format_percent(snapshot.get("change_5_sessions_percent"))
            twenty_day = self._format_percent(snapshot.get("change_20_sessions_percent"))
            position = snapshot.get("range_position_percent")
            range_text = self._range_position(position)
            status = self._momentum_status(snapshot.get("change_5_sessions_percent"))
            rows.append(
                f"| **{symbol}** | {close_text} | {five_day} | {twenty_day} | {range_text} | {status} |"
            )
        return "\n".join(rows)

    def _catalyst_section(
        self,
        symbols: list[str],
        market_data: dict[str, dict[str, Any]],
        catalysts: dict[str, str],
    ) -> str:
        lines: list[str] = []
        for symbol in symbols:
            snapshot = market_data.get(symbol, {})
            status = self._momentum_status(snapshot.get("change_5_sessions_percent"))
            explanation = catalysts.get(symbol, "No clear catalyst found in current news sources.")
            lines.append(f"* **{symbol} ({status.split()[0]} {status.split()[-1]}):** {explanation}")
        return "\n".join(lines) if lines else "No assets are configured."

    @staticmethod
    def _strategy(manual_note: str) -> str:
        note = AgentOrchestrator._safe_note(manual_note)
        if note:
            return (
                f"Regarding your note: {note}. Treat it as unconfirmed until an official company or regulator "
                "source verifies the event and date. Review current results, debt, cash generation, and position "
                "size before you act. A short-term price move or a low one-year range position does not establish value."
            )
        return (
            "No manual portfolio update was provided. Review company results, debt, cash generation, diversification, "
            "and position size before changing an investment. A short-term price move or a low one-year range "
            "position does not establish value."
        )

    def _appendix(
        self,
        symbols: list[str],
        market_data: dict[str, dict[str, Any]],
        research: dict[str, dict[str, Any]],
        manual_note: str,
    ) -> str:
        lines: list[str] = []
        for symbol in symbols:
            snapshot = market_data.get(symbol, {})
            result = research.get(symbol, {})
            if snapshot.get("status") == "available":
                market_diagnostics = (
                    f"Market data: available from {snapshot.get('source', 'source not supplied')}; "
                    f"provider symbol {snapshot.get('provider_symbol', symbol)}; "
                    f"last daily bar {snapshot.get('history_as_of', 'date not supplied')}; "
                    f"retrieved {snapshot.get('retrieved_at_utc', 'time not supplied')} UTC."
                )
            else:
                market_diagnostics = (
                    f"Market data: {snapshot.get('status', 'unavailable')}; "
                    f"reason: {snapshot.get('reason', 'not supplied')}; "
                    f"provider symbol {snapshot.get('provider_symbol', symbol)}."
                )
            lines.append(
                f"* **{symbol}:** {market_diagnostics} Search {result.get('status', 'not run')}; "
                f"{result.get('successful_query_count', 0)} of {result.get('query_count', 0)} queries completed; "
                f"{len(result.get('articles', []))} trusted sources returned; "
                f"{result.get('request_count', 0)} requests, {result.get('failed_request_count', 0)} failures, "
                f"{result.get('rate_limited_count', 0)} rate-limit responses."
            )
            if result.get("failed_topics"):
                lines.append(f"  * Failed topics: {', '.join(result['failed_topics'])}.")
            if result.get("failure_reasons"):
                lines.append(f"  * Search errors: {', '.join(result['failure_reasons'])}.")
            if not result.get("articles"):
                lines.append("  * No source was returned. This is not evidence that no event occurred.")
            for article in result.get("articles", []):
                title = re.sub(r"[\r\n]+", " ", str(article.get("title") or "Untitled source"))
                publisher = article.get("publisher", "Publisher not supplied")
                published = article.get("published") or "date not supplied"
                url = str(article.get("url") or "")
                if url:
                    lines.append(f"  * [{publisher}: {title}]({url}) ({published}).")

        note_urls = self._urls(manual_note)
        if note_urls:
            lines.append("* **Links supplied in the portfolio note:**")
            lines.extend(f"  * {url}" for url in note_urls)

        lines.extend(
            [
                "",
                "### Data limits",
                "* Coverage counts configured assets with available daily price data.",
                "* Prices can be delayed. The table uses the latest daily close returned by the provider.",
                "* The direction label describes the sign of the five-session change. It is not a forecast or a trade recommendation.",
                "* The local model can fail or return an unverified statement. The catalyst agent accepts only a cited publisher returned by search; otherwise it uses the no-catalyst fallback.",
                "* Search results can be stale or incomplete. Verify publication dates and official filings.",
                "* Gold uses continuous futures ticker GC=F. It is not the spot XAU/USD price.",
                "* This report is educational. It is not personal investment advice.",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _safe_note(note: str) -> str:
        cleaned = re.sub(r"https?://\S+", "[link in appendix]", note, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", cleaned).strip().strip('"“”')

    @staticmethod
    def _urls(text: str) -> list[str]:
        return re.findall(r"https?://[^\s)]+", text, flags=re.IGNORECASE)

    @staticmethod
    def _safe_headline(title: Any) -> str:
        title = re.sub(r"https?://\S+", "[link omitted]", str(title), flags=re.IGNORECASE)
        title = re.sub(r"[\r\n.!?]+", " ", title)
        return re.sub(r"\s+", " ", title).strip()

    @staticmethod
    def _format_percent(value: Any) -> str:
        return f"{value:+.2f}%" if isinstance(value, (int, float)) else "N/A"

    @staticmethod
    def _range_position(position: Any) -> str:
        if not isinstance(position, (int, float)):
            return "N/A"
        if position >= 80:
            zone = "High"
        elif position <= 20:
            zone = "Low"
        else:
            zone = "Middle"
        return f"{zone} ({position:.0f}%)"

    @staticmethod
    def _momentum_status(change: Any) -> str:
        if not isinstance(change, (int, float)):
            return "⚪ Unavailable"
        if change > 0:
            return "🟢 Positive"
        if change < 0:
            return "🔴 Negative"
        return "🟡 Flat"

