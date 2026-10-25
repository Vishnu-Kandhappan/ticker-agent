"""Ticker Research Agent (prompt: agents/ticker-research-agent.prompt).

The program collects and calculates every number. The local model only
classifies crawled news per factor. Its output is validated and falls back to
keyword rules when the model is unavailable or its output fails a check.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from financial_alert.telemetry import get_tracer
from financial_alert.genai_telemetry import record_validation
from ticker_review.crawler import CrawlResult, NewsItem
from ticker_review.formatting import money_compact, pct, qty
from ticker_review.llm import LocalLLM
from ticker_review import ste

logger = logging.getLogger(__name__)
TRACER = get_tracer(__name__)
AGENT = "ticker_research_agent"

FACTORS: dict[str, str] = {
    "geopolitical": "Geopolitical events",
    "volume_liquidity": "Trading volume and liquidity",
    "sentiment_media": "Market sentiment and media coverage",
    "economic_health": "Economic health (GDP signals)",
    "sector_trends": "Industry sector trends",
    "inflation": "Inflation",
    "interest_rates": "Interest rates",
    "corporate_actions": "Corporate actions",
    "product_innovation": "Product innovations",
}
NEWS_FACTORS = ["geopolitical", "sentiment_media", "economic_health", "inflation",
                "interest_rates", "corporate_actions", "product_innovation"]
MACRO_FACTORS = {"geopolitical", "economic_health", "inflation", "interest_rates"}
KEYWORDS = {
    "geopolitical": r"tariff|sanction|war\b|conflict|geopolit|election|export control|trade (deal|war)",
    "sentiment_media": r"upgrade|downgrade|analyst|rally|slump|sell-?off|investors|sentiment|price target",
    "economic_health": r"\bgdp\b|economy|economic growth|recession|jobs report|payroll|unemployment",
    "inflation": r"inflation|\bcpi\b|\bpce\b|consumer prices",
    "interest_rates": r"interest rate|rate (cut|hike)|federal reserve|\bfed\b|\brbi\b|treasury yield|bond yield",
    "corporate_actions": r"earnings|dividend|buyback|repurchase|acquisition|merger|stock split|guidance|results",
    "product_innovation": r"launch|unveil|new product|\bchip\b|model|platform|release|patent|innovation",
}
EFFECTS = {"positive", "negative", "neutral", "no_data"}


class TickerResearchAgent:
    def __init__(self, system_prompt: str, llm: LocalLLM | None) -> None:
        self.system_prompt = system_prompt
        self.llm = llm

    def run(self, symbol: str, metadata: dict[str, Any], market: dict[str, Any], regime: dict[str, Any],
            sector: dict[str, Any], ticker_crawl: CrawlResult, macro_crawl: CrawlResult) -> dict[str, Any]:
        with TRACER.start_as_current_span("ticker_research_agent") as span:
            span.set_attribute("ticker.symbol", symbol)
            tech, fund, earn = market["technical"], market["fundamentals"], market["earnings"]
            news = [*ticker_crawl.items, *macro_crawl.items]
            factors = self._news_factors(symbol, ticker_crawl.items, macro_crawl.items)
            factors["volume_liquidity"] = self._volume_factor(tech)
            factors["sector_trends"] = self._sector_factor(sector)
            factors["corporate_actions"] = self._corporate_factor(earn, factors.get("corporate_actions"))
            ordered = {key: {"label": FACTORS[key], **factors[key]} for key in FACTORS}
            direction = self._direction(tech, ordered)
            span.set_attribute("research.news_items", len(news))
            span.set_attribute("research.direction", direction["label"])
            return {
                "symbol": symbol,
                "metadata": metadata,
                "name": fund.get("name") or symbol,
                "currency": fund.get("currency") or ("INR" if symbol.upper().endswith((".NS", ".BO")) else "USD"),
                "market": market,
                "regime": regime,
                "sector": sector,
                "price_summary": self._price_summary(tech, fund),
                "factors": ordered,
                "direction": direction,
                "news": [item.as_dict() for item in news],
                "source_log": [entry.as_dict() for entry in (*ticker_crawl.log, *macro_crawl.log)],
                "queries": [ticker_crawl.query, macro_crawl.query],
                "data_gaps": self._data_gaps(tech, fund, earn, regime, sector, news),
                "classifier": factors.pop("_classifier", "rules"),
            }

    # -- 1. price summary -------------------------------------------------
    @staticmethod
    def _price_summary(tech: dict, fund: dict) -> dict[str, Any]:
        if tech.get("status") != "available":
            return {"status": "unavailable", "notes": [tech.get("reason", "No price data.")]}
        currency = fund.get("currency")
        change = None
        if tech.get("close") and tech.get("prev_close"):
            change = round((tech["close"] / tech["prev_close"] - 1) * 100, 2)
        notes = []
        avg20, turnover = tech.get("avg_volume_20"), tech.get("avg_turnover_20")
        if avg20 is not None:
            level = "high" if avg20 >= 1_000_000 else ("medium" if avg20 >= 300_000 else "low")
            notes.append(f"Liquidity is {level}. The 20-session average volume is {qty(avg20)} shares.")
        if turnover is not None:
            notes.append(f"The 20-session average turnover is {money_compact(turnover, currency)} per session.")
        if tech.get("rel_volume") is not None:
            notes.append(f"Last session volume was {tech['rel_volume']}x the 50-session average.")
        return {
            "status": "available", "as_of": tech.get("as_of"), "currency": currency,
            "open": tech.get("open"), "high": tech.get("high"), "low": tech.get("low"), "close": tech.get("close"),
            "change_pct": change, "volume": tech.get("volume"), "notes": notes,
        }

    # -- 2. factors -------------------------------------------------------
    def _news_factors(self, symbol: str, ticker_items: list[NewsItem], macro_items: list[NewsItem]) -> dict[str, Any]:
        all_items = {item.id: item for item in (*ticker_items, *macro_items)}
        result: dict[str, Any] = {key: self._keyword_factor(key, ticker_items, macro_items) for key in NEWS_FACTORS}
        result["_classifier"] = "rules"
        if not all_items or self.llm is None:
            return result
        payload = {
            "ticker": symbol,
            "factors": {key: FACTORS[key] for key in NEWS_FACTORS},
            "news": [{"id": i.id, "scope": i.scope, "title": i.title, "trust": i.trust,
                      "published": i.published or "date not supplied", "text": i.text[:600]}
                     for i in all_items.values()],
        }
        reply = self.llm.chat_json(AGENT, self.system_prompt, payload)
        if reply is None:
            return result  # unavailable or invalid JSON: already counted by the client
        factors = reply.get("factors")
        if not isinstance(factors, dict):
            record_validation(AGENT, "rejected", "missing_factors_object")
            return result
        accepted = 0
        for key in NEWS_FACTORS:
            checked, reason = self._validate_factor(factors.get(key), all_items, key)
            record_validation(AGENT, "accepted" if checked else "rejected", reason, item=key)
            if checked is not None:
                result[key] = checked
                accepted += 1
        result["_classifier"] = f"local model ({accepted} of {len(NEWS_FACTORS)} factors accepted)"
        return result

    @staticmethod
    def _validate_factor(value: Any, items: dict[str, NewsItem], key: str) -> tuple[dict[str, Any] | None, str | None]:
        """Return (factor, None) when the output passes, or (None, reason) when it fails."""
        if not isinstance(value, dict):
            return None, "missing_factor"
        effect = str(value.get("effect", "")).strip().lower()
        observation = re.sub(r"\s+", " ", str(value.get("observation", ""))).strip()
        raw_evidence = value.get("evidence") if isinstance(value.get("evidence"), list) else []
        evidence = [str(e) for e in raw_evidence if str(e) in items]
        if effect not in EFFECTS:
            return None, "invalid_effect"
        if effect == "no_data":
            return {"effect": "no_data", "observation": "No crawled source reports on this factor.", "evidence": []}, None
        if len(evidence) < len(raw_evidence):
            return None, "unknown_evidence_id"
        if not evidence:
            return None, "no_evidence"
        if not observation:
            return None, "empty_observation"
        if len(ste.sentences(observation)) > 2 or ste.issues(observation, 25):
            return None, "ste_violation"
        if key not in MACRO_FACTORS and all(items[e].scope == "macro" for e in evidence):
            return None, "macro_only_evidence"  # company factors need company news
        source_text = " ".join(f"{items[e].title} {items[e].text}" for e in evidence)
        if not ste.numbers_grounded(observation, source_text):
            return None, "ungrounded_number"
        return {"effect": effect, "observation": observation, "evidence": evidence}, None

    @staticmethod
    def _keyword_factor(key: str, ticker_items: list[NewsItem], macro_items: list[NewsItem]) -> dict[str, Any]:
        pool = [*ticker_items, *(macro_items if key in MACRO_FACTORS else [])]
        hits = [i for i in pool if re.search(KEYWORDS[key], f"{i.title} {i.text}", re.IGNORECASE)]
        if not hits:
            return {"effect": "no_data", "observation": "No crawled source reports on this factor.", "evidence": []}
        ids = ", ".join(i.id for i in hits)
        return {"effect": "mentioned", "evidence": [i.id for i in hits],
                "observation": f"{'Sources' if len(hits) > 1 else 'Source'} {ids} {'report' if len(hits) > 1 else 'reports'} on this topic. The program did not classify the direction."}

    @staticmethod
    def _volume_factor(tech: dict) -> dict[str, Any]:
        rel, avg20 = tech.get("rel_volume"), tech.get("avg_volume_20")
        if rel is None:
            return {"effect": "no_data", "observation": "No volume data.", "evidence": [], "measure": None}
        down = tech.get("down_day")
        effect = "neutral"
        if rel >= 1.2:
            effect = "negative" if down else "positive"
        direction = "down" if down else "up"
        return {
            "effect": effect, "evidence": [], "measure": rel,
            "observation": (f"Last session volume was {rel}x the 50-session average on an {direction} day. "
                            f"The 20-session average is {qty(avg20)} shares."),
        }

    @staticmethod
    def _sector_factor(sector: dict) -> dict[str, Any]:
        etf, gap = sector.get("etf"), sector.get("etf_vs_spy_1m")
        if not etf or gap is None:
            return {"effect": "no_data", "evidence": [], "measure": None,
                    "observation": "No sector ETF data for this ticker."}
        return {
            "effect": "positive" if gap > 0 else ("negative" if gap < 0 else "neutral"), "evidence": [], "measure": gap,
            "observation": (f"{etf} changed {pct(sector.get('etf_perf_1m'))} in 1 month. "
                            f"SPY changed {pct(sector.get('spy_perf_1m'))}."),
        }

    @staticmethod
    def _corporate_factor(earn: dict, news_factor: dict | None) -> dict[str, Any]:
        sentences, effect = [], "no_data"
        since, to_next, surprise = earn.get("sessions_since_last"), earn.get("sessions_to_next"), earn.get("last_surprise_pct")
        if to_next is not None:
            sentences.append(f"The next earnings date is {earn['next_date']}, in {to_next} sessions.")
            effect = "neutral"
        if earn.get("last_date"):
            text = f"The last report was on {earn['last_date']}"
            text += f" with an EPS surprise of {pct(surprise)}." if surprise is not None else "."
            sentences.append(text)
            if since is not None and since <= 10 and surprise is not None:
                effect = "positive" if surprise > 0 else "negative"
            elif effect == "no_data":
                effect = "neutral"
        evidence: list[str] = []
        if news_factor and news_factor.get("effect") not in (None, "no_data"):
            sentences.append(news_factor["observation"])
            evidence = news_factor.get("evidence", [])
            if effect in ("no_data", "neutral") and news_factor["effect"] in ("positive", "negative"):
                effect = news_factor["effect"]
            elif effect == "no_data":
                effect = news_factor["effect"]
        if not sentences:
            return {"effect": "no_data", "observation": "No earnings dates or corporate news found.", "evidence": []}
        return {"effect": effect, "observation": " ".join(sentences), "evidence": evidence,
                "measure": {"sessions_to_next": to_next, "sessions_since_last": since}}

    # -- 3. direction -----------------------------------------------------
    @staticmethod
    def _direction(tech: dict, factors: dict) -> dict[str, Any]:
        votes: list[tuple[str, int, str]] = []
        p5 = tech.get("perf_5d")
        if p5 is not None:
            vote = 1 if p5 > 1 else (-1 if p5 < -1 else 0)
            votes.append(("5-session change", vote, f"The price changed {pct(p5)} in 5 sessions."))
        if tech.get("close") is not None and tech.get("ema21") is not None:
            above = tech["close"] > tech["ema21"]
            votes.append(("Price vs 21 EMA", 1 if above else -1,
                          f"The close is {'above' if above else 'below'} the 21 EMA ({tech['ema21']})."))
        if tech.get("macd_hist") is not None:
            hist = tech["macd_hist"]
            votes.append(("MACD histogram", 1 if hist > 0 else (-1 if hist < 0 else 0),
                          f"The MACD histogram is {hist}."))
        positive = sum(f["effect"] == "positive" for k, f in factors.items() if k in NEWS_FACTORS)
        negative = sum(f["effect"] == "negative" for k, f in factors.items() if k in NEWS_FACTORS)
        if positive or negative:
            vote = 1 if positive > negative else (-1 if negative > positive else 0)
            votes.append(("News balance", vote, f"News factors: {positive} positive, {negative} negative."))
        total = sum(v for _, v, _ in votes)
        label = "Rise" if total >= 2 else ("Fall" if total <= -2 else "Sideways")
        justification = [text for _, _, text in votes]
        justification.append(f"The vote total is {total:+d}. The classification is {label}.")
        return {"label": label, "score": total, "votes": votes, "justification": justification}

    @staticmethod
    def _data_gaps(tech: dict, fund: dict, earn: dict, regime: dict, sector: dict, news: list) -> list[str]:
        gaps = []
        if tech.get("status") != "available":
            gaps.append("No price history.")
        else:
            for key, label in (("ema200", "200 EMA"), ("rsi14", "RSI"), ("avg_volume_50", "50-session volume"),
                               ("perf_6m", "6-month performance")):
                if tech.get(key) is None:
                    gaps.append(f"{label} not available (short history).")
        if fund.get("market_cap") is None:
            gaps.append("Market cap not available.")
        if fund.get("revenue_growth_pct") is None:
            gaps.append("Revenue growth not available.")
        if earn.get("last_date") is None:
            gaps.append("Earnings history not available.")
        if earn.get("next_date") is None:
            gaps.append("Next earnings date not available.")
        if regime.get("regime") == "unknown":
            gaps.append("Market regime (S&P 500, VIX) not available.")
        if not sector.get("etf"):
            gaps.append("No sector ETF mapping.")
        if not news:
            gaps.append("No news page could be read within the crawler rules.")
        return gaps
