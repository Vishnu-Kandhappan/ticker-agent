"""Ticker Strategy Analyzer (prompt: agents/ticker-strategy-analyzer.prompt,
skill: skills/ticker-strategy-analyzer/skill-description.md).

Uses only the research agent's output. Ratings, risk, scenarios, gates and
confidence follow the skill rules in code. The local model only writes the
"intuition" text, and that text must pass the ASD-STE100 and number checks.
"""

from __future__ import annotations

import json
import math
from typing import Any

from financial_alert.genai_telemetry import record_validation
from financial_alert.telemetry import get_tracer
from ticker_review import ste
from ticker_review.agents.research_agent import FACTORS
from ticker_review.formatting import money, money_compact, pct, qty
from ticker_review.llm import LocalLLM
from ticker_review.strategies import StrategyResult, evaluate

TRACER = get_tracer(__name__)
AGENT = "ticker_strategy_analyzer"

BLURBS = {
    "mean_reversion": "This strategy buys a healthy stock after a hard sell-off. It expects a return to the average price.",
    "pead": "This strategy follows the drift that often continues after a clear earnings beat.",
    "vcp": "This strategy buys a breakout after the price range becomes tighter and volume dries up.",
    "gap": "This strategy follows a gap up on real news for 1 to 3 days.",
    "momentum": "This strategy buys a stock that already rises faster than the market.",
    "runup": "This strategy holds the stock in the days before earnings. It sells before the report.",
    "high52": "This strategy buys a stock at a new 52-week high, where no holder has a loss.",
    "sector": "This strategy follows money into a strong sector and picks a strong stock in it.",
}


