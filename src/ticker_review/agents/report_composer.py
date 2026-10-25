"""Report Composer Agent (prompt: agents/report-composer-agent.prompt).

Deterministic by design: it only copies values from the two agent outputs,
so it cannot add new data.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from financial_alert.telemetry import get_tracer
from ticker_review import ste
from ticker_review.formatting import money, pct, qty
from ticker_review.strategies import StrategyResult

TRACER = get_tracer(__name__)
DISCLAIMER = "*Analysis, not investment advice — verify prices before acting.*"
EFFECT_ICON = {"positive": "🟢 positive", "negative": "🔴 negative", "neutral": "⚪ neutral",
               "mentioned": "🟡 mentioned", "no_data": "— no data"}
MOVE_ICON = {"Rise": "🟢 Rise", "Fall": "🔴 Fall", "Sideways": "🟡 Sideways"}


def _cell(text: Any) -> str:
    return str(text if text is not None else "n/a").replace("|", "\\|").replace("\n", " ")


class ReportComposerAgent:
    def run(self, research: dict[str, Any], analysis: dict[str, Any], report_date: str | None = None) -> str:
        with TRACER.start_as_current_span("report_composer_agent") as span:
            span.set_attribute("ticker.symbol", research["symbol"])
            report_date = report_date or date.today().isoformat()
            prose: list[str] = []
            parts = [
                self._header(research, report_date),
                self._warnings(analysis, prose),
                self._overview(research, analysis, prose),
                self._movement(analysis, prose),
                self._strategy(research, analysis, prose),
                self._final(analysis),
                self._appendix(research, analysis, prose),
                DISCLAIMER,
            ]
            return "\n\n".join(part for part in parts if part).rstrip() + "\n"

    # -- header -----------------------------------------------------------
    @staticmethod
    def _header(research: dict, report_date: str) -> str:
        meta = research.get("metadata", {})
        role = meta.get("role", "ad hoc")
        tech = research["market"]["technical"]
        lines = [f"# 📈 {research['symbol']} — {research['name']}",
                 f"**Ticker review:** {report_date} | **Portfolio role:** {role} | "
                 f"**Last session:** {tech.get('as_of', 'n/a')}"]
        if meta.get("manual_updates"):
            lines.append(f"> **Your note (not verified):** {meta['manual_updates']}")
        return "\n".join(lines)

    @staticmethod
    def _warnings(analysis: dict, prose: list[str]) -> str:
        if not analysis["warnings"]:
            return ""
        prose.extend(analysis["warnings"])
        return "\n".join(f"> ⚠️ **WARNING:** {w}" for w in analysis["warnings"])

    # -- 1 ----------------------------------------------------------------
    @staticmethod
    def _overview(research: dict, analysis: dict, prose: list[str]) -> str:
        ps, cur = research["price_summary"], research["currency"]
        out = ["## 1. Ticker Overview", "### Today's price summary"]
        if ps.get("status") != "available":
            out.append("No price data is available. " + " ".join(ps.get("notes", [])))
            return "\n\n".join(out)
        out.append("| Open | High | Low | Close | Change | Volume |\n|---|---|---|---|---|---|\n"
                   f"| {money(ps['open'], cur)} | {money(ps['high'], cur)} | {money(ps['low'], cur)} | "
                   f"**{money(ps['close'], cur)}** | {pct(ps['change_pct'])} | {qty(ps['volume'])} |")
        if ps["notes"]:
            out.append("\n".join(f"- {note}" for note in ps["notes"]))
            prose.extend(ps["notes"])
        out.append("### This week's expected trend")
        expected = analysis["scenarios"].get("Expected case")
        trend = [f"- **Research agent:** {MOVE_ICON[research['direction']['label']]} "
                 f"(vote total {research['direction']['score']:+d}).",
                 f"- **Strategy analyzer:** {MOVE_ICON[analysis['movement']['label']]} "
                 f"(vote total {analysis['movement']['score']:+d}, confidence {analysis['confidence']['level']})."]
        if expected:
            trend.append(f"- **Expected range (5 sessions):** {money(expected['low'], cur)} – {money(expected['high'], cur)}.")
        out.append("\n".join(trend))
        return "\n\n".join(out)

    # -- 2 ----------------------------------------------------------------
    @staticmethod
    def _movement(analysis: dict, prose: list[str]) -> str:
        move = analysis["movement"]
        prose.append(move["justification"])
        return f"## 2. Movement Classification\n\n**{MOVE_ICON[move['label']]}** — {move['justification']}"

    # -- 3 ----------------------------------------------------------------
    def _strategy(self, research: dict, analysis: dict, prose: list[str]) -> str:
        cur = research["currency"]
        out = ["## 3. Strategic Analysis Summary", "### Key signals (rated 0–5)"]
        rows = ["| Factor | Rating | Effect | Observation | Sources |", "|---|---|---|---|---|"]
        for r in analysis["ratings"]:
            rows.append(f"| {r['label']} | {'●' * r['rating']}{'○' * (5 - r['rating'])} {r['rating']} | "
                        f"{EFFECT_ICON.get(r['effect'], r['effect'])} | {_cell(r['observation'])} {_cell(r['reason'])} | "
                        f"{', '.join(r['evidence']) or '—'} |")
            prose.extend([r["observation"], r["reason"]])
        out.append("\n".join(rows))

        out.append("### Risk profile")
        rows = ["| Risk | Level | Reason |", "|---|---|---|"]
        for name, item in analysis["risk"].items():
            rows.append(f"| {name} | **{item['level']}** | {_cell(item['reason'])} |")
            prose.append(item["reason"])
        out.append("\n".join(rows))

        out.append("### Scenario map (next 5 sessions / 7 days)")
        scenarios = analysis["scenarios"]
        if scenarios:
            rows = ["| Case | Range | Description |", "|---|---|---|"]
            for case in ("Best case", "Expected case", "Worst case"):
                item = scenarios[case]
                rows.append(f"| {case} | {money(item['low'], cur)} – {money(item['high'], cur)} | {_cell(item['text'])} |")
                prose.append(item["text"])
            out.append("\n".join(rows))
            out.append(f"_{scenarios['range']['text']} This is a volatility range, not a forecast._")
        else:
            out.append("No scenario map: ATR or close is not available.")

        out.append(self._verdict(research, analysis, prose))
        return "\n\n".join(out)

    def _verdict(self, research: dict, analysis: dict, prose: list[str]) -> str:
        best: StrategyResult | None = analysis["best"]
        tech, cur = research["market"]["technical"], research["currency"]
        if best is None:
            return "### Strategy verdict\n\nNo price data. The program cannot test the 8 strategies."
        macd_state = "n/a" if tech.get("macd_hist") is None else ("bullish" if tech["macd_hist"] > 0 else "bearish")
        out = ["### Strategy verdict (ticker-strategy-analyzer skill)",
               f"**Price:** {money(tech.get('close'), cur)} | **RSI:** {tech.get('rsi14', 'n/a')} | "
               f"**MACD:** {macd_state} | **Volume:** {qty(tech.get('volume'))} vs {qty(tech.get('avg_volume_50'))} avg"]
        title = best.name if best.is_setup else f"NO SETUP (closest: {best.name})"
        out.append(f"#### Verdict: {title} — Confidence {best.confidence}/100\n_{best.band}_")
        out.append(f"**Intuition:** {analysis['intuition']}")
        prose.append(analysis["intuition"])

        if best.is_setup:
            rr = best.risk_reward
            rows = ["| Parameter | Value |", "|---|---|",
                    f"| Entry | {money(best.entry, cur)} — {best.entry_note} |",
                    f"| Target 1 | {self._target(best.target1, best.entry, cur)} — {best.target1_source} |",
                    f"| Target 2 | {self._target(best.target2, best.entry, cur)} — {best.target2_source} |",
                    f"| Stop loss | {self._target(best.stop, best.entry, cur)} — {best.stop_source} |",
                    f"| Hold period | {best.hold} |",
                    f"| Risk/reward | {'1:' + format(rr, '.1f') if rr is not None else 'n/a'} |"]
            out.append("#### Trade parameters\n\n" + "\n".join(rows))
            if rr is not None and rr < 1.5:
                out.append(f"> ⚠️ **WARNING:** Risk/reward to Target 1 is 1:{rr:.1f}, below 1:1.5. The reward does not pay for the risk.")
            sizing = best.sizing()
            if sizing:
                text = (f"Position size = (10,000 × 0.015) ÷ ({money(best.entry, cur)} − {money(best.stop, cur)}) "
                        f"= {sizing['risk_cash']:.0f} ÷ {sizing['per_share']:.2f} = **{sizing['shares']} shares** "
                        f"({money(sizing['capital'], cur)} capital).")
                if sizing["capital_capped_shares"] is not None:
                    text += (f" This is more than the 10,000 account, so capital limits the size to "
                             f"{sizing['capital_capped_shares']} shares.")
                out.append("**Position size (10,000 account, 1.5% risk):** " + text)
        else:
            out.append("**Trade parameters:** none. The confidence is below 50, so there is no trade.")

        rows = ["| Gate | Type | Value | Pass |", "|---|---|---|---|"]
        for g in best.gates:
            rows.append(f"| {_cell(g.name)} | {'mandatory' if g.mandatory else 'optional'} | {_cell(g.value)} | {g.mark} |")
        out.append("#### Gate check\n\n" + "\n".join(rows))

        lines = [f"- Base: **{best.base}** ({len(best.mandatory_failed)} mandatory gate(s) failed)"]
        lines += [f"- {label}: **{delta:+d}**" for label, delta in best.adjustments]
        lines += [f"- Disqualifier: {d}" for d in best.disqualifiers]
        lines += [f"- Cap: {c}" for c in best.caps]
        lines.append(f"- **Final: {best.confidence}/100**")
        lines += [f"- Note: {n}" for n in best.notes]
        out.append("#### Score breakdown\n\n" + "\n".join(lines))

        out.append("#### What would change this\n\n" + "\n".join(f"- {s}" for s in self._changes(best, tech, cur)))
        if analysis["runner_up"] is not None:
            ru = analysis["runner_up"]
            failed = ", ".join(g.name for g in ru.mandatory_failed[:2]) or "fewer optional confirmations"
            out.append(f"**Runner-up:** {ru.name} at {ru.confidence}/100 — fails: {failed}.")

        rows = ["| Strategy | Confidence | Mandatory passed | Main failed gate |", "|---|---|---|---|"]
        for s in analysis["strategies"]:
            mandatory = [g for g in s.gates if g.mandatory]
            failed = s.mandatory_failed[0].name if s.mandatory_failed else (s.disqualifiers[0] if s.disqualifiers else "—")
            rows.append(f"| {s.name} | {s.confidence} | {sum(g.ok for g in mandatory)}/{len(mandatory)} | {_cell(failed)} |")
        out.append("#### All 8 strategies\n\n" + "\n".join(rows))
        return "\n\n".join(out)

    @staticmethod
    def _target(value: float | None, entry: float | None, cur: str) -> str:
        if value is None:
            return "n/a"
        if entry:
            return f"{money(value, cur)} ({pct((value / entry - 1) * 100)})"
        return money(value, cur)

    @staticmethod
    def _changes(best: StrategyResult, tech: dict, cur: str) -> list[str]:
        out = []
        if best.is_setup and best.stop is not None:
            out.append(f"A close below the stop at {money(best.stop, cur)} invalidates the setup.")
        elif tech.get("ema200") is not None:
            side = "above" if (tech.get("close") or 0) > tech["ema200"] else "below"
            out.append(f"The price is {side} the 200 EMA ({money(tech['ema200'], cur)}). "
                       "A cross of this level changes the long-term read.")
        failed = best.mandatory_failed
        if failed:
            out.append("The confidence rises when these gates pass: " + ", ".join(g.name for g in failed[:3]) + ".")
        else:
            missing = [g.name for g in best.gates if not g.mandatory and not g.ok][:2]
            if missing:
                out.append("Each of these optional gates adds 5 points: " + ", ".join(missing) + ".")
        return out

    # -- 4 ----------------------------------------------------------------
    @staticmethod
    def _final(analysis: dict) -> str:
        best = analysis["best"]
        trade = (f"{best.name} — {best.confidence}/100 ({best.band})" if best and best.is_setup
                 else f"No setup (best fit {best.name} at {best.confidence}/100)" if best else "Not tested")
        return ("## 4. Final Assessment\n\n"
                "| Item | Result |\n|---|---|\n"
                f"| Expected movement (next 7 days) | **{MOVE_ICON[analysis['movement']['label']]}** |\n"
                f"| Movement confidence | **{analysis['confidence']['level']}** — {analysis['confidence']['reason']} |\n"
                f"| Trade setup | {trade} |")

    # -- 5 ----------------------------------------------------------------
    @staticmethod
    def _appendix(research: dict, analysis: dict, prose: list[str]) -> str:
        out = ["## 5. Source Appendix"]
        rows = ["| # | Crawl | Status | Trust | Timestamp (UTC) | HTTP | Title | URL |", "|---|---|---|---|---|---|---|---|"]
        ids = {(item["scope"], item["url"]): item["id"] for item in research["news"]}
        for entry in research["source_log"]:
            url = entry["url"]
            link = url if not url.startswith("http") else f"<{url}>"
            rows.append(f"| {ids.get((entry.get('scope'), url), '—')} | {entry.get('scope', '')} | {entry['status']} | {entry['trust']} | {entry['timestamp_utc']} | "
                        f"{entry.get('http_status') or '—'} | {_cell(entry.get('title') or entry.get('note'))} | {link} |")
        out.append("### Crawled URLs\n\n" + ("\n".join(rows) if research["source_log"] else "No URLs were crawled."))
        blocked = [f'{e["url"]} ({e["status"]})' for e in research["source_log"] if e["status"] in ("blocked", "robots_disallowed", "search_failed")]
        out.append("### Blocked pages\n\n" + ("\n".join(f"- {u}" for u in dict.fromkeys(blocked)) if blocked else "None."))

        market = research["market"]
        notes = [f"- Market data: {market['source']}; retrieved {market['retrieved_at_utc']}.",
                 f"- Search queries: {'; '.join(q for q in research['queries'] if q)}.",
                 f"- News classifier: {research['classifier']}. Intuition text: {analysis['intuition_source']}."]
        notes += [f"- Data gap: {gap}" for gap in research["data_gaps"]]
        out.append("### Data notes\n\n" + "\n".join(notes))

        problems = [issue for text in prose for issue in ste.issues(text)]
        out.append("### ASD-STE100 check\n\n" + ("All prose sentences are 25 words or fewer." if not problems
                                                  else "\n".join(f"- {p}" for p in problems)))
        return "\n\n".join(out)
