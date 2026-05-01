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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "7775433175:AAFISLH8L375CqiqGih2BU41uK2wgxfuye4")
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


def _analyze_trade_outcome(trade: dict, result: str, duration_mins: float) -> str:
    """Generate a plain-English explanation of why a trade won or lost."""
    ind = {}
    try:
        raw = trade.get("indicators") or "{}"
        ind = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        pass

    direction = trade.get("direction", "LONG")
    confidence = trade.get("confidence", 0) or 0

    if result == "WIN":
        drivers = []
        fr = ind.get("funding_rate_pct", 0) or 0
        if abs(fr) > 0.05:
            drivers.append(f"extreme funding ({fr:+.4f}%) — crowded side unwound")
        tvb = ind.get("taker_vs_baseline", 1.0) or 1.0
        if tvb > 1.4 and direction == "LONG":
            drivers.append(f"taker squeeze ({tvb:.1f}x baseline) confirmed buy pressure")
        elif tvb < 0.7 and direction == "SHORT":
            drivers.append(f"heavy sell takers ({tvb:.1f}x baseline) confirmed sell pressure")
        cvd = ind.get("cvd_signal", "")
        if "confirm" in cvd and "bullish" in cvd and direction == "LONG":
            drivers.append("CVD confirmed real accumulation")
        elif "confirm" in cvd and "bearish" in cvd and direction == "SHORT":
            drivers.append("CVD confirmed real distribution")
        elif "divergence" in cvd:
            drivers.append(f"CVD divergence called the reversal correctly")
        ls = ind.get("ls_momentum", 0) or 0
        if abs(ls) > 3:
            drivers.append(f"L/S ratio flipped {ls:+.1f}% in our favour")
        if drivers:
            return "Why it won: " + " · ".join(drivers)
        return "Why it won: all signals aligned and price followed through."

    else:  # LOSS
        if duration_mins < 20:
            return (
                f"Why it lost: stopped out fast ({duration_mins:.0f}m) — "
                f"confidence was {confidence:.0f}/100, likely entered choppy conditions."
            )
        warnings = []
        tvb = ind.get("taker_vs_baseline", 1.0) or 1.0
        if tvb < 0.8 and direction == "LONG":
            warnings.append(f"taker sell pressure at entry ({tvb:.1f}x baseline) — should have been a red flag")
        elif tvb > 1.2 and direction == "SHORT":
            warnings.append(f"taker buy pressure at entry ({tvb:.1f}x baseline) — counter-signal was present")
        cvd = ind.get("cvd_signal", "")
        if ("bearish" in cvd) and direction == "LONG":
            warnings.append("CVD was bearish/distributing at entry — divergence ignored")
        elif ("bullish" in cvd) and direction == "SHORT":
            warnings.append("CVD was bullish/accumulating at entry — divergence ignored")
        htf = ind.get("htf_trend", "")
        if htf == "bearish" and direction == "LONG":
            warnings.append("entered long against a 4H downtrend")
        elif htf == "bullish" and direction == "SHORT":
            warnings.append("entered short against a 4H uptrend")
        rsi = ind.get("rsi", 50) or 50
        if rsi > 55 and direction == "LONG":
            warnings.append(f"RSI was already elevated ({rsi:.0f}) at long entry")
        elif rsi < 45 and direction == "SHORT":
            warnings.append(f"RSI was already low ({rsi:.0f}) at short entry")
        if warnings:
            return "Why it lost: " + " · ".join(warnings)
        return f"Why it lost: setup was valid, market reversed after {duration_mins:.0f}m — no clear warning signs at entry."


def should_open_trade(signal, symbol: str, config: dict,
                      strategy_version: str = "v2_sr") -> tuple[bool, str]:
    if signal.direction == "NEUTRAL":
        return False, "No signal"
    min_conf = 65 if strategy_version == "v3_ema" else config.get("min_confidence", 75)
    if signal.confidence < min_conf:
        return False, f"Confidence too low ({signal.confidence} < {min_conf})"
    all_open   = db.get_open_trades(symbol)
    strat_open = [t for t in all_open if t.get("strategy_version") == strategy_version]
    if len(strat_open) >= MAX_OPEN_TRADES:
        return False, f"Max open {strategy_version} trades reached ({MAX_OPEN_TRADES})"
    same_dir = [t for t in strat_open if t["direction"] == signal.direction]
    if same_dir:
        return False, f"Already have open {signal.direction} {strategy_version} trade"
    if db.had_recent_sl(symbol, signal.direction, minutes=30):
        return False, f"SL cooldown active — skipping {signal.direction} for 30 min"
    return True, "OK"

def open_paper_trade(signal, symbol: str, config: dict,
                     strategy_version: str = 'v2_sr') -> dict | None:
    should, reason = should_open_trade(signal, symbol, config, strategy_version=strategy_version)
    if not should:
        logger.info(f"Skipping trade: {reason}")
        return None

    position_size = config.get("position_size_usd", 100)
    trade_id = db.open_trade(signal, symbol, position_size, strategy_version=strategy_version)

    sl_pct   = signal.indicators.get('sl_pct', config.get('sl_pct', 1.2))
    tp_pct   = signal.indicators.get('tp_pct', config.get('tp_pct', 3.0))
    level    = signal.indicators.get('level_price', 0)
    lvl_type = signal.indicators.get('level_type', '')
    ob_imb   = signal.indicators.get('ob_imbalance', 0)
    reasons_text = " | ".join(signal.reasons[:4])
    logger.info(f'Opened {signal.direction} #{trade_id} @ {signal.entry_price} TP={signal.tp_price} SL={signal.sl_price} conf={signal.confidence}')
    strat_label = "SR Bot" if strategy_version == "v2_sr" else "EMA Bot"
    emoji = '🟢' if signal.direction == 'LONG' else '🔴'
    send_tg(
        f"{emoji} <b>{strat_label} [{strategy_version}] - {signal.direction} OPENED</b>\n\n"
        f"<b>{symbol}</b> @ ${signal.entry_price:,.4f}\n"
        f"Level: {lvl_type} @ {level:.4f}\n"
        f"TP: ${signal.tp_price:,.4f} (+{tp_pct:.2f}%)  |  SL: ${signal.sl_price:,.4f} (-{sl_pct:.2f}%)\n"
        f"Confidence: {signal.confidence:.0f}/100  |  OB: {ob_imb:+.2f}\n\n"
        f"<b>Setup:</b>\n{reasons_text}"
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

            outcome_analysis = _analyze_trade_outcome(trade, result, (datetime.utcnow() - datetime.fromisoformat(trade["opened_at"])).total_seconds() / 60)
            send_tg(
                f"{emoji} <b>OI Bot — {trade['direction']} CLOSED — {result}</b>\n\n"
                f"<b>{symbol}</b>\n"
                f"📥 Entry: ${trade['entry_price']:,.2f}  →  📤 Exit: ${exit_price:,.2f}\n"
                f"💵 PnL: <b>${pnl_usd:+.2f}</b> ({pnl_pct:+.2f}%)  |  ⏱ Held: {duration}\n"
                f"4H trend at entry: {htf_trend}\n\n"
                f"<b>Entry reasons:</b>\n{reasons_text}\n\n"
                f"<b>{outcome_analysis}</b>"
            )

            closed.append({"trade_id": trade["id"], "reason": reason, "pnl_pct": pnl_pct})

    return closed
