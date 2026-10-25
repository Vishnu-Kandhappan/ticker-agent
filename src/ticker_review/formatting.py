"""Shared display helpers."""

from __future__ import annotations

from typing import Any

CURRENCY_SYMBOLS = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£", "JPY": "¥", "HKD": "HK$", "AED": "AED "}


def money(value: Any, currency: str | None = "USD") -> str:
    if value is None:
        return "n/a"
    symbol = CURRENCY_SYMBOLS.get((currency or "USD").upper(), f"{currency} ")
    return f"{symbol}{float(value):,.2f}"


def money_compact(value: Any, currency: str | None = "USD") -> str:
    if value is None:
        return "n/a"
    symbol = CURRENCY_SYMBOLS.get((currency or "USD").upper(), f"{currency} ")
    return f"{symbol}{qty(value)}"


def pct(value: Any, signed: bool = True) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):+.2f}%" if signed else f"{float(value):.2f}%"


def qty(value: Any) -> str:
    if value is None:
        return "n/a"
    value = float(value)
    if abs(value) >= 1e9:
        return f"{value / 1e9:.2f}B"
    if abs(value) >= 1e6:
        return f"{value / 1e6:.2f}M"
    if abs(value) >= 1e3:
        return f"{value / 1e3:.1f}K"
    return f"{value:.0f}"
