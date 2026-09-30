"""Gold Fx — multi-source live gold price feed.

Priority for spot ticker:
  1) Median of Binance PAXGUSDT + XAUTUSDT + public XAU spot APIs
  2) Fallback to whichever source responds

Klines (candles) still come from Binance PAXGUSDT (free historical bars).
"""
from __future__ import annotations

import json
import statistics
import time
import urllib.error
import urllib.request
from typing import Any

_CACHE: dict[str, tuple[float, Any]] = {}
_UA = {"User-Agent": "GoldFx/1.1", "Accept": "application/json"}

BINANCE_BASES = [
    "https://data-api.binance.vision/api/v3/",
    "https://api.binance.com/api/v3/",
    "https://api1.binance.com/api/v3/",
]


def _get_json(url: str, timeout: float = 5.0) -> Any:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _binance_price(symbol: str) -> float | None:
    for base in BINANCE_BASES:
        try:
            d = _get_json(base + f"ticker/price?symbol={symbol}", timeout=4)
            return float(d["price"])
        except Exception:
            continue
    return None


def _spot_gold_api() -> float | None:
    """https://api.gold-api.com/price/XAU — free, no key."""
    try:
        d = _get_json("https://api.gold-api.com/price/XAU", timeout=5)
        p = d.get("price")
        return float(p) if p is not None else None
    except Exception:
        return None


def _spot_goldprice_dev() -> float | None:
    """https://api.goldprice.dev — free anonymous spot."""
    try:
        d = _get_json(
            "https://api.goldprice.dev/v1/prices?symbol=XAU-USD-SPOT",
            timeout=5,
        )
        syms = d.get("symbols") or []
        if not syms:
            return None
        if syms[0].get("is_stale"):
            return None
        return float(syms[0]["price"])
    except Exception:
        return None


def live_gold_price() -> dict[str, Any]:
    """Return blended XAUUSD-style price from multiple real sources."""
    cache_key = "gold_live"
    hit = _CACHE.get(cache_key)
    if hit and time.time() - hit[0] < 2.0:
        return hit[1]

    sources: dict[str, float] = {}
    paxg = _binance_price("PAXGUSDT")
    if paxg:
        sources["PAXGUSDT"] = paxg
    xaut = _binance_price("XAUTUSDT")
    if xaut:
        sources["XAUTUSDT"] = xaut
    ga = _spot_gold_api()
    if ga:
        sources["gold-api"] = ga
    gd = _spot_goldprice_dev()
    if gd:
        sources["goldprice.dev"] = gd

    if not sources:
        raise RuntimeError("all gold price sources failed")

    vals = list(sources.values())
    if len(vals) >= 3:
        price = float(statistics.median(vals))
        method = "median"
    elif len(vals) == 2:
        price = float(sum(vals) / 2)
        method = "average"
    else:
        price = float(vals[0])
        method = "single"

    out = {
        "symbol": "XAUUSD",
        "price": f"{price:.2f}",
        "price_num": round(price, 2),
        "sources": {k: round(v, 2) for k, v in sources.items()},
        "method": method,
        "primary_feed": "PAXGUSDT",
    }
    _CACHE[cache_key] = (time.time(), out)
    return out


def binance_klines(symbol: str, interval: str, limit: int) -> list:
    path = f"klines?symbol={symbol}&interval={interval}&limit={limit}"
    last = None
    for base in BINANCE_BASES:
        try:
            return _get_json(base + path, timeout=6)
        except Exception as e:
            last = e
    raise last or RuntimeError("klines failed")
