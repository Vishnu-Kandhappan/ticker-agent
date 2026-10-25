"""Market data from Yahoo Finance (yfinance): history, fundamentals, earnings, regime, sector."""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from financial_alert.telemetry import get_tracer, record_error
from ticker_review.indicators import perf, technical_snapshot

logger = logging.getLogger(__name__)
TRACER = get_tracer(__name__)
SOURCE = "Yahoo Finance via yfinance (data can be delayed)"

SECTOR_ETF = {
    "Technology": "XLK", "Financial Services": "XLF", "Energy": "XLE", "Healthcare": "XLV",
    "Industrials": "XLI", "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP",
    "Utilities": "XLU", "Real Estate": "XLRE", "Basic Materials": "XLB",
    "Communication Services": "XLC",
}


def _num(value: Any, digits: int = 2) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if math.isfinite(number) else None


class MarketData:
    """Thin wrapper so tests can replace the provider."""

    def __init__(self, provider: Any = None) -> None:
        if provider is None:
            import yfinance as provider  # noqa: PLW0127
        self.yf = provider
        self._history_cache: dict[str, pd.DataFrame | None] = {}

    def history(self, symbol: str, period: str = "2y") -> pd.DataFrame | None:
        if symbol in self._history_cache:
            return self._history_cache[symbol]
        with TRACER.start_as_current_span("market_history") as span:
            span.set_attribute("market.symbol", symbol)
            try:
                df = self.yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=False)
                df = df if df is not None and not df.empty else None
            except Exception as exc:
                record_error(span, exc)
                logger.warning("History unavailable for %s: %s", symbol, type(exc).__name__)
                df = None
        self._history_cache[symbol] = df
        return df

    # -- per ticker -------------------------------------------------------
    def snapshot(self, symbol: str) -> dict[str, Any]:
        df = self.history(symbol)
        tech = technical_snapshot(df) if df is not None else {"status": "unavailable", "reason": "No price history."}
        fundamentals = self.fundamentals(symbol)
        earnings = self.earnings(symbol, df)
        return {
            "symbol": symbol,
            "source": SOURCE,
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "technical": tech,
            "fundamentals": fundamentals,
            "earnings": earnings,
        }

    def fundamentals(self, symbol: str) -> dict[str, Any]:
        info: dict[str, Any] = {}
        try:
            info = self.yf.Ticker(symbol).info or {}
        except Exception as exc:
            logger.info("Fundamentals unavailable for %s: %s", symbol, type(exc).__name__)
        return {
            "name": info.get("longName") or info.get("shortName") or symbol,
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "currency": info.get("currency"),
            "quote_type": info.get("quoteType"),
            "market_cap": _num(info.get("marketCap"), 0),
            "trailing_pe": _num(info.get("trailingPE")),
            "revenue_growth_pct": _num(info["revenueGrowth"] * 100) if _num(info.get("revenueGrowth")) is not None else None,
            "earnings_growth_pct": _num(info["earningsGrowth"] * 100) if _num(info.get("earningsGrowth")) is not None else None,
            "target_mean": _num(info.get("targetMeanPrice")),
            "target_low": _num(info.get("targetLowPrice")),
            "target_high": _num(info.get("targetHighPrice")),
            "analyst_count": info.get("numberOfAnalystOpinions"),
            "recommendation": info.get("recommendationKey"),
        }

    def earnings(self, symbol: str, df: pd.DataFrame | None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "next_date": None, "sessions_to_next": None, "last_date": None, "sessions_since_last": None,
            "last_surprise_pct": None, "beats_last4": None, "reported_last4": 0, "avg_surprise_last4": None,
            "reaction_change_pct": None, "reaction_rel_volume": None,
        }
        try:
            table = self.yf.Ticker(symbol).get_earnings_dates(limit=12)
        except Exception as exc:
            logger.info("Earnings dates unavailable for %s: %s", symbol, type(exc).__name__)
            table = None
        if table is None or getattr(table, "empty", True):
            return out

        table = table.sort_index()
        today = pd.Timestamp(date.today())
        reported_col = "Reported EPS" if "Reported EPS" in table else None
        surprise_col = next((c for c in table.columns if "Surprise" in c), None)
        index_dates = [pd.Timestamp(ts).tz_localize(None) if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts) for ts in table.index]
        table = table.set_axis(index_dates)

        future = table[(table.index.normalize() >= today) & (table[reported_col].isna() if reported_col else True)]
        if not future.empty:
            next_ts = future.index[0]
            out["next_date"] = next_ts.date().isoformat()
            out["sessions_to_next"] = int(np.busday_count(today.date(), next_ts.date()))

        past = table[table[reported_col].notna()] if reported_col else table[table.index < today]
        if past.empty:
            return out
        last_ts = past.index[-1]
        out["last_date"] = last_ts.date().isoformat()
        if surprise_col:
            surprises = pd.to_numeric(past[surprise_col], errors="coerce").dropna().tail(4)
            if not surprises.empty:
                out["last_surprise_pct"] = _num(surprises.iloc[-1])
                out["beats_last4"] = int((surprises > 0).sum())
                out["reported_last4"] = int(len(surprises))
                out["avg_surprise_last4"] = _num(surprises.mean())

        if df is not None and not df.empty:
            sessions = df.index.tz_localize(None) if df.index.tz is not None else df.index
            # After-close reports move the next session; pre-market reports move the same day.
            reaction_day = last_ts.normalize() + (pd.Timedelta(days=1) if last_ts.hour >= 16 else pd.Timedelta(0))
            positions = np.where(sessions.normalize() >= reaction_day)[0]
            if len(positions):
                pos = int(positions[0])
                out["sessions_since_last"] = int(len(df) - 1 - pos)
                if pos >= 1:
                    out["reaction_change_pct"] = _num((df["Close"].iloc[pos] / df["Close"].iloc[pos - 1] - 1) * 100)
                if pos >= 10:
                    base = df["Volume"].iloc[pos - 10:pos].mean()
                    out["reaction_rel_volume"] = _num(df["Volume"].iloc[pos] / base) if base else None
        return out

    # -- market wide ------------------------------------------------------
    def regime(self) -> dict[str, Any]:
        spx = self.history("^GSPC")
        vix = self.history("^VIX", period="1mo")
        out: dict[str, Any] = {"spx_close": None, "spx_sma200": None, "spx_sma50": None,
                               "spx_vs_200_pct": None, "spx_above_50": None, "vix": None,
                               "regime": "unknown", "spx_perf_3m": None, "spx_perf_6m": None}
        if spx is not None and len(spx) >= 200:
            close = spx["Close"].dropna()
            out["spx_close"] = _num(close.iloc[-1])
            out["spx_sma200"] = _num(close.tail(200).mean())
            out["spx_sma50"] = _num(close.tail(50).mean())
            out["spx_vs_200_pct"] = _num((close.iloc[-1] / close.tail(200).mean() - 1) * 100)
            out["spx_above_50"] = bool(close.iloc[-1] > close.tail(50).mean())
            out["spx_perf_3m"] = perf(close, 63)
            out["spx_perf_6m"] = perf(close, 126)
        if vix is not None and not vix.empty:
            out["vix"] = _num(vix["Close"].dropna().iloc[-1])
        out["regime"] = classify_regime(out["spx_vs_200_pct"], out["vix"])
        return out

    def sector(self, sector_name: str | None, stock_perf_1m: float | None) -> dict[str, Any]:
        etf = SECTOR_ETF.get(sector_name or "")
        out: dict[str, Any] = {"etf": etf, "etf_perf_1m": None, "etf_perf_3m": None,
                               "spy_perf_1m": None, "spy_perf_3m": None,
                               "etf_vs_spy_1m": None, "etf_vs_spy_3m": None, "stock_vs_etf_1m": None}
        spy = self.history("SPY")
        if spy is not None:
            out["spy_perf_1m"] = perf(spy["Close"].dropna(), 21)
            out["spy_perf_3m"] = perf(spy["Close"].dropna(), 63)
        if etf:
            history = self.history(etf)
            if history is not None:
                out["etf_perf_1m"] = perf(history["Close"].dropna(), 21)
                out["etf_perf_3m"] = perf(history["Close"].dropna(), 63)
        if out["etf_perf_1m"] is not None and out["spy_perf_1m"] is not None:
            out["etf_vs_spy_1m"] = _num(out["etf_perf_1m"] - out["spy_perf_1m"])
        if out["etf_perf_3m"] is not None and out["spy_perf_3m"] is not None:
            out["etf_vs_spy_3m"] = _num(out["etf_perf_3m"] - out["spy_perf_3m"])
        if out["etf_perf_1m"] is not None and stock_perf_1m is not None:
            out["stock_vs_etf_1m"] = _num(stock_perf_1m - out["etf_perf_1m"])
        return out


def classify_regime(spx_vs_200_pct: float | None, vix: float | None) -> str:
    if spx_vs_200_pct is None or vix is None:
        return "unknown"
    if spx_vs_200_pct <= -5 and vix > 35:
        return "panic"
    if abs(spx_vs_200_pct) <= 3 and 20 <= vix <= 28:
        return "transition"
    if spx_vs_200_pct < 0:
        return "bear"
    if spx_vs_200_pct >= 5 and vix < 18:
        return "strong bull"
    return "cautious bull"
