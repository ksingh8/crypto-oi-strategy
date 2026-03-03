"""
OI-Based Futures Strategy
-------------------------
Signal generation logic combining:
1. OI momentum (rate of change)
2. Funding rate extremes
3. RSI (momentum confirmation)
4. Long/Short ratio divergence
5. Liquidation cascade detection

Entry conditions:
  LONG:  OI rising + funding negative/neutral + RSI < 55 + LS ratio flipping bullish
  SHORT: OI rising + funding strongly positive + RSI > 45 + LS ratio flipping bearish

TP: 1.5% default | SL: 0.8% default (configurable)
"""

import statistics
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

@dataclass
class Signal:
    direction: str          # "LONG" | "SHORT" | "NEUTRAL"
    confidence: float       # 0-100
    entry_price: float
    tp_price: float
    sl_price: float
    reasons: list[str]
    indicators: dict
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()


def calculate_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    
    # Use last `period` values
    recent_deltas = deltas[-(period):]
    avg_gain = sum(d for d in recent_deltas if d > 0) / period
    avg_loss = sum(-d for d in recent_deltas if d < 0) / period
    
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_oi_momentum(oi_history: list[dict], lookback: int = 10) -> dict:
    """OI rate of change and trend"""
    if not oi_history or len(oi_history) < lookback:
        return {"roc": 0, "trend": "flat", "acceleration": 0}
    
    recent = [x["oi"] for x in oi_history[-lookback:]]
    roc = (recent[-1] - recent[0]) / recent[0] * 100  # % change
    
    # OI acceleration (is it speeding up?)
    mid = len(recent) // 2
    first_half_roc = (recent[mid] - recent[0]) / recent[0] * 100
    second_half_roc = (recent[-1] - recent[mid]) / recent[mid] * 100
    acceleration = second_half_roc - first_half_roc
    
    trend = "rising" if roc > 0.3 else "falling" if roc < -0.3 else "flat"
    return {"roc": roc, "trend": trend, "acceleration": acceleration}


def calculate_funding_signal(funding_rate: float) -> dict:
    """
    Funding > 0.05% = longs paying shorts = crowded long = bearish signal
    Funding < -0.02% = shorts paying longs = crowded short = bullish signal
    """
    rate_pct = funding_rate * 100
    if rate_pct > 0.08:
        signal = "bearish_extreme"
        score = min((rate_pct - 0.05) / 0.05 * 50, 50)
    elif rate_pct > 0.03:
        signal = "bearish_mild"
        score = 20
    elif rate_pct < -0.03:
        signal = "bullish_extreme"
        score = -min((abs(rate_pct) - 0.02) / 0.03 * 50, 50)
    elif rate_pct < -0.01:
        signal = "bullish_mild"
        score = -20
    else:
        signal = "neutral"
        score = 0
    return {"signal": signal, "score": score, "rate_pct": rate_pct}


def calculate_ls_signal(ls_history: list[dict], lookback: int = 6) -> dict:
    """Detect LS ratio momentum flip"""
    if not ls_history or len(ls_history) < lookback:
        return {"signal": "neutral", "ratio": 1.0, "momentum": 0}
    
    recent = [x["long_short_ratio"] for x in ls_history[-lookback:]]
    current = recent[-1]
    prev_avg = statistics.mean(recent[:-2])
    momentum = (current - prev_avg) / prev_avg * 100
    
    if momentum > 2:
        signal = "bullish"  # ratio increasing = more longs
    elif momentum < -2:
        signal = "bearish"
    else:
        signal = "neutral"
    
    return {"signal": signal, "ratio": current, "momentum": momentum}