class TickerStrategyAnalyzer:
    def __init__(self, system_prompt: str, skill_text: str, llm: LocalLLM | None) -> None:
        self.system_prompt = f"{system_prompt}\n\n--- SKILL FILE ---\n{skill_text}"
        self.llm = llm

    def run(self, research: dict[str, Any]) -> dict[str, Any]:
        with TRACER.start_as_current_span("ticker_strategy_analyzer") as span:
            span.set_attribute("ticker.symbol", research["symbol"])
            market = research["market"]
            tech, fund, earn = market["technical"], market["fundamentals"], market["earnings"]
            regime, sector = research["regime"], research["sector"]
            currency = research["currency"]

            ratings = self._ratings(research)
            strategies = evaluate(tech, fund, earn, regime, sector, self._recent_trusted_news(research))
            best = strategies[0] if strategies else None
            runner_up = None
            if best and len(strategies) > 1 and best.confidence - strategies[1].confidence <= 15 \
                    and strategies[1].confidence > 0:
                runner_up = strategies[1]
            movement = self._movement(research, regime, best)
            coverage = sum(r["rating"] > 0 for r in ratings)
            confidence = self._confidence(movement["score"], coverage)
            intuition, source = self._intuition(research, best)
            warnings = []
            if tech.get("perf_5d") is not None and tech["perf_5d"] >= 15:
                warnings.append(f"The price rose {pct(tech['perf_5d'])} in 5 sessions. A new entry is late. Do not chase.")
            if fund.get("revenue_growth_pct") is not None and fund["revenue_growth_pct"] < 0:
                warnings.insert(0, f"Revenue growth is {pct(fund['revenue_growth_pct'])}. Fundamentals are deteriorating.")
            if research["symbol"].upper().endswith((".NS", ".BO")):
                warnings.append("This is a non-US listing. The regime filter uses the S&P 500 and the VIX, "
                                "and the sector filter uses US ETFs.")
            span.set_attribute("strategy.best", best.name if best else "none")
            span.set_attribute("strategy.confidence", best.confidence if best else 0)
            return {
                "ratings": ratings,
                "coverage": coverage,
                "risk": self._risk(tech, regime, sector, earn, currency),
                "scenarios": self._scenarios(tech, movement["label"], currency),
                "movement": movement,
                "confidence": confidence,
                "strategies": strategies,
                "best": best,
                "runner_up": runner_up,
                "intuition": intuition,
                "intuition_source": source,
                "warnings": warnings,
            }

    # -- 1. signal strength ----------------------------------------------
    @staticmethod
    def _ratings(research: dict) -> list[dict[str, Any]]:
        trust_by_id = {item["id"]: item["trust"] for item in research["news"]}
        out = []
        for key, factor in research["factors"].items():
            effect, measure = factor["effect"], factor.get("measure")
            rating, reason = 0, "No observed data."
            if effect != "no_data":
                if key == "volume_liquidity" and isinstance(measure, (int, float)):
                    change = abs(measure - 1)
                    rating = 1 if change < 0.2 else 2 if change < 0.5 else 3 if change < 1 else 4 if change < 2 else 5
                    reason = f"Relative volume is {measure}x."
                elif key == "sector_trends" and isinstance(measure, (int, float)):
                    gap = abs(measure)
                    rating = 1 if gap < 1 else 2 if gap < 3 else 3 if gap < 5 else 4 if gap < 8 else 5
                    reason = f"The sector ETF differs from SPY by {measure:+.2f} points in 1 month."
                elif key == "corporate_actions" and isinstance(measure, dict) and (
                        _near(measure.get("sessions_to_next"), 5) or _near(measure.get("sessions_since_last"), 5)):
                    rating, reason = 4, "Earnings are within 5 sessions."
                elif key == "corporate_actions" and isinstance(measure, dict) and (
                        _near(measure.get("sessions_to_next"), 10) or _near(measure.get("sessions_since_last"), 10)):
                    rating, reason = 3, "Earnings are within 10 sessions."
                else:
                    trusts = [trust_by_id.get(e, "unknown") for e in factor.get("evidence", [])]
                    trusted = trusts.count("trusted")
                    if trusted >= 2:
                        rating = 4
                    elif trusted == 1:
                        rating = 3
                    elif "semi-trusted" in trusts:
                        rating = 2
                    elif trusts:
                        rating = 1
                    elif key in ("corporate_actions",):
                        rating = 1
                    if rating and effect in ("positive", "negative"):
                        rating = min(5, rating + 1)
                    elif effect == "mentioned":
                        rating = min(rating, 2)  # topic found, direction not classified
                    reason = (f"{len(trusts)} source(s): {trusted} trusted." if trusts
                              else "Only dates are known. No news supports this factor.")
            out.append({"key": key, "label": FACTORS[key], "rating": rating, "effect": effect, "reason": reason,
                        "observation": factor["observation"], "evidence": factor.get("evidence", [])})
        return out

    @staticmethod
    def _recent_trusted_news(research: dict) -> bool:
        from datetime import datetime, timedelta, timezone

        limit = datetime.now(timezone.utc) - timedelta(days=2)
        for item in research["news"]:
            if item["scope"] != "ticker" or item["trust"] != "trusted" or not item["published"]:
                continue
            try:
                published = datetime.fromisoformat(item["published"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            if published >= limit:
                return True
        return False

    # -- 2. risk ----------------------------------------------------------
    @staticmethod
    def _risk(tech: dict, regime: dict, sector: dict, earn: dict, currency: str) -> dict[str, dict[str, str]]:
        name = regime.get("regime", "unknown")
        market_level = {"strong bull": "Low", "cautious bull": "Medium", "transition": "Medium",
                        "bear": "High", "panic": "High"}.get(name, "Unknown")
        market_reason = (f"The regime is {name}. The S&P 500 is {pct(regime.get('spx_vs_200_pct'))} from its 200-DMA. "
                         f"The VIX is {regime.get('vix', 'n/a')}.")
        gap = sector.get("etf_vs_spy_1m")
        if gap is None:
            sector_level, sector_reason = "Unknown", "No sector ETF data."
        else:
            sector_level = "Low" if gap >= 1 else ("High" if gap <= -3 else "Medium")
            sector_reason = f"{sector.get('etf')} differs from SPY by {gap:+.2f} points in 1 month."
        avg20, turnover = tech.get("avg_volume_20"), tech.get("avg_turnover_20")
        if avg20 is None:
            liq_level, liq_reason = "Unknown", "No volume data."
        else:
            liq_level = "Low" if avg20 >= 1e6 and (turnover or 0) >= 50e6 else ("Medium" if avg20 >= 3e5 else "High")
            liq_reason = f"The 20-session average volume is {qty(avg20)} shares. Turnover is {money_compact(turnover, currency)}."
        to_next, since = earn.get("sessions_to_next"), earn.get("sessions_since_last")
        if (to_next is not None and to_next <= 10) or (since is not None and since <= 2):
            event_level = "High"
        elif to_next is None or to_next <= 20:
            event_level = "Medium"
        else:
            event_level = "Low"
        event_reason = (f"The next earnings date is {earn['next_date']} ({to_next} sessions)."
                        if to_next is not None else "The next earnings date is not known.")
        return {
            "Market risk": {"level": market_level, "reason": market_reason},
            "Sector risk": {"level": sector_level, "reason": sector_reason},
            "Liquidity risk": {"level": liq_level, "reason": liq_reason},
            "Event risk": {"level": event_level, "reason": event_reason},
        }

    # -- 3. scenarios -----------------------------------------------------
    @staticmethod
    def _scenarios(tech: dict, direction: str, currency: str) -> dict[str, dict[str, Any]]:
        close, atr = tech.get("close"), tech.get("atr14")
        if close is None or atr is None:
            return {}
        rng = round(atr * math.sqrt(5), 2)
        up, down = round(close + rng, 2), round(close - rng, 2)
        skew = {"Rise": (0.25, 0.75), "Fall": (0.75, 0.25)}.get(direction, (0.5, 0.5))
        exp_low, exp_high = round(close - skew[0] * rng, 2), round(close + skew[1] * rng, 2)

        def levels(low: float, high: float, names: tuple[str, ...]) -> str:
            found = [f"{label} {money(tech[key], currency)}" for key, label in names
                     if tech.get(key) is not None and low <= tech[key] <= high]
            return ", ".join(found)

        resistance = levels(close, up, (("high_60d_prior", "60-session high"), ("high_52w", "52-week high")))
        support = levels(down, close, (("ema50", "50 EMA"), ("ema200", "200 EMA"), ("ema21", "21 EMA")))
        return {
            "range": {"value": rng, "text": f"ATR(14) is {money(atr, currency)}. The 5-session range is ±{money(rng, currency)}."},
            "Best case": {"low": close, "high": up,
                          "text": f"The price rises to about {money(up, currency)}."
                                  + (f" Resistance in this range: {resistance}." if resistance else "")},
            "Expected case": {"low": exp_low, "high": exp_high,
                              "text": f"The price stays between {money(exp_low, currency)} and {money(exp_high, currency)}. "
                                      f"This range uses the {direction} classification."},
            "Worst case": {"low": down, "high": close,
                           "text": f"The price falls to about {money(down, currency)}."
                                   + (f" Support in this range: {support}." if support else "")},
        }

    # -- 4/5. movement and confidence ------------------------------------
    @staticmethod
    def _movement(research: dict, regime: dict, best: StrategyResult | None) -> dict[str, Any]:
        votes = [(name, vote) for name, vote, _ in research["direction"]["votes"]]
        name = regime.get("regime")
        if name in ("strong bull", "cautious bull"):
            votes.append((f"Market regime ({name})", 1))
        elif name in ("bear", "panic"):
            votes.append((f"Market regime ({name})", -1))
        if best is not None and best.confidence >= 65:
            votes.append((f"Valid {best.name} setup ({best.confidence}/100)", 1))
        total = sum(v for _, v in votes)
        label = "Rise" if total >= 2 else ("Fall" if total <= -2 else "Sideways")
        parts = [f"{n} {v:+d}" for n, v in votes if v]
        justification = (f"The votes are: {'; '.join(parts) or 'no clear votes'}. "
                         f"The total is {total:+d}, so the decision is {label}.")
        return {"label": label, "score": total, "votes": votes, "justification": justification}

    @staticmethod
    def _confidence(score: int, coverage: int) -> dict[str, str]:
        if abs(score) >= 4 and coverage >= 7:
            level = "High"
        elif abs(score) >= 2 and coverage >= 5:
            level = "Medium"
        else:
            level = "Low"
        return {"level": level,
                "reason": f"The vote total is {score:+d} and {coverage} of 9 factors have observed data."}

    # -- intuition --------------------------------------------------------
    def _intuition(self, research: dict, best: StrategyResult | None) -> tuple[str, str]:
        fallback = self._rule_intuition(research, best)
        if best is None or self.llm is None:
            return fallback, "rules"
        tech, fund = research["market"]["technical"], research["market"]["fundamentals"]
        mandatory = [g for g in best.gates if g.mandatory]
        facts = {
            "ticker": research["symbol"], "name": research["name"],
            "close": tech.get("close"), "change_5_sessions_pct": tech.get("perf_5d"),
            "change_1_month_pct": tech.get("perf_1m"), "rsi14": tech.get("rsi14"),
            "best_strategy": best.name, "strategy_idea": BLURBS[best.key],
            "confidence": best.confidence, "band": best.band,
            "mandatory_gates_passed": sum(g.ok for g in mandatory), "mandatory_gates_total": len(mandatory),
            "failed_gates": [f"{g.name} ({g.value})" for g in best.mandatory_failed],
            "disqualifiers": best.disqualifiers,
            "market_regime": research["regime"].get("regime"),
            "revenue_growth_pct": fund.get("revenue_growth_pct"),
            "research_direction": research["direction"]["label"],
        }
        reply = self.llm.chat_json(AGENT, self.system_prompt, {"facts": facts})
        if reply is None:
            return fallback, "rules"  # unavailable or invalid JSON: already counted by the client
        text = str(reply.get("intuition", "")).strip()
        reason = self._intuition_problem(text, facts, best)
        record_validation(AGENT, "rejected" if reason else "accepted", reason, item="intuition")
        return (fallback, "rules") if reason else (text, "local model")

    @staticmethod
    def _intuition_problem(text: str, facts: dict, best: StrategyResult) -> str | None:
        """Return why the model text fails the checks, or None when it passes."""
        if not text:
            return "empty_output"
        if not 2 <= len(ste.sentences(text)) <= 4:
            return "sentence_count"
        if ste.issues(text, 25):
            return "ste_violation"
        if not ste.numbers_grounded(text, json.dumps(facts, default=str)):
            return "ungrounded_number"
        if best.confidence < 50 and "no setup" not in text.lower() and "no valid setup" not in text.lower():
            return "missing_no_setup"
        return None

    @staticmethod
    def _rule_intuition(research: dict, best: StrategyResult | None) -> str:
        fund = research["market"]["fundamentals"]
        sentences = []
        growth = fund.get("revenue_growth_pct")
        if growth is not None and growth < 0:
            sentences.append(f"Revenue growth is {pct(growth)}, so fundamentals are deteriorating.")
        if best is None:
            return "No price data is available. The program cannot test any strategy."
        mandatory = [g for g in best.gates if g.mandatory]
        passed = sum(g.ok for g in mandatory)
        if best.is_setup:
            sentences.append(f"{best.name} fits best, with {passed} of {len(mandatory)} mandatory gates.")
            sentences.append(BLURBS[best.key])
            sentences.append(f"The confidence is {best.confidence} of 100: {best.band.lower()}.")
        else:
            sentences.append("No strategy has a valid setup today.")
            failed = ", ".join(g.name for g in best.mandatory_failed[:3])
            sentences.append(f"The closest fit is {best.name}, with {passed} of {len(mandatory)} mandatory gates.")
            if failed:
                sentences.append(f"The failed gates include: {failed}.")
            elif best.caps:
                sentences.append(f"All gates pass, but a rule limits the score: {best.caps[-1].split(' →')[0].lower()}.")
            if best.disqualifiers:
                sentences.append(best.disqualifiers[0])
        return " ".join(sentences[:4])


def _near(value: Any, limit: int) -> bool:
    return value is not None and 0 <= value <= limit
