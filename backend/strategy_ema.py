"""
EMA Pullback Scalp Strategy — v3_ema
--------------------------------------
Concept: In a trending market, price pulls back to the 5m EMA21 and
bounces. Enter on the confirmation bar (closes back in trend direction)
with taker flow confirming. 15m EMA9/EMA21 defines the trend direction.

Entry conditions:
  1. 15m trend: EMA9 > EMA21 = bullish | EMA9 < EMA21 = bearish
  2. 5m price touches EMA21 (within 0.4% or bar wicks through it)
  3. Signal bar closes in trend direction (green=LONG, red=SHORT)
  4. Taker ratio confirms (>= 1.1 LONG | <= 0.9 SHORT)

SL: signal bar low (LONG) or high (SHORT) + 0.1% buffer
TP: SL distance x 1.5 (1.5:1 R:R — backtest showed this is the natural move size)
Min SL: 0.25% | Max SL: 1.5%
"""

from dataclasses import dataclass
from typing import Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


def _ema(values: list, period: int) -> list:
    if not values:
        return []
    k = 2.0 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def get_15m_trend(klines_15m: list) -> dict:
    """Current 15m trend from EMA9 vs EMA21."""
    if not klines_15m or len(klines_15m) < 21:
        return {"trend": "neutral", "ema9": 0.0, "ema21": 0.0}
    closes = [k["close"] for k in klines_15m]
    ema9   = _ema(closes, 9)
    ema21  = _ema(closes, 21)
    trend  = "bullish" if ema9[-1] > ema21[-1] else "bearish"
    return {"trend": trend, "ema9": round(ema9[-1], 4), "ema21": round(ema21[-1], 4)}


def detect_ema_pullback(klines_5m: list, trend: str, pullback_tol: float = 0.4) -> dict:
    """
    Look back up to 2 bars for a bar that:
      - Touched the 5m EMA21 (wick or close within pullback_tol%)
      - Closed in the trend direction (green=LONG, red=SHORT)
    Returns the signal bar and EMA values if found.
    """
    empty = {"found": False}
    if not klines_5m or len(klines_5m) < 25:
        return empty

    closes = [k["close"] for k in klines_5m]
    ema21  = _ema(closes, 21)

    # Check the last 2 completed bars (not current forming bar)
    for lookback in [2, 3]:
        if len(klines_5m) < lookback + 1:
            continue
        idx   = len(klines_5m) - lookback
        bar   = klines_5m[idx]
        e21   = ema21[idx]
        price = bar["close"]

        # Did the bar touch EMA21?
        dist      = abs(price - e21) / e21 * 100
        bar_wicks = bar["low"] <= e21 <= bar["high"]
        at_ema    = dist <= pullback_tol or bar_wicks

        if not at_ema:
            continue

        # Did it close in the right direction?
        green = price > bar["open"]
        red   = price < bar["open"]

        if trend == "bullish" and green and price >= e21 * 0.999:
            return {
                "found":     True,
                "direction": "LONG",
                "bar":       bar,
                "e21":       round(e21, 4),
                "sl_ref":    bar["low"],
                "lookback":  lookback,
            }
        elif trend == "bearish" and red and price <= e21 * 1.001:
            return {
                "found":     True,
                "direction": "SHORT",
                "bar":       bar,
                "e21":       round(e21, 4),
                "sl_ref":    bar["high"],
                "lookback":  lookback,
            }

    return empty


