"""Collect normalized market snapshots with isolated per-symbol failures."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from opentelemetry.trace import Status, StatusCode

from financial_alert.telemetry import get_tracer, record_error

logger = logging.getLogger(__name__)
DATA_SOURCE = "Yahoo Finance via yfinance; data can be delayed and coverage varies"
TRACER = get_tracer(__name__)


class MarketDataAgent:
    """Fetch quote and history data; convert provider failures to snapshot records."""

    def collect(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        unique_symbols = list(dict.fromkeys(symbols))
        snapshots: dict[str, dict[str, Any]] = {}
        with TRACER.start_as_current_span("fetch_all_market_data") as parent:
            parent.set_attribute("market.symbol_count", len(unique_symbols))
            for symbol in unique_symbols:
                with TRACER.start_as_current_span(f"fetch_market_data_{symbol}") as span:
                    span.set_attribute("market.symbol", symbol)
                    try:
                        snapshot = self._collect_one(symbol)
                        snapshots[symbol] = snapshot
                        span.set_attribute("market.outcome", snapshot["status"])
                        if snapshot["status"] != "available":
                            span.set_status(Status(StatusCode.ERROR, snapshot.get("reason", "Unavailable")))
                            span.set_attribute("error.type", "market_data_unavailable")
                    except Exception as exc:
                        record_error(span, exc)
                        logger.exception("Market data unavailable for %s.", symbol)
                        provider_symbol, instrument_note = self._provider_symbol(symbol)
                        snapshots[symbol] = self._unavailable(
                            symbol,
                            "Price or volume data could not be retrieved.",
                            provider_symbol,
                            instrument_note,
                        )
            parent.set_attribute(
                "market.success_count",
                sum(snapshot.get("status") == "available" for snapshot in snapshots.values()),
            )
            parent.set_attribute("market.failure_count", len(snapshots) - sum(
                snapshot.get("status") == "available" for snapshot in snapshots.values()
            ))
        return snapshots

    def _collect_one(self, requested_symbol: str) -> dict[str, Any]:
        import yfinance as yf

        provider_symbol, instrument_note = self._provider_symbol(requested_symbol)
        ticker = yf.Ticker(provider_symbol)
        is_gold_futures = provider_symbol == "GC=F"

        if is_gold_futures:
            # Use the requested one-day daily bar as the gold futures price basis.
            try:
                latest_history = ticker.history(period="1d")
            except Exception as exc:
                logger.warning("Gold futures close unavailable: %s", exc)
                return self._unavailable(
                    requested_symbol,
                    "The gold futures daily close could not be retrieved.",
                    provider_symbol,
                    instrument_note,
                )
            if latest_history.empty or "Close" not in latest_history:
                return self._unavailable(
                    requested_symbol,
                    "No daily close returned for the gold futures contract.",
                    provider_symbol,
                    instrument_note,
                )
            latest_closes = latest_history["Close"].dropna()
            if latest_closes.empty:
                return self._unavailable(
                    requested_symbol,
                    "The gold futures daily bar did not contain a usable close.",
                    provider_symbol,
                    instrument_note,
                )
            latest_close = float(latest_closes.iloc[-1])
            history_as_of = latest_history.index[-1].date().isoformat()
            try:
                history = ticker.history(period="1y", interval="1d", auto_adjust=False)
            except Exception as exc:
                logger.info("One-year gold history unavailable: %s", exc)
                history = None
        else:
            history = ticker.history(period="1y", interval="1d", auto_adjust=False)
            if history is None or history.empty or "Close" not in history:
                return self._unavailable(
                    requested_symbol,
                    "No daily price history returned.",
                    provider_symbol,
                    instrument_note,
                )
            closes = history["Close"].dropna()
            if closes.empty:
                return self._unavailable(
                    requested_symbol,
                    "Price history did not contain usable closing prices.",
                    provider_symbol,
                    instrument_note,
                )
            latest_close = float(closes.iloc[-1])
            history_as_of = history.index[-1].date().isoformat()

        history_closes = (
            history["Close"].dropna()
            if history is not None and not history.empty and "Close" in history
            else None
        )
        currency = None
        quote_price = None
        try:
            fast_info = ticker.fast_info
            currency = fast_info.get("currency")
            if not is_gold_futures:
                quote_price = self._positive_number(fast_info.get("last_price"))
        except Exception as exc:
            logger.info("Quote currency metadata unavailable for %s: %s", requested_symbol, exc)

        year_low, year_high = self._year_range(history)
        screen_price = quote_price if quote_price is not None else latest_close
        range_position = self._range_position(screen_price, year_low, year_high)
        volume_metrics = (
            self._volume_metrics(history)
            if history is not None and not history.empty and not is_gold_futures
            else self._missing_volume_metrics()
        )
        return {
            "status": "available",
            "provider_symbol": provider_symbol,
            "instrument_note": instrument_note,
            "source": DATA_SOURCE,
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "quote_currency": currency,
            "current_price": screen_price,
            "price_basis": "provider latest quote" if quote_price is not None else "latest daily close",
            "latest_close": latest_close,
            "history_as_of": history_as_of,
            "year_low": year_low,
            "year_high": year_high,
            "range_position_percent": range_position,
            "price_zone": self._price_zone(range_position) if range_position is not None else "not available",
            **(self._price_momentum(history_closes) if history_closes is not None else self._missing_momentum()),
            **volume_metrics,
        }

    @staticmethod
    def _positive_number(value: Any) -> float | None:
        try:
            number = float(value)
            return number if math.isfinite(number) and number > 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _positive_number(value: Any) -> float | None:
        try:
            number = float(value)
            return number if math.isfinite(number) and number > 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _year_range(history: Any) -> tuple[float | None, float | None]:
        if history is None or history.empty or "Close" not in history:
            return None, None
        closes = history["Close"].dropna()
        if closes.empty:
            return None, None
        lows = history["Low"].dropna() if "Low" in history else closes
        highs = history["High"].dropna() if "High" in history else closes
        return (float(lows.min()), float(highs.max())) if not lows.empty and not highs.empty else (None, None)

    @staticmethod
    def _range_position(price: float, year_low: float | None, year_high: float | None) -> float | None:
        if year_low is None or year_high is None:
            return None
        range_width = year_high - year_low
        return round(max(0.0, min(100.0, (price - year_low) / range_width * 100.0)), 1) if range_width > 0 else 50.0

    @staticmethod
    def _price_momentum(closes: Any) -> dict[str, float | None]:
        def change_over_sessions(session_count: int) -> float | None:
            if len(closes) <= session_count:
                return None
            previous = float(closes.iloc[-(session_count + 1)])
            current = float(closes.iloc[-1])
            return round((current / previous - 1) * 100, 2) if previous else None

        return {
            "change_5_sessions_percent": change_over_sessions(5),
            "change_20_sessions_percent": change_over_sessions(20),
        }

    @staticmethod
    def _missing_momentum() -> dict[str, None]:
        return {"change_5_sessions_percent": None, "change_20_sessions_percent": None}

    @staticmethod
    def _volume_metrics(history: Any) -> dict[str, float | int | None]:
        if "Volume" not in history:
            return MarketDataAgent._missing_volume_metrics()
        volume = history["Volume"].dropna()
        if volume.empty or not (volume > 0).any():
            return MarketDataAgent._missing_volume_metrics()

        recent_volume = volume.tail(5)
        prior_volume = volume.iloc[-10:-5]
        average_volume = float(volume.tail(20).mean()) if not volume.tail(20).empty else None
        volume_change = None
        if not recent_volume.empty and not prior_volume.empty and float(prior_volume.mean()) != 0:
            volume_change = round((float(recent_volume.mean()) / float(prior_volume.mean()) - 1) * 100, 1)
        turnover = None
        if average_volume is not None and "Close" in history:
            recent = history[["Close", "Volume"]].tail(20).dropna()
            if not recent.empty:
                turnover = round(float((recent["Close"] * recent["Volume"]).mean()), 2)
        returns = history["Close"].pct_change().dropna().tail(20)
        volatility = round(float(returns.std() * math.sqrt(252) * 100), 1) if len(returns) > 1 else None
        return {
            "average_daily_volume_20_sessions": round(average_volume, 1) if average_volume is not None else None,
            "volume_change_5_sessions_percent": volume_change,
            "average_daily_turnover_20_sessions": turnover,
            "annualized_volatility_20_sessions_percent": volatility,
        }

    @staticmethod
    def _missing_volume_metrics() -> dict[str, float | None]:
        return {
            "average_daily_volume_20_sessions": None,
            "volume_change_5_sessions_percent": None,
            "average_daily_turnover_20_sessions": None,
            "annualized_volatility_20_sessions_percent": None,
        }

    @staticmethod
    def _price_zone(position: float) -> str:
        if position <= 20:
            return "near 1-year low"
        if position >= 80:
            return "near 1-year high"
        return "middle of 1-year range"

    @staticmethod
    def _provider_symbol(symbol: str) -> tuple[str, str | None]:
        if symbol.strip().lower() in {"gold (xau/usd)", "gold", "xau/usd"}:
            return "GC=F", "Continuous Gold Futures contract (GC=F), not spot XAU/USD."
        return symbol, None

    @staticmethod
    def _unavailable(
        symbol: str,
        reason: str,
        provider_symbol: str | None = None,
        instrument_note: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "symbol": symbol,
            "provider_symbol": provider_symbol or symbol,
            "instrument_note": instrument_note,
            "source": DATA_SOURCE,
            "reason": reason,
            "current_price": None,
            "price_basis": "unavailable",
            "latest_close": None,
            "history_as_of": None,
            "quote_currency": None,
            "year_low": None,
            "year_high": None,
            "range_position_percent": None,
            "price_zone": "not available",
            **MarketDataAgent._missing_momentum(),
            **MarketDataAgent._missing_volume_metrics(),
        }
