"""
OI-Based Futures Strategy
-------------------------
Signal generation logic combining:
1. OI momentum (rate of change)
2. Funding rate extremes
3. RSI (momentum confirmation)
4. Long/Short ratio divergence
5. 4H HTF trend filter (NEW) — only trade in direction of higher timeframe trend
6. Min 2-signal confluence (NEW) — require at least 2 distinct signals agreeing

Entry conditions:
  LONG:  4H bullish + OI/funding/RSI/LS at least 2 signals pointing long + score >= 40
  SHORT: 4H bearish + OI/funding/RSI/LS at least 2 signals pointing short + score >= 40

TP: 2.0% | SL: 0.8% | R:R: 2.5:1
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
    recent_deltas = deltas[-(period):]
    avg_gain = sum(d for d in recent_deltas if d > 0) / period
    avg_loss = sum(-d for d in recent_deltas if d < 0) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_ema(data: list[float], period: int) -> float:
    """Compute EMA for a list of closing prices, return final value."""
    if len(data) < period:
        return data[-1] if data else 0
    k = 2 / (period + 1)
    ema = data[0]
    for v in data[1:]:
        ema = v * k + ema * (1 - k)
    return ema


def calculate_oi_momentum(oi_history: list[dict], lookback: int = 10) -> dict:
    if not oi_history or len(oi_history) < lookback:
        return {"roc": 0, "trend": "flat", "acceleration": 0}
    recent = [x["oi"] for x in oi_history[-lookback:]]
    roc = (recent[-1] - recent[0]) / recent[0] * 100
    mid = len(recent) // 2
    first_half_roc = (recent[mid] - recent[0]) / recent[0] * 100
    second_half_roc = (recent[-1] - recent[mid]) / recent[mid] * 100
    acceleration = second_half_roc - first_half_roc
    trend = "rising" if roc > 0.3 else "falling" if roc < -0.3 else "flat"
    return {"roc": roc, "trend": trend, "acceleration": acceleration}


def calculate_funding_signal(funding_rate: float) -> dict:
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
    if not ls_history or len(ls_history) < lookback:
        return {"signal": "neutral", "ratio": 1.0, "momentum": 0}
    recent = [x["long_short_ratio"] for x in ls_history[-lookback:]]
    current_ratio = recent[-1]
    momentum = (recent[-1] - recent[0]) / recent[0] * 100
    if momentum > 3:
        signal = "bullish"
    elif momentum < -3:
        signal = "bearish"
    else:
        signal = "neutral"
    return {"signal": signal, "ratio": current_ratio, "momentum": momentum}


def get_htf_trend(htf_klines: list[dict]) -> dict:
    """Compute 4H trend using EMA50 vs EMA200."""
    if not htf_klines or len(htf_klines) < 50:
        return {"trend": "neutral", "ema50": 0, "ema200": 0}
    closes = [k["close"] for k in htf_klines]
    ema50  = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200) if len(closes) >= 200 else calc_ema(closes, len(closes))
    trend = "bullish" if ema50 > ema200 else "bearish"
    return {"trend": trend, "ema50": round(ema50, 2), "ema200": round(ema200, 2)}


def generate_signal(market_data: dict, config: dict) -> Signal:
    """
    Main signal generator. Returns a Signal object.
    Includes 4H HTF trend filter and minimum 2-signal confluence requirement.
    """
    tp_pct = config.get("tp_pct", 2.0)   # Updated: 2.0% TP for better R:R
    sl_pct = config.get("sl_pct", 0.8)

    klines      = market_data.get("klines") or []
    htf_klines  = market_data.get("htf_klines") or []
    oi_history  = market_data.get("oi_history") or []
    funding     = market_data.get("funding") or {}
    ls_history  = market_data.get("ls_ratio") or []
    ticker      = market_data.get("ticker") or {}

    closes = [k["close"] for k in klines] if klines else []
    current_price = ticker.get("price") or (closes[-1] if closes else 0)

    if not current_price:
        return Signal("NEUTRAL", 0, 0, 0, 0, ["No price data"], {})

    # --- HTF Trend (4H EMA50 vs EMA200) ---
    htf = get_htf_trend(htf_klines)
    htf_trend = htf["trend"]   # "bullish" | "bearish" | "neutral"

    # --- Compute indicators ---
    rsi        = calculate_rsi(closes) if closes else 50.0
    oi_mom     = calculate_oi_momentum(oi_history)
    funding_sig = calculate_funding_signal(funding.get("funding_rate", 0))
    ls_sig     = calculate_ls_signal(ls_history)

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
        "price_change_24h": ticker.get("price_change_pct", 0),
        "htf_trend": htf_trend,
        "htf_ema50": htf["ema50"],
        "htf_ema200": htf["ema200"],
    }

    # --- Directional scoring ---
    long_score  = 0
    short_score = 0
    long_signals  = 0   # count of distinct signals pointing LONG
    short_signals = 0   # count of distinct signals pointing SHORT
    reasons = []

    # 1. HTF trend context (informational — does NOT add score, but gates the trade later)
    reasons.append(f"4H trend: {htf_trend.upper()} (EMA50={htf['ema50']:.0f} vs EMA200={htf['ema200']:.0f})")

    # 2. OI momentum
    oi_rising  = oi_mom["trend"] == "rising"  and oi_mom["roc"] > 0.3
    oi_falling = oi_mom["trend"] == "falling" and oi_mom["roc"] < -0.3
    price_chg  = ticker.get("price_change_pct", 0)

    if oi_rising:
        reasons.append(f"OI rising +{oi_mom['roc']:.2f}% — new money entering")
        if price_chg > 0:
            long_score  += 20; long_signals  += 1
        else:
            short_score += 20; short_signals += 1
    elif oi_falling:
        reasons.append(f"OI falling {oi_mom['roc']:.2f}% — positions unwinding")
        if price_chg > 0:
            short_score += 15; short_signals += 1
        else:
            long_score  += 15; long_signals  += 1

    if oi_mom["acceleration"] > 0.5:
        reasons.append("OI accelerating — momentum building")
        if long_score >= short_score:
            long_score  += 10
        else:
            short_score += 10

    # 3. Funding rate
    if funding_sig["signal"] in ("bullish_extreme", "bullish_mild"):
        reasons.append(f"Funding negative ({funding_sig['rate_pct']:.4f}%) — shorts paying, bullish")
        long_score  += abs(funding_sig["score"]) + 10
        long_signals += 1
    elif funding_sig["signal"] in ("bearish_extreme", "bearish_mild"):
        reasons.append(f"Funding positive ({funding_sig['rate_pct']:.4f}%) — crowded longs, bearish")
        short_score  += abs(funding_sig["score"]) + 10
        short_signals += 1
    else:
        reasons.append(f"Funding neutral ({funding_sig['rate_pct']:.4f}%)")
        long_score += 5   # slight bullish bias on neutral funding

    # 4. RSI
    if rsi < 30:
        reasons.append(f"RSI oversold ({rsi:.1f}) — strong bullish")
        long_score += 30; long_signals += 1
    elif rsi < 40:
        reasons.append(f"RSI low ({rsi:.1f}) — bullish")
        long_score += 20; long_signals += 1
    elif rsi < 50:
        reasons.append(f"RSI below midline ({rsi:.1f}) — mild bullish")
        long_score += 10
    elif rsi > 70:
        reasons.append(f"RSI overbought ({rsi:.1f}) — strong bearish")
        short_score += 30; short_signals += 1
    elif rsi > 60:
        reasons.append(f"RSI elevated ({rsi:.1f}) — bearish")
        short_score += 20; short_signals += 1
    elif rsi > 50:
        reasons.append(f"RSI above midline ({rsi:.1f}) — mild bearish")
        short_score += 10

    # 5. Long/Short ratio
    if ls_sig["signal"] == "bullish":
        reasons.append(f"LS ratio flipping bullish (+{ls_sig['momentum']:.1f}%)")
        long_score  += 20; long_signals  += 1
    elif ls_sig["signal"] == "bearish":
        reasons.append(f"LS ratio flipping bearish ({ls_sig['momentum']:.1f}%)")
        short_score += 20; short_signals += 1
    else:
        reasons.append(f"LS ratio neutral ({ls_sig['ratio']:.2f})")

    # --- Determine direction ---
    # Rules:
    # 1. Score must reach min_confidence threshold
    # 2. Must have at least 2 distinct signals agreeing (not just 1 strong RSI alone)
    # 3. 4H trend must not be against the trade direction
    min_score = config.get("min_confidence", 40)
    MIN_SIGNALS = 2   # require at least 2 distinct indicators pointing same way

    long_ok  = (long_score > short_score
                and long_score >= min_score
                and long_signals >= MIN_SIGNALS
                and htf_trend != "bearish")   # don't long in 4H downtrend

    short_ok = (short_score > long_score
                and short_score >= min_score
                and short_signals >= MIN_SIGNALS
                and htf_trend != "bullish")   # don't short in 4H uptrend

    if long_ok:
        direction  = "LONG"
        confidence = min(long_score, 100)
        tp_price   = round(current_price * (1 + tp_pct / 100), 2)
        sl_price   = round(current_price * (1 - sl_pct / 100), 2)
    elif short_ok:
        direction  = "SHORT"
        confidence = min(short_score, 100)
        tp_price   = round(current_price * (1 - tp_pct / 100), 2)
        sl_price   = round(current_price * (1 + sl_pct / 100), 2)
    else:
        direction  = "NEUTRAL"
        confidence = max(long_score, short_score)
        tp_price   = 0
        sl_price   = 0
        # Add reason for being neutral
        blocks = []
        if htf_trend == "bearish" and long_score > short_score:
            blocks.append("4H downtrend blocks LONG")
        if htf_trend == "bullish" and short_score > long_score:
            blocks.append("4H uptrend blocks SHORT")
        if long_signals < MIN_SIGNALS and long_score > short_score:
            blocks.append(f"only {long_signals}/2 signals agreeing for LONG")
        if short_signals < MIN_SIGNALS and short_score > long_score:
            blocks.append(f"only {short_signals}/2 signals agreeing for SHORT")
        if blocks:
            reasons.append("Blocked: " + " | ".join(blocks))
        elif not reasons:
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
