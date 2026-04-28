import httpx
import asyncio
from typing import Optional
import logging

logger = logging.getLogger(__name__)

BASE_FUTURES = "https://fapi.binance.com"
BASE_SPOT    = "https://api.binance.com"

async def fetch(url: str, params: dict = {}) -> dict | list:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()

async def get_open_interest(symbol: str) -> dict:
    data = await fetch(f"{BASE_FUTURES}/fapi/v1/openInterest", {"symbol": symbol})
    oi = float(data.get("openInterest", 0))
    return {"oi": oi, "oi_usd": 0.0}

async def get_oi_history(symbol: str, period: str = "5m", limit: int = 20) -> list:
    data = await fetch(f"{BASE_FUTURES}/futures/data/openInterestHist", {
        "symbol": symbol, "period": period, "limit": limit
    })
    return [{"timestamp": d["timestamp"], "oi": float(d["sumOpenInterest"]),
             "oi_usd": float(d["sumOpenInterestValue"])} for d in data]

async def get_funding_rate(symbol: str) -> dict:
    data = await fetch(f"{BASE_FUTURES}/fapi/v1/premiumIndex", {"symbol": symbol})
    return {
        "funding_rate": float(data["lastFundingRate"]),
        "mark_price":   float(data["markPrice"]),
        "index_price":  float(data["indexPrice"])
    }

async def get_klines(symbol: str, interval: str = "5m", limit: int = 50) -> list:
    data = await fetch(f"{BASE_FUTURES}/fapi/v1/klines", {
        "symbol": symbol, "interval": interval, "limit": limit
    })
    return [{
        "timestamp": d[0], "open": float(d[1]), "high": float(d[2]),
        "low": float(d[3]), "close": float(d[4]), "volume": float(d[5]),
        "close_time": d[6]
    } for d in data]

async def get_long_short_ratio(symbol: str, period: str = "5m", limit: int = 50) -> list:
    data = await fetch(f"{BASE_FUTURES}/futures/data/globalLongShortAccountRatio", {
        "symbol": symbol, "period": period, "limit": limit
    })
    return [{"timestamp": d["timestamp"], "long_short_ratio": float(d["longShortRatio"]),
             "long_account": float(d["longAccount"]), "short_account": float(d["shortAccount"])}
            for d in data]

async def get_taker_ratio(symbol: str, period: str = "5m", limit: int = 24) -> list:
    data = await fetch(f"{BASE_FUTURES}/futures/data/takerlongshortRatio", {
        "symbol": symbol, "period": period, "limit": limit
    })
    return [{"timestamp": int(d["timestamp"]),
             "buy_sell_ratio": float(d["buySellRatio"]),
             "buy_vol":        float(d["buyVol"]),
             "sell_vol":       float(d["sellVol"])} for d in data]

async def get_ticker_24h(symbol: str) -> dict:
    data = await fetch(f"{BASE_FUTURES}/fapi/v1/ticker/24hr", {"symbol": symbol})
    return {
        "price":            float(data["lastPrice"]),
        "price_change_pct": float(data["priceChangePercent"]),
        "volume":           float(data["volume"]),
        "high":             float(data["highPrice"]),
        "low":              float(data["lowPrice"])
    }

async def get_orderbook(symbol: str, limit: int = 20) -> dict:
    """
    Fetch order book depth.
    Returns bids/asks as [[price_str, qty_str], ...] sorted best-first.
    limit: 5, 10, 20, 50, 100, 500, 1000
    """
    data = await fetch(f"{BASE_FUTURES}/fapi/v1/depth", {
        "symbol": symbol, "limit": limit
    })
    return {
        "bids": data.get("bids", []),  # [[price, qty], ...] descending
        "asks": data.get("asks", []),  # [[price, qty], ...] ascending
    }

async def get_all_market_data(symbol: str) -> dict:
    """Fetch all needed data concurrently for S/R + order flow strategy."""
    results = await asyncio.gather(
        get_open_interest(symbol),
        get_oi_history(symbol, "5m", 50),
        get_funding_rate(symbol),
        get_klines(symbol, "5m", 100),         # 5m klines — entry candles + CVD
        get_long_short_ratio(symbol, "5m", 50),
        get_ticker_24h(symbol),
        get_klines(symbol, "4h", 210),          # 4H klines — S/R level detection
        get_taker_ratio(symbol, "5m", 24),      # taker buy/sell — delta confirmation
        get_klines(symbol, "15m", 60),          # 15m klines — sweep + retest detection
        get_orderbook(symbol, limit=20),        # order book depth — who is in control
        return_exceptions=True
    )
    keys = ["oi", "oi_history", "funding", "klines", "ls_ratio", "ticker",
            "htf_klines", "taker_ratio", "klines_15m", "orderbook"]
    out = {}
    for k, v in zip(keys, results):
        if isinstance(v, Exception):
            logger.warning(f"Failed fetching {k} for {symbol}: {v}")
            out[k] = None
        else:
            out[k] = v
    return out