def generate_ema_signal(market_data: dict, config: dict):
    """
    EMA pullback scalp signal (v3_ema).
    Returns a Signal-compatible object.
    """
    # Import here to avoid circular at module level
    from strategy import Signal

    klines_5m   = market_data.get("klines")      or []
    klines_15m  = market_data.get("klines_15m")  or []
    taker_history = market_data.get("taker_ratio") or []
    ticker      = market_data.get("ticker")       or {}

    closes_5m     = [k["close"] for k in klines_5m]
    current_price = ticker.get("price") or (closes_5m[-1] if closes_5m else 0)

    if not current_price:
        return Signal("NEUTRAL", 0, 0, 0, 0, ["No price data"], {})

    reasons    = []
    indicators = {"current_price": current_price, "strategy": "v3_ema"}

    # ── 1. 15m trend ──────────────────────────────────────────────────────────
    trend_info = get_15m_trend(klines_15m)
    trend      = trend_info["trend"]
    indicators.update({"ema_trend": trend, "ema9_15m": trend_info["ema9"],
                        "ema21_15m": trend_info["ema21"]})

    if trend == "neutral":
        return Signal("NEUTRAL", 0, current_price, 0, 0,
                      ["15m trend neutral — insufficient klines"], indicators)

    # ── 2. EMA21 pullback on 5m ───────────────────────────────────────────────
    pullback = detect_ema_pullback(klines_5m, trend)
    if not pullback["found"]:
        reasons.append(f"15m trend: {trend.upper()} — waiting for 5m EMA21 pullback")
        return Signal("NEUTRAL", 0, current_price, 0, 0, reasons, indicators)

    direction = pullback["direction"]
    e21       = pullback["e21"]
    indicators.update({"ema21_5m": e21, "pullback_bars_ago": pullback["lookback"]})
    reasons.append(f"15m {trend.upper()} trend | 5m EMA21 pullback @ {e21} "
                   f"({pullback['lookback']} bars ago), closed in trend direction")

    # ── 3. Taker confirmation ─────────────────────────────────────────────────
    taker_ratio = taker_history[-1].get("buy_sell_ratio", 1.0) if taker_history else 1.0
    indicators["taker_ratio"] = taker_ratio

    if direction == "LONG" and taker_ratio < 1.1:
        reasons.append(f"Taker not confirming LONG: {taker_ratio:.2f}x (need >= 1.1)")
        return Signal("NEUTRAL", 0, current_price, 0, 0, reasons, indicators)
    if direction == "SHORT" and taker_ratio > 0.9:
        reasons.append(f"Taker not confirming SHORT: {taker_ratio:.2f}x (need <= 0.9)")
        return Signal("NEUTRAL", 0, current_price, 0, 0, reasons, indicators)

    reasons.append(f"Taker confirms: {taker_ratio:.2f}x {'(buyers active)' if direction == 'LONG' else '(sellers active)'}")

    # ── 4. SL / TP ────────────────────────────────────────────────────────────
    RR         = 1.5
    BUFFER     = 0.001   # 0.1%
    MIN_SL_PCT = 0.25
    MAX_SL_PCT = 1.5

    if direction == "LONG":
        sl_raw  = pullback["sl_ref"] * (1 - BUFFER)
        sl_dist = (current_price - sl_raw) / current_price * 100
    else:
        sl_raw  = pullback["sl_ref"] * (1 + BUFFER)
        sl_dist = (sl_raw - current_price) / current_price * 100

    if sl_dist < MIN_SL_PCT:
        reasons.append(f"SL too tight: {sl_dist:.2f}% < {MIN_SL_PCT}% — bar too small")
        return Signal("NEUTRAL", 0, current_price, 0, 0, reasons, indicators)
    if sl_dist > MAX_SL_PCT:
        sl_dist = MAX_SL_PCT
        sl_raw  = (current_price * (1 - MAX_SL_PCT/100) if direction == "LONG"
                   else current_price * (1 + MAX_SL_PCT/100))

    sl_price = round(sl_raw, 4)
    tp_dist  = sl_dist * RR

    if direction == "LONG":
        tp_price = round(current_price * (1 + tp_dist / 100), 4)
    else:
        tp_price = round(current_price * (1 - tp_dist / 100), 4)

    indicators.update({"sl_pct": round(sl_dist, 3), "tp_pct": round(tp_dist, 3), "rr_ratio": RR})
    reasons.append(f"SL @ {sl_price} ({sl_dist:.2f}%) | TP @ {tp_price} (+{tp_dist:.2f}%, {RR}:1 R:R)")

    # ── 5. Confidence score ───────────────────────────────────────────────────
    score = 65  # base: trend + pullback detected
    if direction == "LONG"  and taker_ratio >= 1.3: score += 20
    elif direction == "SHORT" and taker_ratio <= 0.75: score += 20
    elif direction == "LONG"  and taker_ratio >= 1.1: score += 10
    elif direction == "SHORT" and taker_ratio <= 0.9: score += 10
    if pullback["lookback"] == 2: score += 5   # fresher signal
    indicators["score"] = score

    return Signal(
        direction=direction,
        confidence=min(score, 100),
        entry_price=current_price,
        tp_price=tp_price,
        sl_price=sl_price,
        reasons=reasons,
        indicators=indicators,
    )
