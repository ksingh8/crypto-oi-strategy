import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import binance_client as bc
import database as db
import paper_trader as pt
from strategy import generate_signal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
CONFIG = {
    "symbols":               os.getenv("SYMBOLS", "ETHUSDT,AVAXUSDT,LTCUSDT").split(","),
    "tp_pct":                float(os.getenv("TP_PCT",            "3.0")),   # kept for reference; TP is now dynamic
    "sl_pct":                float(os.getenv("SL_PCT",            "1.2")),   # kept for reference; SL is now dynamic
    "min_confidence":        float(os.getenv("MIN_CONFIDENCE",    "65")),
    "position_size_usd":     float(os.getenv("POSITION_SIZE_USD", "100")),
    "signal_interval_minutes": int(os.getenv("SIGNAL_INTERVAL",  "5")),
    "strategy_version":      "v2_sr",
}

STRATEGY_VERSION = "v2_sr"

scheduler          = AsyncIOScheduler()
latest_market_data = {}

# ── Core loop ──────────────────────────────────────────────────────────────────
async def run_strategy_cycle():
    for symbol in CONFIG["symbols"]:
        try:
            market_data = await bc.get_all_market_data(symbol)
            latest_market_data[symbol] = {
                "data":       market_data,
                "updated_at": datetime.utcnow().isoformat()
            }

            ticker    = market_data.get("ticker") or {}
            oi        = market_data.get("oi")      or {}
            funding   = market_data.get("funding") or {}
            ls_history = market_data.get("ls_ratio") or []

            current_price = ticker.get("price", 0)
            if not current_price:
                continue

            # Save price snapshot
            taker_history = market_data.get("taker_ratio") or []
            taker_last    = taker_history[-1] if taker_history else {}
            ls_ratio      = ls_history[-1]["long_short_ratio"] if ls_history else None
            db.save_price_snapshot(
                symbol, current_price,
                oi=oi.get("oi"),
                funding_rate=funding.get("funding_rate"),
                ls_ratio=ls_ratio,
                buy_vol=taker_last.get("buy_vol"),
                sell_vol=taker_last.get("sell_vol"),
                taker_ratio=taker_last.get("buy_sell_ratio"),
            )

            # Check existing positions
            klines = market_data.get("klines") or []
            pt.check_open_positions(current_price, symbol, klines)

            # Generate signal
            signal = generate_signal(market_data, CONFIG)
            db.save_signal(signal, symbol, strategy_version=STRATEGY_VERSION)

            logger.info(f"[{symbol}] {signal.direction} conf={signal.confidence} price={current_price}")

            if signal.direction != "NEUTRAL":
                pt.open_paper_trade(signal, symbol, CONFIG, strategy_version=STRATEGY_VERSION)

        except Exception as e:
            logger.error(f"Error in strategy cycle [{symbol}]: {e}", exc_info=True)


async def check_positions_only():
    for symbol in CONFIG["symbols"]:
        try:
            if not db.get_open_trades(symbol):
                continue
            ticker = await bc.get_ticker_24h(symbol)
            current_price = ticker.get("price", 0)
            if not current_price:
                continue
            klines = await bc.get_klines(symbol, "5m", 10)
            pt.check_open_positions(current_price, symbol, klines)
        except Exception as e:
            logger.warning(f"Position check error [{symbol}]: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.add_job(run_strategy_cycle, "interval",
                      minutes=CONFIG["signal_interval_minutes"], id="strategy")
    scheduler.add_job(check_positions_only, "interval", minutes=1, id="pos_check")
    scheduler.start()
    asyncio.create_task(run_strategy_cycle())
    yield
    scheduler.shutdown()


app = FastAPI(title="SR Order Flow Bot", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── API Routes ─────────────────────────────────────────────────────────────────
@app.get("/api/stats")
def get_stats(v: str = Query(default="v2_sr")):
    """v=v2_sr (default) | v=v1_oi | v=all"""
    version = None if v == "all" else v
    return db.get_stats(strategy_version=version)

@app.get("/api/trades")
def get_trades(limit: int = 100, v: str = Query(default="v2_sr")):
    version = None if v == "all" else v
    return db.get_all_trades(limit, strategy_version=version)

@app.get("/api/signals")
def get_signals(limit: int = 50, v: str = Query(default="v2_sr")):
    version = None if v == "all" else v
    return db.get_recent_signals(limit, strategy_version=version)

@app.get("/api/open-trades")
def get_open_trades():
    return db.get_open_trades()

@app.get("/api/price-history/{symbol}")
def price_history(symbol: str, limit: int = 200):
    return db.get_price_history(symbol.upper(), limit)

@app.get("/api/market-data/{symbol}")
async def market_data_live(symbol: str):
    sym    = symbol.upper()
    cached = latest_market_data.get(sym)
    if cached:
        return cached
    try:
        data = await bc.get_all_market_data(sym)
        latest_market_data[sym] = {"data": data, "updated_at": datetime.utcnow().isoformat()}
        return latest_market_data[sym]
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/config")
def get_config():
    return CONFIG

@app.post("/api/run-now")
async def run_now():
    await run_strategy_cycle()
    return {"status": "ok", "ran_at": datetime.utcnow().isoformat()}

@app.post("/api/close-trade/{trade_id}")
def manual_close(trade_id: int, price: float):
    db.close_trade(trade_id, price, "MANUAL")
    return {"status": "closed"}

@app.get("/", response_class=HTMLResponse)
def dashboard():
    with open("../frontend/index.html", encoding="utf-8") as f:
        return f.read()
