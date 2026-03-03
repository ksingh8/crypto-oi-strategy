import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
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
    "symbols": os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(","),
    "tp_pct": float(os.getenv("TP_PCT", "1.5")),
    "sl_pct": float(os.getenv("SL_PCT", "0.8")),
    "min_confidence": float(os.getenv("MIN_CONFIDENCE", "45")),
    "position_size_usd": float(os.getenv("POSITION_SIZE_USD", "100")),
    "signal_interval_minutes": int(os.getenv("SIGNAL_INTERVAL", "5")),
}

scheduler = AsyncIOScheduler()
latest_market_data = {}  # in-memory cache

# ── Core loop ───────────────────────────────────────────────────────────────────
async def run_strategy_cycle():
    for symbol in CONFIG["symbols"]:
        try:
            market_data = await bc.get_all_market_data(symbol)
            latest_market_data[symbol] = {
                "data": market_data,
                "updated_at": datetime.utcnow().isoformat()
            }

            ticker = market_data.get("ticker") or {}
            oi = market_data.get("oi") or {}
            funding = market_data.get("funding") or {}
            ls_history = market_data.get("ls_ratio") or []

            current_price = ticker.get("price", 0)
            if not current_price:
                continue

            # Save snapshot
            ls_ratio = ls_history[-1]["long_short_ratio"] if ls_history else None
            db.save_price_snapshot(
                symbol, current_price,
                oi=oi.get("oi"),
                funding_rate=funding.get("funding_rate"),
                ls_ratio=ls_ratio
            )

            # Check existing positions
            pt.check_open_positions(current_price, symbol)

            # Generate signal
            signal = generate_signal(market_data, CONFIG)
            db.save_signal(signal, symbol)

            logger.info(f"[{symbol}] {signal.direction} confidence={signal.confidence} price={current_price}")

            # Open trade if warranted
            if signal.direction != "NEUTRAL":
                pt.open_paper_trade(signal, symbol, CONFIG)

        except Exception as e:
            logger.error(f"Error in strategy cycle for {symbol}: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.add_job(run_strategy_cycle, "interval",
                      minutes=CONFIG["signal_interval_minutes"], id="strategy")
    scheduler.start()
    # Run immediately on startup
    asyncio.create_task(run_strategy_cycle())
    yield
    scheduler.shutdown()


app = FastAPI(title="OI Strategy Bot", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── API Routes ──────────────────────────────────────────────────────────────────
@app.get("/api/stats")
def get_stats():
    return db.get_stats()

@app.get("/api/trades")
def get_trades(limit: int = 100):
    return db.get_all_trades(limit)

@app.get("/api/signals")
def get_signals(limit: int = 50):
    return db.get_recent_signals(limit)

@app.get("/api/open-trades")
def get_open_trades():
    return db.get_open_trades()

@app.get("/api/price-history/{symbol}")
def price_history(symbol: str, limit: int = 200):
    return db.get_price_history(symbol.upper(), limit)

@app.get("/api/market-data/{symbol}")
async def market_data_live(symbol: str):
    sym = symbol.upper()
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
    """Manually trigger a strategy cycle"""
    await run_strategy_cycle()
    return {"status": "ok", "ran_at": datetime.utcnow().isoformat()}

@app.post("/api/close-trade/{trade_id}")
def manual_close(trade_id: int, price: float):
    """Manually close a trade"""
    db.close_trade(trade_id, price, "MANUAL")
    return {"status": "closed"}

@app.get("/", response_class=HTMLResponse)
def dashboard():
    with open("../frontend/index.html", encoding="utf-8") as f:
        return f.read()
