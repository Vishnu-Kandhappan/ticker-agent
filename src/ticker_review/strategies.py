"""The 8 strategies, gate checks and confidence scoring.

Rules come from skills/ticker-strategy-analyzer/skill-description.md.
A gate with missing data is a fail (passed is None, shown as "no data").
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

ACCOUNT_SIZE = 10_000
RISK_FRACTION = 0.015
MAX_CONFIDENCE = 85
LONG_ONLY = {"vcp", "momentum", "high52", "gap", "sector"}


@dataclass
class Gate:
    name: str
    value: str
    passed: bool | None
    mandatory: bool

    @property
    def ok(self) -> bool:
        return self.passed is True

    @property
    def mark(self) -> str:
        return "✅" if self.passed else ("❔ no data" if self.passed is None else "❌")


@dataclass
class StrategyResult:
    key: str
    name: str
    hold: str
    gates: list[Gate] = field(default_factory=list)
    disqualifiers: list[str] = field(default_factory=list)
    adjustments: list[tuple[str, int]] = field(default_factory=list)
    caps: list[str] = field(default_factory=list)
    base: int = 0
    confidence: int = 0
    entry: float | None = None
    entry_note: str = ""
    target1: float | None = None
    target1_source: str = ""
    target2: float | None = None
    target2_source: str = ""
    stop: float | None = None
    stop_source: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def mandatory_failed(self) -> list[Gate]:
        return [g for g in self.gates if g.mandatory and not g.ok]

    @property
    def optional_passed(self) -> list[Gate]:
        return [g for g in self.gates if not g.mandatory and g.ok]

    @property
    def band(self) -> str:
        if self.confidence >= 80:
            return "High conviction — full position"
        if self.confidence >= 65:
            return "Valid setup — normal position"
        if self.confidence >= 50:
            return "Marginal — half position or wait a day"
        return "No setup — do not trade"

    @property
    def is_setup(self) -> bool:
        return self.confidence >= 50

    @property
    def risk_reward(self) -> float | None:
        if None in (self.entry, self.stop, self.target1) or self.entry <= self.stop:  # type: ignore[operator]
            return None
        return round((self.target1 - self.entry) / (self.entry - self.stop), 2)  # type: ignore[operator]

    def sizing(self) -> dict[str, Any] | None:
        if self.entry is None or self.stop is None or self.entry <= self.stop:
            return None
        risk_cash = ACCOUNT_SIZE * RISK_FRACTION
        per_share = self.entry - self.stop
        shares = math.floor(risk_cash / per_share)
        capital = shares * self.entry
        capped = math.floor(ACCOUNT_SIZE / self.entry) if capital > ACCOUNT_SIZE else None
        return {"risk_cash": risk_cash, "per_share": round(per_share, 2), "shares": shares,
                "capital": round(capital, 2), "capital_capped_shares": capped}


# -- helpers --------------------------------------------------------------

def _fmt(value: Any, unit: str = "", digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        if abs(value) >= 1e9:
            return f"{value / 1e9:,.1f}B{unit}"
        if abs(value) >= 1e6:
            return f"{value / 1e6:,.2f}M{unit}"
        return f"{value:,.{digits}f}{unit}"
    return str(value)


def _check(*conditions: bool | None) -> bool | None:
    """False if any condition fails; None if none fail but some are unknown."""
    if any(c is False for c in conditions):
        return False
    if any(c is None for c in conditions):
        return None
    return True


def _gt(value: Any, threshold: float) -> bool | None:
    return None if value is None else value > threshold


def _lt(value: Any, threshold: float) -> bool | None:
    return None if value is None else value < threshold


def _between(value: Any, low: float, high: float) -> bool | None:
    return None if value is None else low <= value <= high


def _above(a: Any, b: Any) -> bool | None:
    return None if a is None or b is None else a > b


def _pct(price: float | None, pct: float) -> float | None:
    return None if price is None else round(price * (1 + pct / 100), 2)


class Context:
    """All inputs for one ticker, with short accessors."""

    def __init__(self, technical: dict, fundamentals: dict, earnings: dict, regime: dict,
                 sector: dict, news_recent_trusted: bool) -> None:
        self.t, self.f, self.e, self.r, self.s = technical, fundamentals, earnings, regime, sector
        self.news_recent = news_recent_trusted

    def __getattr__(self, name: str) -> Any:  # technical values as attributes
        return self.t.get(name)

    @property
    def liquid_cap(self) -> Callable[[float, float], bool | None]:
        def check(min_price: float, min_volume: float) -> bool | None:
            return _check(_gt(self.f.get("market_cap"), 2e9), _gt(self.close, min_price),
                          _gt(self.avg_volume_50, min_volume) if min_volume else True)
        return check

    def cap_text(self, min_price: float, min_volume: float = 0) -> str:
        text = f"cap {_fmt(self.f.get('market_cap'))}, price {_fmt(self.close)}"
        return text + (f", avg vol {_fmt(self.avg_volume_50)}" if min_volume else "")


def _g(name: str, value: str, passed: bool | None, mandatory: bool = True) -> Gate:
    return Gate(name, value, passed, mandatory)


# -- strategies -----------------------------------------------------------

def mean_reversion(c: Context) -> StrategyResult:
    r = StrategyResult("mean_reversion", "Mean Reversion", "3–15 days")
    r.gates = [
        _g("RSI(14) < 40", _fmt(c.rsi14, digits=1), _lt(c.rsi14, 40)),
        _g("Price > 200 EMA", f"{_fmt(c.close)} vs {_fmt(c.ema200)}", _above(c.close, c.ema200)),
        _g("Price < 50 EMA", f"{_fmt(c.close)} vs {_fmt(c.ema50)}", _above(c.ema50, c.close)),
        _g("1-month perf < −5%", _fmt(c.perf_1m, "%"), _lt(c.perf_1m, -5)),
        _g("P/E > 1", _fmt(c.f.get("trailing_pe")), _gt(c.f.get("trailing_pe"), 1)),
        _g("Cap > $2B, price > $25, avg vol > 300K", c.cap_text(25, 300_000), c.liquid_cap(25, 300_000)),
        _g("RSI < 32", _fmt(c.rsi14, digits=1), _lt(c.rsi14, 32), False),
        _g("Price < lower Bollinger Band", f"{_fmt(c.close)} vs {_fmt(c.bb_lower)}", _above(c.bb_lower, c.close), False),
        _g("ADX(14) < 25", _fmt(c.adx14, digits=1), _lt(c.adx14, 25), False),
        _g("Bullish rejection candle", _fmt(c.bullish_rejection_candle), c.bullish_rejection_candle, False),
        _g("Volume spike on a down day", f"rel vol {_fmt(c.rel_volume, 'x')}",
           _check(c.down_day, _gt(c.rel_volume, 1.5)), False),
        _g("Revenue growth > 5%", _fmt(c.f.get("revenue_growth_pct"), "%"), _gt(c.f.get("revenue_growth_pct"), 5), False),
    ]
    since, surprise = c.e.get("sessions_since_last"), c.e.get("last_surprise_pct")
    if since is not None and since <= 21 and surprise is not None and surprise < 0:
        r.disqualifiers.append(f"Earnings miss ({surprise}%) {since} sessions ago — a repricing, not a dip.")
    r.entry, r.entry_note = c.close, "at the close, after a bullish candle confirms"
    r.target1, r.target1_source = c.sma20, "middle Bollinger Band (20 SMA)"
    r.target2, r.target2_source = c.ema50, "50 EMA"
    if c.close is not None and c.atr14 is not None:
        r.stop, r.stop_source = round(c.close - 1.5 * c.atr14, 2), "entry − 1.5 × ATR(14)"
    return r


def pead(c: Context) -> StrategyResult:
    r = StrategyResult("pead", "PEAD — Post-Earnings Drift", "5–60 days")
    since, surprise = c.e.get("sessions_since_last"), c.e.get("last_surprise_pct")
    r.gates = [
        _g("Earnings in last 3 trading days", f"{_fmt(since, digits=0)} sessions since {c.e.get('last_date') or 'n/a'}",
           None if since is None else since <= 2),
        _g("EPS beat > 5%", _fmt(surprise, "%"), _gt(surprise, 5)),
        _g("Stock rose on earnings day", _fmt(c.e.get("reaction_change_pct"), "%"), _gt(c.e.get("reaction_change_pct"), 0)),
        _g("Earnings-day volume ≥ 2× 10-day avg", _fmt(c.e.get("reaction_rel_volume"), "x"),
           None if c.e.get("reaction_rel_volume") is None else c.e["reaction_rel_volume"] >= 2),
        _g("Price > 50 EMA", f"{_fmt(c.close)} vs {_fmt(c.ema50)}", _above(c.close, c.ema50)),
        _g("EPS beat > 10%", _fmt(surprise, "%"), _gt(surprise, 10), False),
        _g("EPS beat > 20%", _fmt(surprise, "%"), _gt(surprise, 20), False),
        _g("52-week high in last 3 months", _fmt(c.high_52w_within_3m), c.high_52w_within_3m, False),
        _g("Revenue growth > 0", _fmt(c.f.get("revenue_growth_pct"), "%"), _gt(c.f.get("revenue_growth_pct"), 0), False),
    ]
    r.notes.append("Revenue surprise, guidance change and analyst upgrades are not checked (no free data).")
    r.entry, r.entry_note = c.close, "day 1–3 after the report; if the gap was > 10%, wait for a pullback to the 21 EMA"
    r.target1, r.target1_source = _pct(c.close, 8), "+8% from entry"
    r.target2, r.target2_source = _pct(c.close, 15), "+15% from entry"
    r.stop, r.stop_source = _pct(c.close, -4), "−4% from entry"
    return r


def vcp(c: Context) -> StrategyResult:
    r = StrategyResult("vcp", "VCP Breakout", "10–30 days")
    cons = c.contractions or []
    shrinking = None if len(cons) < 3 else (cons[0] > cons[1] > cons[2])
    two_plus = None if len(cons) < 3 else (cons[1] > cons[2] and cons[2] > 0)
    breakout = _check(None if c.rel_volume is None else c.rel_volume >= 1.4, _above(c.close, c.high_20d_prior))
    r.gates = [
        _g("S&P 500 above its 50-DMA", _fmt(c.r.get("spx_above_50")), c.r.get("spx_above_50")),
        _g("Stage 2: price > 50 > 150 > 200 EMA",
           f"{_fmt(c.close)} / {_fmt(c.ema50)} / {_fmt(c.ema150)} / {_fmt(c.ema200)}",
           _check(_above(c.close, c.ema50), _above(c.ema50, c.ema150), _above(c.ema150, c.ema200))),
        _g("200 EMA rising for 1 month", f"{_fmt(c.ema200_month_ago)} → {_fmt(c.ema200)}", _above(c.ema200, c.ema200_month_ago)),
        _g("2+ contractions, each smaller", " → ".join(f"{x}%" for x in cons) or "n/a", two_plus),
        _g("Volume dry-up in last contraction", _fmt(c.volume_dry_up), c.volume_dry_up),
        _g("Breakout: vol ≥ 1.4× 50d avg and close > 20d high",
           f"rel vol {_fmt(c.rel_volume, 'x')}, close {_fmt(c.close)} vs {_fmt(c.high_20d_prior)}", breakout),
        _g("Within 25% of 52-week high", _fmt(c.pct_from_52w_high, "%"),
           None if c.pct_from_52w_high is None else c.pct_from_52w_high >= -25),
        _g("≥ 30% above 52-week low", _fmt(c.pct_above_52w_low, "%"), None if c.pct_above_52w_low is None else c.pct_above_52w_low >= 30),
        _g("ATR(14) ≤ 1/3 of its 50-session avg", f"{_fmt(c.atr14)} vs {_fmt(c.atr14_avg50)}",
           None if None in (c.atr14, c.atr14_avg50) else c.atr14 <= c.atr14_avg50 / 3, False),
        _g("3 contractions, each smaller", " → ".join(f"{x}%" for x in cons) or "n/a", shrinking, False),
        _g("Beats S&P 500 over 3 months", f"{_fmt(c.perf_3m, '%')} vs {_fmt(c.r.get('spx_perf_3m'), '%')}",
           _above(c.perf_3m, c.r.get("spx_perf_3m")), False),
    ]
    r.notes.append("Contractions are approximated from three 20-session windows of daily bars.")
    r.entry, r.entry_note = c.close, f"only on a breakout close above {_fmt(c.high_20d_prior)} on volume — never before"
    if c.close is not None and c.last_contraction_low is not None:
        r.stop = round(max(c.last_contraction_low, c.close * 0.92), 2)
        r.stop_source = "last contraction low (max −8%)"
        risk = c.close - r.stop
        r.target1, r.target1_source = round(c.close + 2 * risk, 2), "2R (2 × stop distance)"
        r.target2, r.target2_source = round(c.close + 3 * risk, 2), "3R"
    return r


def gap_and_go(c: Context) -> StrategyResult:
    r = StrategyResult("gap", "Gap and Go", "1–3 days")
    since = c.e.get("sessions_since_last")
    catalyst = True if (since is not None and since <= 1) or c.news_recent else (None if since is None and not c.news_recent else False)
    r.gates = [
        _g("Opening gap > 3%", _fmt(c.gap_pct, "%"), _gt(c.gap_pct, 3)),
        _g("Volume ≥ 2× average", _fmt(c.rel_volume, "x"), None if c.rel_volume is None else c.rel_volume >= 2),
        _g("Real catalyst (earnings or dated news, last 2 days)",
           "earnings" if since is not None and since <= 1 else ("news" if c.news_recent else "none found"), catalyst),
        _g("Price > 200 EMA", f"{_fmt(c.close)} vs {_fmt(c.ema200)}", _above(c.close, c.ema200)),
        _g("RSI(14) 40–70", _fmt(c.rsi14, digits=1), _between(c.rsi14, 40, 70)),
        _g("Cap > $2B, price > $15", c.cap_text(15), c.liquid_cap(15, 0)),
        _g("Gap > 5% with volume ≥ 3×", f"{_fmt(c.gap_pct, '%')}, {_fmt(c.rel_volume, 'x')}",
           _check(_gt(c.gap_pct, 5), None if c.rel_volume is None else c.rel_volume >= 3), False),
        _g("Closed above the gap open", f"{_fmt(c.close)} vs {_fmt(c.open)}", _above(c.close, c.open), False),
        _g("No 60-session high within 5% above", f"{_fmt(c.high_60d_prior)}",
           None if None in (c.high_60d_prior, c.close) else not (c.close < c.high_60d_prior <= c.close * 1.05), False),
    ]
    if c.gap_pct is not None and c.gap_pct > 15:
        r.disqualifiers.append(f"Gap of {c.gap_pct}% is above 15% — too extended.")
    surprise = c.e.get("last_surprise_pct")
    if since is not None and since <= 1 and surprise is not None and surprise < 0:
        r.disqualifiers.append("Gap on an earnings miss.")
    r.entry, r.entry_note = c.close, "only if price holds above the gap open"
    r.target1, r.target1_source = _pct(c.close, 4.5), "+4.5% (midpoint of +3–6%)"
    r.target2, r.target2_source = c.high_60d_prior, "prior resistance (60-session high)"
    r.stop, r.stop_source = c.open, "below the gap open price"
    return r


def momentum(c: Context) -> StrategyResult:
    r = StrategyResult("momentum", "Momentum / Trend Following", "20–60 days")
    near21 = None if None in (c.close, c.ema21) else abs(c.close / c.ema21 - 1) <= 0.02
    r.gates = [
        _g("3-month perf > 15%", _fmt(c.perf_3m, "%"), _gt(c.perf_3m, 15)),
        _g("1-month perf > 5%", _fmt(c.perf_1m, "%"), _gt(c.perf_1m, 5)),
        _g("Price > 50 EMA and > 200 EMA", f"{_fmt(c.close)} vs {_fmt(c.ema50)} / {_fmt(c.ema200)}",
           _check(_above(c.close, c.ema50), _above(c.close, c.ema200))),
        _g("RSI(14) 50–70", _fmt(c.rsi14, digits=1), _between(c.rsi14, 50, 70)),
        _g("Avg volume > 500K", _fmt(c.avg_volume_50), _gt(c.avg_volume_50, 500_000)),
        _g("Market cap > $2B", _fmt(c.f.get("market_cap")), _gt(c.f.get("market_cap"), 2e9)),
        _g("Beats S&P 500 over 3 and 6 months",
           f"{_fmt(c.perf_3m, '%')}/{_fmt(c.perf_6m, '%')} vs {_fmt(c.r.get('spx_perf_3m'), '%')}/{_fmt(c.r.get('spx_perf_6m'), '%')}",
           _check(_above(c.perf_3m, c.r.get("spx_perf_3m")), _above(c.perf_6m, c.r.get("spx_perf_6m"))), False),
        _g("ADX(14) > 20", _fmt(c.adx14, digits=1), _gt(c.adx14, 20), False),
        _g("Pullback to 21 EMA (within 2%)", f"{_fmt(c.close)} vs {_fmt(c.ema21)}", near21, False),
        _g("Last EPS surprise > 0", _fmt(c.e.get("last_surprise_pct"), "%"), _gt(c.e.get("last_surprise_pct"), 0), False),
        _g("Sector ETF beats SPY (1 month)", _fmt(c.s.get("etf_vs_spy_1m"), " pt"), _gt(c.s.get("etf_vs_spy_1m"), 0), False),
    ]
    if c.rsi14 is not None and c.rsi14 > 78:
        r.disqualifiers.append(f"RSI {c.rsi14} is above 78 — too extended.")
    r.entry, r.entry_note = c.close, f"best on a pullback to the 21 EMA ({_fmt(c.ema21)})"
    r.target1, r.target1_source = c.high_60d_prior, "prior swing high (60-session high)"
    r.target2, r.target2_source = _analyst(c)
    r.stop, r.stop_source = c.ema21, "close below the 21 EMA"
    return r


def earnings_runup(c: Context) -> StrategyResult:
    r = StrategyResult("runup", "Earnings Run-Up", "3–10 days")
    to_next, beats, n = c.e.get("sessions_to_next"), c.e.get("beats_last4"), c.e.get("reported_last4") or 0
    r.gates = [
        _g("Earnings 5–10 trading days away", f"{_fmt(to_next, digits=0)} sessions ({c.e.get('next_date') or 'n/a'})",
           _between(to_next, 5, 10)),
        _g("EPS beat in ≥ 3 of last 4 quarters", f"{_fmt(beats, digits=0)} of {n}", None if beats is None else beats >= 3),
        _g("Price > 200 EMA", f"{_fmt(c.close)} vs {_fmt(c.ema200)}", _above(c.close, c.ema200)),
        _g("RSI(14) 45–65", _fmt(c.rsi14, digits=1), _between(c.rsi14, 45, 65)),
        _g("Avg vol > 300K, cap > $2B", c.cap_text(0, 300_000), c.liquid_cap(0, 300_000)),
        _g("Beat 4 of 4", f"{_fmt(beats, digits=0)} of {n}", None if beats is None else (beats == 4 and n == 4), False),
        _g("Average beat > 5%", _fmt(c.e.get("avg_surprise_last4"), "%"), _gt(c.e.get("avg_surprise_last4"), 5), False),
        _g("Analyst rating buy / strong buy", _fmt(c.f.get("recommendation")),
           None if not c.f.get("recommendation") else c.f["recommendation"] in {"buy", "strong_buy"}, False),
    ]
    r.notes.append("Rule: sell the day before earnings, always.")
    r.entry, r.entry_note = c.close, "now; exit the session before the report"
    r.target1, r.target1_source = _pct(c.close, 6.5), "+6.5% (midpoint of +5–8%)"
    r.target2, r.target2_source = None, f"exit the day before earnings ({c.e.get('next_date') or 'date n/a'}), at any price"
    r.stop, r.stop_source = _pct(c.close, -3), "−3% from entry"
    return r


def high52(c: Context) -> StrategyResult:
    r = StrategyResult("high52", "52-Week High Breakout", "20–90 days")
    to_next = c.e.get("sessions_to_next")
    r.gates = [
        _g("Within 2% of 52-week high", _fmt(c.pct_from_52w_high, "%"),
           None if c.pct_from_52w_high is None else c.pct_from_52w_high >= -2),
        _g("Relative volume > 1.5×", _fmt(c.rel_volume, "x"), _gt(c.rel_volume, 1.5)),
        _g("Price > 200 EMA", f"{_fmt(c.close)} vs {_fmt(c.ema200)}", _above(c.close, c.ema200)),
        _g("RSI(14) 55–75", _fmt(c.rsi14, digits=1), _between(c.rsi14, 55, 75)),
        _g("1-month perf > 8%", _fmt(c.perf_1m, "%"), _gt(c.perf_1m, 8)),
        _g("Cap > $2B, price > $25", c.cap_text(25), c.liquid_cap(25, 0)),
        _g("Prior 52-week high held 6+ months", f"{_fmt(c.prior_52w_high_age_sessions, digits=0)} sessions",
           None if c.prior_52w_high_age_sessions is None else c.prior_52w_high_age_sessions >= 126, False),
        _g("ADX(14) > 20", _fmt(c.adx14, digits=1), _gt(c.adx14, 20), False),
        _g("Earnings growth > 0", _fmt(c.f.get("earnings_growth_pct"), "%"), _gt(c.f.get("earnings_growth_pct"), 0), False),
        _g("Earnings > 5 days away", _fmt(to_next, digits=0), None if to_next is None else to_next > 5, False),
    ]
    if to_next is not None and to_next <= 5:
        r.disqualifiers.append("Earnings within 5 days — the report will override the chart.")
    r.entry, r.entry_note = c.close, f"on a close above the 52-week high ({_fmt(c.high_52w)}) on volume"
    r.target1, r.target1_source = _pct(c.close, 10), "+10% from breakout"
    r.target2, r.target2_source = _analyst(c)
    r.stop, r.stop_source = _pct(c.close, -4), "−4% from entry"
    return r


def sector_rotation(c: Context) -> StrategyResult:
    r = StrategyResult("sector", "Sector Rotation", "30–90 days")
    etf = c.s.get("etf") or "sector ETF"
    r.gates = [
        _g(f"{etf} beats SPY over 1 month", _fmt(c.s.get("etf_vs_spy_1m"), " pt"), _gt(c.s.get("etf_vs_spy_1m"), 0)),
        _g(f"Stock beats {etf} over 1 month", _fmt(c.s.get("stock_vs_etf_1m"), " pt"), _gt(c.s.get("stock_vs_etf_1m"), 0)),
        _g("Price > 200 EMA", f"{_fmt(c.close)} vs {_fmt(c.ema200)}", _above(c.close, c.ema200)),
        _g("RSI(14) 50–70", _fmt(c.rsi14, digits=1), _between(c.rsi14, 50, 70)),
        _g("Market cap > $2B", _fmt(c.f.get("market_cap")), _gt(c.f.get("market_cap"), 2e9)),
        _g(f"{etf} beats SPY over 3 months", _fmt(c.s.get("etf_vs_spy_3m"), " pt"), _gt(c.s.get("etf_vs_spy_3m"), 0), False),
    ]
    r.notes.append("Macro-cycle fit, ETF inflows and ETF top holdings are not checked (no free data).")
    r.entry, r.entry_note = c.close, "now, while the sector keeps relative strength"
    r.target1, r.target1_source = c.high_60d_prior, "60-session high"
    r.target2, r.target2_source = _analyst(c)
    r.stop, r.stop_source = _pct(c.close, -6), "−6% from entry"
    return r


def _analyst(c: Context) -> tuple[float | None, str]:
    mean, count = c.f.get("target_mean"), c.f.get("analyst_count")
    if mean is None:
        return None, "analyst consensus (no data)"
    return mean, (f"analyst consensus, {count or '?'} analysts, range "
                  f"{_fmt(c.f.get('target_low'))}–{_fmt(c.f.get('target_high'))}")


STRATEGIES = [mean_reversion, pead, vcp, gap_and_go, momentum, earnings_runup, high52, sector_rotation]


# -- scoring --------------------------------------------------------------

def score(result: StrategyResult, c: Context) -> StrategyResult:
    mandatory = [g for g in result.gates if g.mandatory]
    failed = len(result.mandatory_failed)
    if failed == 0:
        result.base = 70
    elif failed == 1:
        result.base = 45
    else:
        result.base = round(40 * (len(mandatory) - failed) / len(mandatory))

    adj = result.adjustments
    for gate in result.optional_passed:
        adj.append((f"Optional gate: {gate.name}", 5))
    rel, down = c.rel_volume, c.down_day
    if rel is not None:
        if rel >= 1.2 and down is False:
            adj.append((f"Volume confirms (rel. volume {rel}x on an up day)", 10))
        elif rel < 0.7:
            adj.append((f"Volume contradicts (thin, {rel}x)", -15))
        elif rel >= 1.2 and down and result.key != "mean_reversion":
            adj.append((f"Volume contradicts (heavy, {rel}x, on a down day)", -15))
    spx_vs_200 = c.r.get("spx_vs_200_pct")
    if spx_vs_200 is not None and spx_vs_200 < 0 and result.key in LONG_ONLY:
        adj.append(("S&P 500 below its 200-DMA (long-only strategy)", -20))
    if c.r.get("vix") is not None and c.r["vix"] > 25:
        adj.append((f"VIX above 25 ({c.r['vix']})", -10))
    growth = c.f.get("revenue_growth_pct")
    if growth is not None and growth < 0:
        adj.append((f"Fundamentals deteriorating (revenue growth {growth}%)", -20))
    to_next = c.e.get("sessions_to_next")
    if to_next is not None and to_next <= 5 and result.key not in {"pead", "runup"}:
        adj.append((f"Earnings within 5 days ({to_next} sessions)", -15))

    value = result.base + sum(delta for _, delta in adj)
    caps = result.caps
    if failed >= 2:
        caps.append("2+ mandatory gates fail → cap 40 (no setup)")
        value = min(value, 40)
    if result.disqualifiers:
        caps.append("Disqualifier present → cap 40")
        value = min(value, 40)
    if spx_vs_200 is not None and spx_vs_200 < 0 and result.key in LONG_ONLY and value > 50:
        caps.append("Long-only strategy in a market below its 200-DMA → cap 50")
        value = 50
    regime = c.r.get("regime")
    allowed = {"panic": set(), "bear": {"mean_reversion"}, "transition": {"pead", "mean_reversion"}}
    if regime in allowed and result.key not in allowed[regime] and value > 40:
        caps.append(f"{regime.title()} regime does not allow this strategy → cap 40")
        value = 40
    if value > MAX_CONFIDENCE:
        caps.append(f"Maximum reported confidence is {MAX_CONFIDENCE}")
    result.confidence = int(max(0, min(MAX_CONFIDENCE, value)))
    return result


def evaluate(technical: dict, fundamentals: dict, earnings: dict, regime: dict, sector: dict,
             news_recent_trusted: bool = False) -> list[StrategyResult]:
    """Return all 8 strategies, best first."""
    if technical.get("status") != "available":
        return []
    context = Context(technical, fundamentals, earnings, regime, sector, news_recent_trusted)
    results = [score(build(context), context) for build in STRATEGIES]
    mandatory_ratio = lambda r: sum(g.ok for g in r.gates if g.mandatory) / max(1, sum(g.mandatory for g in r.gates))  # noqa: E731
    return sorted(results, key=lambda r: (r.confidence, mandatory_ratio(r)), reverse=True)
