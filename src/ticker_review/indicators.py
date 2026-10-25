"""Technical indicators on daily OHLCV bars (pandas only)."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    out = 100 - 100 / (1 + rs)
    return out.fillna(100.0).where(loss != 0, 100.0)


def macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(close, 12) - ema(close, 26)
    signal = ema(line, 9)
    return line, signal, line - signal


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    return pd.concat(
        [df["High"] - df["Low"], (df["High"] - prev_close).abs(), (df["Low"] - prev_close).abs()], axis=1
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up = df["High"].diff()
    down = -df["Low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = true_range(df).ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / tr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def perf(close: pd.Series, sessions: int) -> float | None:
    if len(close) <= sessions:
        return None
    previous = float(close.iloc[-(sessions + 1)])
    return round((float(close.iloc[-1]) / previous - 1) * 100, 2) if previous else None


def _num(value: Any, digits: int = 2) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if math.isfinite(number) else None


def contractions(df: pd.DataFrame, windows: int = 3, size: int = 20) -> list[float]:
    """Max high-to-low pullback (%) in each of the last `windows` blocks of `size` sessions."""
    out: list[float] = []
    tail = df.tail(windows * size)
    if len(tail) < windows * size:
        return out
    for index in range(windows):
        block = tail.iloc[index * size:(index + 1) * size]
        running_high = block["High"].cummax()
        drawdown = ((block["Low"] / running_high) - 1).min() * 100
        out.append(round(abs(float(drawdown)), 2))
    return out


def technical_snapshot(df: pd.DataFrame) -> dict[str, Any]:
    """Compute every value the strategies need. Missing history gives None values."""
    df = df.dropna(subset=["Close"]).copy()
    if len(df) < 2:
        return {"status": "unavailable", "reason": "Not enough price history."}
    close = df["Close"]
    last, prev = df.iloc[-1], df.iloc[-2]
    rsi14 = rsi(close)
    macd_line, macd_signal, macd_hist = macd(close)
    ema200 = ema(close, 200)
    atr14 = atr(df)
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    year = df.tail(252)
    prior = df.iloc[:-1]

    avg_vol = lambda n: _num(prior["Volume"].tail(n).mean(), 0) if len(prior) >= n else None  # noqa: E731
    avg50 = avg_vol(50)
    rel_volume = _num(float(last["Volume"]) / avg50, 2) if avg50 else None
    high_52w = _num(year["High"].max())
    low_52w = _num(year["Low"].min())
    close_now = float(last["Close"])

    prior_year = df.iloc[:-1].tail(252)
    prior_high_age = None
    if len(prior_year) > 1:
        prior_high_age = int(len(prior_year) - 1 - int(prior_year["High"].values.argmax()))

    body = abs(float(last["Close"]) - float(last["Open"]))
    lower_wick = min(float(last["Close"]), float(last["Open"])) - float(last["Low"])
    day_range = float(last["High"]) - float(last["Low"])
    rejection = bool(
        day_range > 0
        and lower_wick >= 2 * max(body, 1e-9)
        and (float(last["Close"]) - float(last["Low"])) / day_range >= 0.5
    )
    cons = contractions(df)
    vol_blocks = [float(b["Volume"].mean()) for b in (df.tail(60).iloc[0:20], df.tail(60).iloc[40:60])] if len(df) >= 60 else []

    snapshot = {
        "status": "available",
        "as_of": df.index[-1].date().isoformat() if hasattr(df.index[-1], "date") else str(df.index[-1]),
        "sessions": len(df),
        "open": _num(last["Open"]), "high": _num(last["High"]), "low": _num(last["Low"]),
        "close": _num(close_now), "prev_close": _num(prev["Close"]),
        "volume": _num(last["Volume"], 0),
        "avg_volume_10": avg_vol(10), "avg_volume_20": avg_vol(20), "avg_volume_50": avg50,
        "rel_volume": rel_volume,
        "avg_turnover_20": _num((prior["Close"] * prior["Volume"]).tail(20).mean(), 0) if len(prior) >= 20 else None,
        "rsi14": _num(rsi14.iloc[-1], 1),
        "macd": _num(macd_line.iloc[-1], 3), "macd_signal": _num(macd_signal.iloc[-1], 3),
        "macd_hist": _num(macd_hist.iloc[-1], 3),
        "ema21": _num(ema(close, 21).iloc[-1]), "ema50": _num(ema(close, 50).iloc[-1]),
        "ema150": _num(ema(close, 150).iloc[-1]), "ema200": _num(ema200.iloc[-1]),
        "ema200_month_ago": _num(ema200.iloc[-22]) if len(ema200) > 22 else None,
        "sma20": _num(sma20.iloc[-1]),
        "bb_upper": _num((sma20 + 2 * std20).iloc[-1]), "bb_lower": _num((sma20 - 2 * std20).iloc[-1]),
        "atr14": _num(atr14.iloc[-1]),
        "atr14_avg50": _num(atr14.tail(50).mean()) if len(atr14) >= 50 else None,
        "adx14": _num(adx(df).iloc[-1], 1),
        "perf_5d": perf(close, 5), "perf_1m": perf(close, 21), "perf_3m": perf(close, 63), "perf_6m": perf(close, 126),
        "high_52w": high_52w, "low_52w": low_52w,
        "pct_from_52w_high": _num((close_now / high_52w - 1) * 100) if high_52w else None,
        "pct_above_52w_low": _num((close_now / low_52w - 1) * 100) if low_52w else None,
        "prior_52w_high_age_sessions": prior_high_age,
        "high_52w_within_3m": bool(len(year) and float(df.tail(63)["High"].max()) >= float(year["High"].max())),
        "gap_pct": _num((float(last["Open"]) / float(prev["Close"]) - 1) * 100) if float(prev["Close"]) else None,
        "high_60d_prior": _num(prior["High"].tail(60).max()) if len(prior) >= 20 else None,
        "high_20d_prior": _num(prior["High"].tail(20).max()) if len(prior) >= 20 else None,
        "last_contraction_low": _num(df.tail(20)["Low"].min()) if len(df) >= 20 else None,
        "bullish_rejection_candle": rejection,
        "down_day": close_now < float(prev["Close"]),
        "contractions": cons,
        "volume_dry_up": (vol_blocks[1] < vol_blocks[0]) if vol_blocks else None,
    }
    return snapshot
