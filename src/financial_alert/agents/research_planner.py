"""Choose ticker-specific research topics for the current financial season."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ResearchPlan:
    season_by_ticker: dict[str, str]
    queries_by_ticker: dict[str, list[dict[str, str]]]


class ResearchPlannerAgent:
    """Build a current, market-aware search plan for each holding and candidate."""

    def plan(
        self,
        assets: list[str],
        watch_list: list[str],
        as_of: date | None = None,
        precious_metals: list[str] | None = None,
    ) -> ResearchPlan:
        today = as_of or date.today()
        precious_metals = precious_metals or []
        queries_by_ticker: dict[str, list[dict[str, str]]] = {}
        season_by_ticker: dict[str, str] = {}

        for symbol in dict.fromkeys([*assets, *watch_list, *precious_metals]):
            is_india = symbol.upper().endswith((".NS", ".BO"))
            is_gold = "gold" in symbol.lower() or "xau" in symbol.lower()
            market = "India" if is_india else "US and global markets"
            current_month = today.strftime("%B")
            current_year = today.year
            current_date = today.isoformat()
            dated_period = f"{current_month} {current_year}; as of {current_date}"

            if is_gold:
                season_by_ticker[symbol] = "macro and commodity monitoring"
                company_query = (
                    f"gold market latest price drivers central bank purchases ETF flows "
                    f"geopolitical risk product demand {dated_period}"
                )
                sector_query = (
                    f"gold industry trends mine supply demand jewelry technology innovation "
                    f"{dated_period}"
                )
            else:
                season_by_ticker[symbol] = (
                    f"{market} reporting period; confirm company-specific fiscal calendar"
                )
                company_query = (
                    f"{symbol} official investor relations upcoming earnings date quarterly results "
                    f"financial results guidance corporate actions product innovation "
                    f"{current_month} {current_year} {current_date}; "
                    "confirm fiscal year and quarter from company source"
                )
                sector_query = (
                    f"{symbol} industry sector trends competitors supply chain demand "
                    f"geopolitical events GDP growth {dated_period}"
                )

            sentiment_liquidity_query = (
                f"{symbol} market sentiment media coverage trading volume liquidity "
                f"unusual activity {dated_period}"
            )
            if is_india:
                macro_query = (
                    f"India GDP inflation CPI RBI interest rates geopolitical events "
                    f"market impact {dated_period}"
                )
            else:
                macro_query = (
                    f"US GDP inflation CPI Federal Reserve interest rates geopolitical events "
                    f"market impact {dated_period}"
                )

            queries_by_ticker[symbol] = [
                {"topic": "Company events and products", "query": company_query},
                {"topic": "Industry trends and geopolitical risk", "query": sector_query},
                {"topic": "Market sentiment, media, and liquidity", "query": sentiment_liquidity_query},
                {"topic": "GDP, inflation, and interest rates", "query": macro_query},
            ]

        return ResearchPlan(season_by_ticker=season_by_ticker, queries_by_ticker=queries_by_ticker)
