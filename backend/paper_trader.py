"""
Paper Trader — simulates trade execution and monitors open positions for TP/SL hits.
"""
import logging
import json
import os
import requests
from datetime import datetime
import database as db

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "7775433175:AAGOfkg9fTskJW6e5YD2RPLGhuz0r8eJdtk")
CHAT_ID        = os.getenv("TELEGRAM_CHAT",  "1279109702")

MAX_OPEN_TRADES = 1


def send_tg(msg: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        logger.warning(f"Telegram error: {e}")


def _format_duration(opened_at_str: str) -> str:
    try:
        opened_at = datetime.fromisoformat(opened_at_str)
        mins = int((datetime.utcnow() - opened_at).total_seconds() / 60)
        if mins >= 60:
            return f"{mins // 60}h {mins % 60}m"
        return f"{mins}m"
    except Exception:
        return "?"


def should_open_trade(signal, symbol: str, config: dict) -> tuple[bool, str]:
    if signal.direction == "NEUTRAL":
        return False, "No signal"
    if signal.confidence < config.get("min_confidence", 40):
        return False, f"Confidence too low ({signal.confidence})"
    open_trades = db.get_open_trades(symbol)
    if len(open_trades) >= MAX_OPEN_TRADES:
        return False, f"Max open trades reached ({MAX_OPEN_TRADES})"
    same_dir = [t for t in open_trades if t["direction"] == signal.direction]
    if same_dir:
        return False, f"Already have open {signal.direction} trade"
    return True, "OK"


def open_paper_trade(signal, symbol: str, config: dict) -> dict | None:
    should, reason = should_open_trade(signal, symbol, config)
    if not should:
        logger.info(f"Skipping trade: {reason}")
        return None

    position_size = config.get("position_size_usd", 100)
    trade_id = db.open_trade(signal, symbol, position_size)

    tp_pct = config.get("tp_pct", 2.0)
    sl_pct = config.get("sl_pct", 0.8)
    htf    = signal.indicators.get("htf_trend", "?").upper()

    # Format entry reasons (skip the HTF line since we show it separately)
    entry_reasons = [r for r in signal.reasons if "4H trend" not in r]
    reasons_text  = "\n".join(f"  • {r}" for r in entry_reasons[:4])

    logger.info(f"Opened {signal.direction} #{trade_id} @ {signal.entry_price} "
                f"TP={signal.tp_price} SL={signal.sl_price} conf={signal.confidence} HTF={htf}")

    send_tg(
        f"{'🟢' if signal.direction == 'LONG' else '🔴'} <b>OI Bot — {signal.direction} OPENED</b>\n\n"
        f"<b>{symbol}</b> @ ${signal.entry_price:,.2f}\n"
        f"🎯 TP: ${signal.tp_price:,.2f} (+{tp_pct}%)  |  🛑 SL: ${signal.sl_price:,.2f} (-{sl_pct}%)\n"
        f"📊 Confidence: {signal.confidence:.0f}/100  |  4H: {htf}\n\n"
        f"<b>Why this trade:</b>\n{reasons_text}"
    )

    return {"trade_id": trade_id, "direction": signal.direction, "entry": signal.entry_price}


def check_open_positions(current_price: float, symbol: str, klines: list = None):
    """
    Check open trades for TP/SL hits using candle high/low for accuracy.
    """
    open_trades = db.get_open_trades(symbol)
    if not open_trades:
        return []

    closed = []

    candle_highs = {}
    candle_lows  = {}
    if klines:
        for trade in open_trades:
            entry_ts = int(datetime.fromisoformat(trade["opened_at"]).timestamp() * 1000)
            relevant = [c for c in klines if c["timestamp"] >= entry_ts]
            if relevant:
                candle_highs[trade["id"]] = max(c["high"] for c in relevant)
                candle_lows[trade["id"]]  = min(c["low"]  for c in relevant)

    for trade in open_trades:
        c_high = candle_highs.get(trade["id"], current_price)
        c_low  = candle_lows.get(trade["id"],  current_price)

        hit = None
        if trade["direction"] == "LONG":
            if c_low  <= trade["sl_price"]: hit = ("SL", trade["sl_price"])
            elif c_high >= trade["tp_price"]: hit = ("TP", trade["tp_price"])
        else:
            if c_high >= trade["sl_price"]: hit = ("SL", trade["sl_price"])
            elif c_low  <= trade["tp_price"]: hit = ("TP", trade["tp_price"])

        if hit:
            reason, exit_price = hit
            db.close_trade(trade["id"], exit_price, reason)

            is_long = trade["direction"] == "LONG"
            pnl_pct = ((exit_price - trade["entry_price"]) / trade["entry_price"] * 100
                       if is_long
                       else (trade["entry_price"] - exit_price) / trade["entry_price"] * 100)
            pnl_usd = round(trade.get("position_size_usd", 100) * pnl_pct / 100, 2)
            result  = "WIN" if reason == "TP" else "LOSS"
            emoji   = "✅" if result == "WIN" else "❌"

            duration = _format_duration(trade["opened_at"])

            # Retrieve stored entry reasons
            stored_reasons = []
            try:
                raw = trade.get("reasons") or "[]"
                stored_reasons = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                pass
            entry_reasons = [r for r in stored_reasons if "4H trend" not in r]
            reasons_text  = "\n".join(f"  • {r}" for r in entry_reasons[:3]) if entry_reasons else "  • (no reasons stored)"

            # Why it won/lost
            if result == "WIN":
                outcome_note = "Price moved in our favour and hit target."
            else:
                outcome_note = "Price reversed against our position and hit stop loss."

            htf = trade.get("indicators") or {}
            if isinstance(htf, str):
                try: htf = json.loads(htf)
                except: htf = {}
            htf_trend = htf.get("htf_trend", "?").upper()

            logger.info(f"Closed #{trade['id']} via {reason} @ {exit_price} | PnL: {pnl_pct:.2f}%")

            send_tg(
                f"{emoji} <b>OI Bot — {trade['direction']} CLOSED — {result}</b>\n\n"
                f"<b>{symbol}</b>\n"
                f"📥 Entry: ${trade['entry_price']:,.2f}  →  📤 Exit: ${exit_price:,.2f}\n"
                f"💵 PnL: <b>${pnl_usd:+.2f}</b> ({pnl_pct:+.2f}%)  |  ⏱ Held: {duration}\n"
                f"4H trend at entry: {htf_trend}\n\n"
                f"<b>Entry reasons:</b>\n{reasons_text}\n\n"
                f"<b>Outcome:</b> {outcome_note}"
            )

            closed.append({"trade_id": trade["id"], "reason": reason, "pnl_pct": pnl_pct})

    return closed
