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

TP: 3.0% | SL: 1.2% | R:R: 2.5:1
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
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    # Seed with simple average over first period
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Wilder's smoothing for remaining bars
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
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
    if not recent[0] or not recent[len(recent) // 2]:  # guard against zero OI values
        return {"roc": 0, "trend": "flat", "acceleration": 0}
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



def calculate_taker_pressure_signal(taker_history: list, lookback: int = 6) -> dict:
    """
    Detect aggressive taker pressure as a liquidation proxy.
    buySellRatio >> 1  → shorts being squeezed (bullish)
    buySellRatio << 1  → longs being liquidated (bearish)
    Compares recent bars to their own baseline to catch spikes.
    """
    if not taker_history or len(taker_history) < 3:
        return {"signal": "neutral", "ratio": 1.0, "ratio_vs_baseline": 1.0,
                "buy_vol": 0, "sell_vol": 0, "score": 0}

    recent  = taker_history[-lookback:]
    current = recent[-1]
    baseline_ratios = [b["buy_sell_ratio"] for b in taker_history[:-lookback]] or [1.0]
    baseline_avg = sum(baseline_ratios) / len(baseline_ratios)

    ratio = current["buy_sell_ratio"]
    ratio_vs_baseline = ratio / baseline_avg if baseline_avg > 0 else 1.0

    # Recent trend: is pressure accelerating?
    recent_avg = sum(b["buy_sell_ratio"] for b in recent) / len(recent)

    # Volume spike: is this bar unusually active?
    all_vols = [b["buy_vol"] + b["sell_vol"] for b in taker_history]
    avg_vol  = sum(all_vols) / len(all_vols)
    cur_vol  = current["buy_vol"] + current["sell_vol"]
    vol_spike = cur_vol / avg_vol if avg_vol > 0 else 1.0

    # Score: based on how extreme the ratio is vs baseline + volume confirmation
    score = 0
    if ratio_vs_baseline > 1.8 and ratio > 1.4:      # strong short squeeze
        score = 30
        signal = "bullish_strong"
    elif ratio_vs_baseline > 1.4 or ratio > 1.5:     # moderate short squeeze
        score = 18
        signal = "bullish"
    elif ratio_vs_baseline < 0.55 and ratio < 0.7:   # strong long liquidation
        score = 30
        signal = "bearish_strong"
    elif ratio_vs_baseline < 0.7 or ratio < 0.65:    # moderate long liquidation
        score = 18
        signal = "bearish"
    else:
        score = 0
        signal = "neutral"

    # Boost score if accompanied by a volume spike (more conviction)
    if vol_spike > 1.8 and signal != "neutral":
        score = min(score + 10, 40)

    return {
        "signal": signal,
        "ratio": round(ratio, 3),
        "ratio_vs_baseline": round(ratio_vs_baseline, 2),
        "buy_vol": round(current["buy_vol"], 1),
        "sell_vol": round(current["sell_vol"], 1),
        "vol_spike": round(vol_spike, 2),
        "score": score,
    }


def calculate_delta_signal(taker_history: list, klines: list) -> dict:
    """
    Compute per-bar delta (buy_vol - sell_vol) and Cumulative Volume Delta (CVD).
    CVD rising + price rising   = bullish confirmation (new money, real buying)
    CVD falling + price falling = bearish confirmation (real selling)
    CVD rising + price falling  = bullish divergence   (accumulation, potential reversal up)
    CVD falling + price rising  = bearish divergence   (distribution, potential reversal down)
    """
    if not taker_history or len(taker_history) < 6:
        return {"signal": "neutral", "delta_last": 0, "cvd": 0,
                "cvd_slope": 0, "divergence": False, "score": 0}

    # Per-bar delta
    deltas = [b["buy_vol"] - b["sell_vol"] for b in taker_history]

    # Rolling CVD over available window
    cvd_series = []
    running = 0.0
    for d in deltas:
        running += d
        cvd_series.append(running)

    # Normalise so CVD starts at 0 in this window
    baseline = cvd_series[0]
    cvd_series = [v - baseline for v in cvd_series]

    # Slope over last 6 bars (~30 min)
    lookback = min(6, len(cvd_series))
    cvd_recent_slope = cvd_series[-1] - cvd_series[-lookback]

    # Price slope over same period
    price_slope = 0.0
    if klines and len(klines) >= lookback:
        price_slope = klines[-1]["close"] - klines[-lookback]["close"]

    cvd_rising   = cvd_recent_slope > 0
    price_rising = price_slope > 0
    divergence   = cvd_rising != price_rising

    if not divergence:
        if cvd_rising:
            signal = "bullish_confirm"
            score  = 22
        else:
            signal = "bearish_confirm"
            score  = 22
    else:
        if cvd_rising:          # CVD up, price down → accumulation
            signal = "bullish_divergence"
            score  = 16
        else:                   # CVD down, price up → distribution
            signal = "bearish_divergence"
            score  = 16

    return {
        "signal":        signal,
        "delta_last":    round(deltas[-1], 2),
        "cvd":           round(cvd_series[-1], 2),
        "cvd_slope":     round(cvd_recent_slope, 2),
        "divergence":    divergence,
        "score":         score,
    }

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
    tp_pct = config.get("tp_pct", 3.0)   # Updated: 3.0% TP — wider target, same 2.5:1 R:R
    sl_pct = config.get("sl_pct", 1.2)   # Updated: 1.2% SL — avoids noise-stops on swings

    klines      = market_data.get("klines") or []
    htf_klines  = market_data.get("htf_klines") or []
    oi_history  = market_data.get("oi_history") or []
    funding     = market_data.get("funding") or {}
    ls_history  = market_data.get("ls_ratio") or []
    ticker      = market_data.get("ticker") or {}

    taker_history = market_data.get("taker_ratio") or []
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
    taker_sig  = calculate_taker_pressure_signal(taker_history)
    delta_sig  = calculate_delta_signal(taker_history, klines)

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
        "taker_ratio": round(taker_sig["ratio"], 3),
        "taker_vs_baseline": round(taker_sig["ratio_vs_baseline"], 2),
        "taker_vol_spike": round(taker_sig["vol_spike"], 2),
        "delta_last": delta_sig["delta_last"],
        "cvd": delta_sig["cvd"],
        "cvd_slope": delta_sig["cvd_slope"],
        "cvd_signal": delta_sig["signal"],
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

    # 6. Taker pressure (liquidation proxy)
    # Taker spikes >= 3.0x have 0% win rate empirically ? someone panic-bought at the top.
    # Real sustained buying sits in the 1.5-3.0x range (46% WR). Cap at 3.0x.
    if taker_sig["ratio"] >= 3.0:
        reasons.append(
            f"Taker spike IGNORED ? ratio {taker_sig['ratio']:.2f}x >= 3.0 is a pump candle, not sustained pressure"
        )
        taker_signal = "neutral"
        taker_score  = 0
    else:
        taker_signal = taker_sig["signal"]
        taker_score  = taker_sig["score"]
    if taker_signal in ("bullish", "bullish_strong"):
        vol_note = f" (vol spike x{taker_sig['vol_spike']:.1f})" if taker_sig["vol_spike"] > 1.8 else ""
        reasons.append(
            f"Taker pressure bullish — buy/sell ratio {taker_sig['ratio']:.2f} "
            f"({taker_sig['ratio_vs_baseline']:.1f}x baseline){vol_note} — shorts being squeezed"
        )
        long_score  += taker_score
        long_signals += 1
    elif taker_signal in ("bearish", "bearish_strong"):
        vol_note = f" (vol spike x{taker_sig['vol_spike']:.1f})" if taker_sig["vol_spike"] > 1.8 else ""
        reasons.append(
            f"Taker pressure bearish — buy/sell ratio {taker_sig['ratio']:.2f} "
            f"({taker_sig['ratio_vs_baseline']:.1f}x baseline){vol_note} — longs being liquidated"
        )
        short_score  += taker_score
        short_signals += 1
    else:
        reasons.append(f"Taker pressure neutral (ratio {taker_sig['ratio']:.2f})")

    # 7. Delta / CVD signal
    delta_signal = delta_sig["signal"]
    delta_score  = delta_sig["score"]
    cvd_note = f"CVD {delta_sig['cvd']:+.0f} (slope {delta_sig['cvd_slope']:+.1f})"
    if delta_signal == "bullish_confirm":
        reasons.append(f"CVD confirming — real buying pressure ({cvd_note})")
        long_score  += delta_score; long_signals  += 1
    elif delta_signal == "bearish_confirm":
        reasons.append(f"CVD confirming — real selling pressure ({cvd_note})")
        short_score += delta_score; short_signals += 1
    elif delta_signal == "bullish_divergence":
        # Only trust divergence if taker pressure is not actively bearish
        # (taker shows what is happening NOW; divergence is backward-looking)
        if taker_sig["ratio"] >= 0.80:
            reasons.append(f"CVD bullish divergence — accumulation while price fell ({cvd_note})")
            long_score  += delta_score; long_signals  += 1
        else:
            reasons.append(
                f"CVD bullish divergence IGNORED — taker actively bearish "
                f"({taker_sig['ratio']:.2f}x) overrides backward-looking CVD"
            )
    elif delta_signal == "bearish_divergence":
        # Only trust divergence if taker pressure is not actively bullish
        if taker_sig["ratio"] <= 1.20:
            reasons.append(f"CVD bearish divergence — distribution while price rose ({cvd_note})")
            short_score += delta_score; short_signals += 1
        else:
            reasons.append(
                f"CVD bearish divergence IGNORED — taker actively bullish "
                f"({taker_sig['ratio']:.2f}x) overrides backward-looking CVD"
            )
    else:
        reasons.append(f"CVD neutral ({cvd_note})")

    # --- Determine direction ---
    # Rules:
    # 1. Score must reach min_confidence threshold
    # 2. Must have at least 2 distinct signals agreeing (not just 1 strong RSI alone)
    # 3. 4H trend must not be against the trade direction
    min_score = config.get("min_confidence", 65)   # Updated: 65 min — quality over quantity
    MIN_SIGNALS = 2   # require at least 2 distinct indicators pointing same way

    long_ok  = (long_score > short_score
                and long_score >= min_score
                and long_signals >= MIN_SIGNALS
                and htf_trend != "bearish")   # don't long in 4H downtrend

    short_ok = (short_score > long_score
                and short_score >= min_score
                and short_signals >= MIN_SIGNALS
                and htf_trend != "bullish")   # don't short in 4H uptrend

    # ── RSI quality gates ──────────────────────────────────────────────────
    # Empirically: LONGs at RSI >= 80 have 0% win rate (chasing extended moves).
    # LONGs at RSI 62-80 only work when funding is actively bullish (shorts paying).
    # Mirror logic for SHORTs.
    funding_bullish = funding_sig["signal"] in ("bullish_mild", "bullish_extreme")
    funding_bearish = funding_sig["signal"] in ("bearish_mild", "bearish_extreme")

    if long_ok:
        if rsi >= 80:
            long_ok = False
            reasons.append(
                f"Quality gate: RSI {rsi:.0f} ≥ 80 — price is extended, not chasing a long here"
            )
        elif rsi >= 62 and not funding_bullish:
            long_ok = False
            reasons.append(
                f"Quality gate: RSI elevated ({rsi:.0f}) with neutral funding — "
                f"need funding support to long an extended move"
            )
        elif 40 <= rsi < 55:
            long_ok = False
            reasons.append(
                f"Quality gate: RSI {rsi:.0f} in dead zone (40-55) "
                f"-- not oversold enough to bounce, not strong enough to continue"
            )
        elif oi_mom["trend"] == "falling":
            long_ok = False
            reasons.append(
                f"Quality gate: OI falling ({oi_mom['roc']:.2f}%) — "
                f"positions unwinding, not a clean long entry"
            )
        elif taker_sig["ratio"] < 1.3:
            long_ok = False
            reasons.append(
                f"Quality gate: taker ratio {taker_sig['ratio']:.2f} < 1.3 — "
                f"no real buyers yet, not entering a falling knife"
            )
        elif klines and klines[-1]["close"] <= klines[-1]["open"]:
            long_ok = False
            reasons.append(
                f"Quality gate: confirmation candle failed — last bar is red "
                f"(close {klines[-1]['close']:.2f} <= open {klines[-1]['open']:.2f}), "
                f"price still falling, waiting for green close"
            )

    # ── BTC macro filter for alt coins ────────────────────────────────────────
    # Block alt LONGs when BTC 4H trend is bearish (EMA50 < EMA200).
    # Alts bleed in BTC downtrends regardless of their own signals.
    # btc_htf_klines is injected by main.py for non-BTC/ETH symbols only.
    btc_htf_klines = market_data.get("btc_htf_klines") or []
    if long_ok and btc_htf_klines:
        btc_htf = get_htf_trend(btc_htf_klines)
        if btc_htf["trend"] == "bearish":
            long_ok = False
            reasons.append(
                f"Quality gate: BTC macro filter — BTC 4H bearish "
                f"(EMA50={btc_htf['ema50']:.0f} < EMA200={btc_htf['ema200']:.0f}), "
                f"blocking alt LONG"
            )

    # ── SHORTs disabled ────────────────────────────────────────────────────────
    # Historical SHORT win rate = 13% (3 wins / 22 trades). Strategy is LONG-only.
    # Keeping the scoring logic intact so confidence numbers are still accurate,
    # but no SHORT trade will ever be opened.
    MIN_SHORT_CONFIDENCE = 75  # SHORTs need higher bar -- 13% WR vs 36% for LONGs historically
    if short_ok:
        if short_score < MIN_SHORT_CONFIDENCE:
            short_ok = False
            reasons.append(
                f"Quality gate: SHORT confidence {short_score:.0f} < {MIN_SHORT_CONFIDENCE} "
                f"-- shorts need higher conviction (historical SHORT win rate is only 13%)"
            )
        elif rsi <= 20:
            short_ok = False
            reasons.append(
                f"Quality gate: RSI {rsi:.0f} ≤ 20 — price is oversold, not chasing a short here"
            )
        elif rsi <= 38 and not funding_bearish:
            short_ok = False
            reasons.append(
                f"Quality gate: RSI low ({rsi:.0f}) with neutral funding — "
                f"need funding support to short an oversold move"
            )

    # Hard block — strategy is LONG-only (SHORT WR = 13% historically)
    if short_ok:
        short_ok = False
        reasons.append(
            f"Quality gate: SHORTs disabled — strategy is LONG-only "
            f"(historical SHORT WR 13%, 3/22 trades). Re-enable only with new evidence."
        )

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
