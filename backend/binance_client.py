import httpx
import asyncio
from typing import Optional
import logging

logger = logging.getLogger(__name__)

BASE_FUTURES = "https://fapi.binance.com"
BASE_SPOT = "https://api.binance.com"

async def fetch(url: str, params: dict = {}) -> dict | list:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()

async def get_open_interest(symbol: str) -> dict:
    """Current OI snapshot"""
    data = await fetch(f"{BASE_FUTURES}/fapi/v1/openInterest", {"symbol": symbol})
    oi = float(data.get("openInterest", 0))
    # openInterestValue may be restricted by region — estimate from mark price if missing
    oi_usd = float(data["openInterestValue"]) if "openInterestValue" in data else 0.0
    return {"oi": oi, "oi_usd": oi_usd}

async def get_oi_history(symbol: str, period: str = "5m", limit: int = 100) -> list:
    """OI history for trend analysis"""
    data = await fetch(f"{BASE_FUTURES}/futures/data/openInterestHist", {
        "symbol": symbol, "period": period, "limit": limit
    })
    return [{"timestamp": d["timestamp"], "oi": float(d["sumOpenInterest"]),
             "oi_usd": float(d["sumOpenInterestValue"])} for d in data]

async def get_funding_rate(symbol: str) -> dict:
    """Current funding rate"""
    data = await fetch(f"{BASE_FUTURES}/fapi/v1/premiumIndex", {"symbol": symbol})
    return {
        "funding_rate": float(data["lastFundingRate"]),
        "mark_price": float(data["markPrice"]),
        "index_price": float(data["indexPrice"])
    }

async def get_funding_history(symbol: str, limit: int = 50) -> list:
    data = await fetch(f"{BASE_FUTURES}/fapi/v1/fundingRate", {"symbol": symbol, "limit": limit})
    return [{"timestamp": d["fundingTime"], "rate": float(d["fundingRate"])} for d in data]

async def get_klines(symbol: str, interval: str = "5m", limit: int = 200) -> list:
    """OHLCV candles"""
    data = await fetch(f"{BASE_FUTURES}/fapi/v1/klines", {
        "symbol": symbol, "interval": interval, "limit": limit
    })
    return [{
        "timestamp": d[0], "open": float(d[1]), "high": float(d[2]),
        "low": float(d[3]), "close": float(d[4]), "volume": float(d[5]),
        "close_time": d[6]
    } for d in data]

async def get_long_short_ratio(symbol: str, period: str = "5m", limit: int = 50) -> list:
    """Global long/short account ratio"""
    data = await fetch(f"{BASE_FUTURES}/futures/data/globalLongShortAccountRatio", {
        "symbol": symbol, "period": period, "limit": limit
    })
    return [{"timestamp": d["timestamp"], "long_short_ratio": float(d["longShortRatio"]),
             "long_account": float(d["longAccount"]), "short_account": float(d["shortAccount"])}
            for d in data]

async def get_liquidations(symbol: str, limit: int = 50) -> list:
    """Recent liquidation orders"""
    try:
        data = await fetch(f"{BASE_FUTURES}/fapi/v1/allForceOrders", {
            "symbol": symbol, "limit": limit
        })
        return [{"timestamp": d["time"], "side": d["side"],
                 "quantity": float(d["origQty"]), "price": float(d["price"]),
                 "usd_value": float(d["origQty"]) * float(d["price"])} for d in data]
    except Exception:
        return []

async def get_ticker_24h(symbol: str) -> dict:
    data = await fetch(f"{BASE_FUTURES}/fapi/v1/ticker/24hr", {"symbol": symbol})
    return {
        "price": float(data["lastPrice"]),
        "price_change_pct": float(data["priceChangePercent"]),
        "volume": float(data["volume"]),
        "high": float(data["highPrice"]),
        "low": float(data["lowPrice"])
    }

async def get_all_market_data(symbol: str) -> dict:
    """Fetch all needed data concurrently"""
    results = await asyncio.gather(
        get_open_interest(symbol),
        get_oi_history(symbol, "5m", 50),
        get_funding_rate(symbol),
        get_klines(symbol, "5m", 100),
        get_long_short_ratio(symbol, "5m", 50),
        get_ticker_24h(symbol),
        return_exceptions=True
    )
    keys = ["oi", "oi_history", "funding", "klines", "ls_ratio", "ticker"]
    out = {}
    for k, v in zip(keys, results):
        if isinstance(v, Exception):
            logger.warning(f"Failed fetching {k}: {v}")
            out[k] = None
        else:
            out[k] = v
    return out
