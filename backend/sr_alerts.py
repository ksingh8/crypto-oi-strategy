"""
S/R Level Alert System — Alert-only, no auto-trade.

Monitors 4H and Daily S/R levels for all watched symbols.
Sends Telegram alert when price touches or retests a key level,
with a brief signal quality summary.

Config (set in main.py or env):
  TELEGRAM_BOT_TOKEN  — bot token from @BotFather
  TELEGRAM_CHAT_ID    — your chat or group ID
  SR_ALERT_SYMBOLS    — symbols to monitor (defaults to CONFIG["symbols"])
  SR_PROXIMITY_PCT    — how close price must be to trigger (default 0.3%)
  SR_COOLDOWN_MINUTES — min minutes between repeated alerts for same level (default 60)
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta

import httpx

import binance_client as bc
from strategy import find_sr_levels

logger = logging.getLogger(__name__)

# ── Telegram ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

async def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured — skipping alert")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       message,
                "parse_mode": "HTML",
            })
            r.raise_for_status()
            return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


# ── Alert cooldown tracker ─────────────────────────────────────────────────────
# key: (symbol, level_price_rounded) → last alert datetime
_last_alerted: dict[tuple, datetime] = {}

def _cooldown_key(symbol: str, level_price: float) -> tuple:
    # Round to 4 sig figs to avoid floating-point mismatches
    rounded = round(level_price, max(0, 4 - len(str(int(level_price)))))
    return (symbol, rounded)

def _is_on_cooldown(symbol: str, level_price: float, cooldown_minutes: int) -> bool:
    key = _cooldown_key(symbol, level_price)
    last = _last_alerted.get(key)
    if last and datetime.utcnow() - last < timedelta(minutes=cooldown_minutes):
        return True
    return False

def _mark_alerted(symbol: str, level_price: float):
    key = _cooldown_key(symbol, level_price)
    _last_alerted[key] = datetime.utcnow()


# ── Signal summary builder ─────────────────────────────────────────────────────
def _build_signal_summary(market_data: dict) -> str:
    """Extract key indicator readings and return a compact summary string."""
    lines = []

    # RSI from 5m klines
    klines = market_data.get("klines") or []
    if len(klines) >= 14:
        closes = [k["close"] for k in klines[-15:]]
        gains, losses = [], []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i - 1]
            (gains if delta > 0 else losses).append(abs(delta))
        avg_gain = sum(gains[-14:]) / 14 if gains else 0
        avg_loss = sum(losses[-14:]) / 14 if losses else 0.0001
        rsi = 100 - (100 / (1 + avg_gain / avg_loss))
        lines.append(f"RSI(14): <b>{rsi:.0f}</b> {'🔴 OS' if rsi < 35 else '🟢 OB' if rsi > 65 else '⚪ Neutral'}")

    # OI trend
    oi_history = market_data.get("oi_history") or []
    if len(oi_history) >= 3:
        oi_recent = oi_history[-1]["oi"]
        oi_prev   = oi_history[-4]["oi"] if len(oi_history) >= 4 else oi_history[0]["oi"]
        oi_chg    = (oi_recent - oi_prev) / oi_prev * 100 if oi_prev else 0
        oi_arrow  = "📈" if oi_chg > 0.5 else "📉" if oi_chg < -0.5 else "➡️"
        lines.append(f"OI: {oi_arrow} {oi_chg:+.1f}%")

    # Taker flow (CVD proxy)
    taker = market_data.get("taker_ratio") or []
    if taker:
        last_taker = taker[-1]
        ratio = last_taker.get("buy_sell_ratio", 1.0)
        flow = "🟢 Buy pressure" if ratio > 1.05 else "🔴 Sell pressure" if ratio < 0.95 else "⚪ Balanced"
        lines.append(f"Taker flow: {flow} ({ratio:.2f})")

    # Funding rate
    funding = market_data.get("funding") or {}
    fr = funding.get("funding_rate")
    if fr is not None:
        fr_pct = fr * 100
        lines.append(f"Funding: {fr_pct:+.4f}% {'⚠️ crowded LONG' if fr_pct > 0.05 else '⚠️ crowded SHORT' if fr_pct < -0.05 else '✅ neutral'}")

    return "\n".join(lines) if lines else "No indicator data available"


def _trade_bias(level_type: str, market_data: dict) -> str:
    """Return a one-line trade quality opinion."""
    klines = market_data.get("klines") or []
    taker  = market_data.get("taker_ratio") or []
    oi_history = market_data.get("oi_history") or []

    bullish_signals = 0
    bearish_signals = 0

    # RSI
    if len(klines) >= 15:
        closes = [k["close"] for k in klines[-15:]]
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i - 1]
            (gains if d > 0 else losses).append(abs(d))
        avg_gain = sum(gains[-14:]) / 14 if gains else 0
        avg_loss = sum(losses[-14:]) / 14 if losses else 0.0001
        rsi = 100 - (100 / (1 + avg_gain / avg_loss))
        if rsi < 40: bullish_signals += 1
        if rsi > 60: bearish_signals += 1

    # Taker flow
    if taker:
        ratio = taker[-1].get("buy_sell_ratio", 1.0)
        if ratio > 1.05: bullish_signals += 1
        if ratio < 0.95: bearish_signals += 1

    # OI rising = conviction
    if len(oi_history) >= 4:
        oi_chg = (oi_history[-1]["oi"] - oi_history[-4]["oi"]) / oi_history[-4]["oi"] * 100
        if oi_chg > 0.5:
            if level_type == "support":   bullish_signals += 1
            if level_type == "resistance": bearish_signals += 1

    if level_type == "support":
        if bullish_signals >= 2: return "⚡ <b>LONG setup looks favorable</b> — multiple bullish confirmations"
        if bearish_signals >= 2: return "⚠️ <b>Caution on LONG</b> — bearish flow at support (possible breakdown)"
        return "🔍 <b>Watch for LONG</b> — wait for sweep + reclaim confirmation"
    else:  # resistance
        if bearish_signals >= 2: return "⚡ <b>SHORT setup looks favorable</b> — multiple bearish confirmations"
        if bullish_signals >= 2: return "⚠️ <b>Caution on SHORT</b> — bullish flow at resistance (possible breakout)"
        return "🔍 <b>Watch for SHORT</b> — wait for sweep + reclaim confirmation"


# ── Main alert check ───────────────────────────────────────────────────────────
async def check_sr_alerts(
    symbol: str,
    market_data: dict,
    proximity_pct: float = 0.3,
    cooldown_minutes: int = 60,
):
    """
    Called once per strategy cycle for each symbol.
    Fetches 4H and Daily levels, checks proximity, fires Telegram alert if triggered.
    """
    ticker = market_data.get("ticker") or {}
    price  = ticker.get("price", 0)
    if not price:
        return

    # Fetch HTF klines (4H and Daily)
    try:
        klines_4h    = await bc.get_klines(symbol, "4h",  limit=100)
        klines_daily = await bc.get_klines(symbol, "1d",  limit=60)
    except Exception as e:
        logger.warning(f"[{symbol}] Failed to fetch HTF klines for SR alerts: {e}")
        return

    levels_4h    = find_sr_levels(klines_4h,    pivot_strength=3, max_levels=8)
    levels_daily = find_sr_levels(klines_daily, pivot_strength=3, max_levels=6)

    triggered = []
    for tf_label, levels in [("4H", levels_4h), ("Daily", levels_daily)]:
        for lvl in levels:
            lp   = lvl["price"]
            dist = abs(price - lp) / lp * 100
            if dist <= proximity_pct:
                if not _is_on_cooldown(symbol, lp, cooldown_minutes):
                    triggered.append((tf_label, lvl, dist))

    if not triggered:
        return

    for tf_label, lvl, dist in triggered:
        lp         = lvl["price"]
        ltype      = lvl["type"]        # "support" | "resistance"
        strength   = lvl["strength"]
        touch_word = "touching" if dist < 0.1 else "approaching"
        direction  = "🟩 SUPPORT" if ltype == "support" else "🟥 RESISTANCE"

        summary = _build_signal_summary(market_data)
        bias    = _trade_bias(ltype, market_data)

        msg = (
            f"📍 <b>S/R ALERT — {symbol}</b>\n"
            f"Price <b>{price:.4f}</b> is {touch_word} {direction}\n"
            f"Level: <b>{lp:.4f}</b> ({tf_label}, strength {strength}) | dist {dist:.2f}%\n"
            f"\n"
            f"<b>Indicators:</b>\n{summary}\n"
            f"\n"
            f"<b>Trade bias:</b>\n{bias}\n"
            f"\n"
            f"<i>⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC — alert-only, no auto-trade</i>"
        )

        sent = await send_telegram(msg)
        if sent:
            _mark_alerted(symbol, lp)
            logger.info(f"[{symbol}] SR alert sent: {ltype} @ {lp:.4f} ({tf_label})")