def generate_signal(market_data: dict, config: dict) -> Signal:
    """
    Main signal generator. Returns a Signal object.
    """
    tp_pct = config.get("tp_pct", 1.5)
    sl_pct = config.get("sl_pct", 0.8)
    
    klines = market_data.get("klines") or []
    oi_history = market_data.get("oi_history") or []
    funding = market_data.get("funding") or {}
    ls_history = market_data.get("ls_ratio") or []
    ticker = market_data.get("ticker") or {}

    closes = [k["close"] for k in klines] if klines else []
    current_price = ticker.get("price") or (closes[-1] if closes else 0)

    if not current_price:
        return Signal("NEUTRAL", 0, 0, 0, 0, ["No price data"], {})

    # --- Compute indicators ---
    rsi = calculate_rsi(closes) if closes else 50.0
    oi_mom = calculate_oi_momentum(oi_history)
    funding_sig = calculate_funding_signal(funding.get("funding_rate", 0))
    ls_sig = calculate_ls_signal(ls_history)

    indicators = {
        "rsi": round(rsi, 2),
        "oi_roc_pct": round(oi_mom["roc"], 3),
        "oi_trend": oi_mom["trend"],
        "oi_acceleration": round(oi_mom["acceleration"], 3),
        "funding_rate_pct": round(funding_sig["rate_pct"], 4),
        "funding_signal": funding_sig["signal"],
        "ls_ratio": round(ls_sig["ratio"], 3),
        "ls_momentum": round(ls_sig["momentum"], 2),
        "current_price": current_price,
        "price_change_24h": ticker.get("price_change_pct", 0)
    }

    # --- Score system ---
    long_score = 0
    short_score = 0
    reasons = []

    # OI momentum (rising OI = conviction, direction depends on other signals)
    if oi_mom["trend"] == "rising" and oi_mom["roc"] > 0.5:
        reasons.append(f"OI rising +{oi_mom['roc']:.2f}% — new money entering")
        long_score += 15
        short_score += 15  # OI alone is not directional

    if oi_mom["acceleration"] > 0.3:
        reasons.append(f"OI accelerating — momentum building")
        long_score += 10
        short_score += 10

    # Funding rate (directional)
    if funding_sig["signal"] in ("bullish_extreme", "bullish_mild"):
        reasons.append(f"Funding negative ({funding_sig['rate_pct']:.4f}%) — shorts paying longs, bullish")
        long_score += abs(funding_sig["score"])
    elif funding_sig["signal"] in ("bearish_extreme", "bearish_mild"):
        reasons.append(f"Funding positive ({funding_sig['rate_pct']:.4f}%) — crowded longs, bearish")
        short_score += abs(funding_sig["score"])
    else:
        reasons.append(f"Funding neutral ({funding_sig['rate_pct']:.4f}%)")

    # RSI
    if rsi < 35:
        reasons.append(f"RSI oversold ({rsi:.1f}) — bullish")
        long_score += 25
    elif rsi < 45:
        reasons.append(f"RSI low ({rsi:.1f}) — mild bullish")
        long_score += 12
    elif rsi > 65:
        reasons.append(f"RSI overbought ({rsi:.1f}) — bearish")
        short_score += 25
    elif rsi > 55:
        reasons.append(f"RSI elevated ({rsi:.1f}) — mild bearish")
        short_score += 12

    # Long/Short ratio
    if ls_sig["signal"] == "bullish":
        reasons.append(f"LS ratio flipping bullish (momentum: +{ls_sig['momentum']:.1f}%)")
        long_score += 15
    elif ls_sig["signal"] == "bearish":
        reasons.append(f"LS ratio flipping bearish (momentum: {ls_sig['momentum']:.1f}%)")
        short_score += 15

    # --- Determine direction ---
    min_score = config.get("min_confidence", 40)
    
    if long_score > short_score and long_score >= min_score:
        direction = "LONG"
        confidence = min(long_score, 100)
        tp_price = round(current_price * (1 + tp_pct / 100), 2)
        sl_price = round(current_price * (1 - sl_pct / 100), 2)
    elif short_score > long_score and short_score >= min_score:
        direction = "SHORT"
        confidence = min(short_score, 100)
        tp_price = round(current_price * (1 - tp_pct / 100), 2)
        sl_price = round(current_price * (1 + sl_pct / 100), 2)
    else:
        direction = "NEUTRAL"
        confidence = 0
        tp_price = 0
        sl_price = 0
        if not reasons:
            reasons.append("No clear signal — conditions mixed")

    return Signal(
        direction=direction,
        confidence=confidence,
        entry_price=current_price,
        tp_price=tp_price,
        sl_price=sl_price,
        reasons=reasons,
        indicators=indicators
    )
