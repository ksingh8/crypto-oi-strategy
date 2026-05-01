"""
S/R + Order Flow Strategy v2
-----------------------------
Entry based on:
1. Price at 4H support/resistance level (pivot highs/lows)
2. Liquidity sweep confirmed on 15m (price swept through level and reclaimed)
3. Order book depth imbalance (who is in control at this level)
4. Delta / CVD confirmation

SL: below sweep wick (LONG) or above sweep wick (SHORT) + 0.15% buffer, capped at 2%
TP: 2.5:1 R:R from SL distance
"""

from dataclasses import dataclass
from typing import Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    direction: str          # "LONG" | "SHORT" | "NEUTRAL"
    confidence: float
    entry_price: float
    tp_price: float
    sl_price: float
    reasons: list
    indicators: dict
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()


def find_sr_levels(htf_klines: list, pivot_strength: int = 3, max_levels: int = 15) -> list:
    """
    Find S/R levels from 4H klines using pivot high/low method.
    Clusters levels within 0.5% of each other (keeps most recent).
    Returns list sorted by recency (most recent first).
    """
    if not htf_klines or len(htf_klines) < pivot_strength * 2 + 2:
        return []

    levels = []
    n = len(htf_klines)
    for i in range(pivot_strength, n - pivot_strength):
        bar = htf_klines[i]
        # Pivot high = resistance
        is_pivot_high = all(
            bar['high'] >= htf_klines[j]['high']
            for j in range(i - pivot_strength, i + pivot_strength + 1) if j != i
        )
        if is_pivot_high:
            levels.append({'price': bar['high'], 'type': 'resistance', 'index': i, 'strength': 1})
        # Pivot low = support
        is_pivot_low = all(
            bar['low'] <= htf_klines[j]['low']
            for j in range(i - pivot_strength, i + pivot_strength + 1) if j != i
        )
        if is_pivot_low:
            levels.append({'price': bar['low'], 'type': 'support', 'index': i, 'strength': 1})

    # Cluster levels within 0.5% — keep most recent, count touches as strength
    clustered = []
    for lvl in reversed(levels):  # most recent first
        nearby = [c for c in clustered if abs(c['price'] - lvl['price']) / lvl['price'] < 0.005]
        if nearby:
            nearby[0]['strength'] += 1
        else:
            clustered.append(dict(lvl))

    clustered.sort(key=lambda x: x['index'], reverse=True)
    return clustered[:max_levels]


def find_nearest_level(sr_levels: list, price: float, proximity_pct: float = 0.4) -> Optional[dict]:
    """Return the nearest S/R level within proximity_pct of current price, or None."""
    if not sr_levels:
        return None
    best, best_dist = None, float('inf')
    for lvl in sr_levels:
        dist = abs(lvl['price'] - price) / price * 100
        if dist <= proximity_pct and dist < best_dist:
            best_dist = dist
            best = lvl
    return best


def detect_sweep(klines_15m: list, level_price: float, level_type: str,
                 sweep_pct: float = 0.1, lookback: int = 4) -> dict:
    """
    Detect liquidity sweep + retest on 15m.

    Support sweep (LONG setup):
      - Any of last N bars had low below level by >= sweep_pct%
      - Current bar closed back ABOVE level with a green close
    Resistance sweep (SHORT setup):
      - Any of last N bars had high above level by >= sweep_pct%
      - Current bar closed back BELOW level with a red close
    """
    empty = {'swept': False, 'retest': False, 'sweep_wick': level_price, 'bars_ago': 0}
    if not klines_15m or len(klines_15m) < lookback + 2:
        return empty

    current = klines_15m[-1]
    prior   = klines_15m[-(lookback + 1):-1]   # last N bars before current

    if level_type == 'support':
        swept, sweep_wick, bars_ago = False, level_price, 0
        for i, bar in enumerate(reversed(prior)):
            if bar['low'] < level_price * (1 - sweep_pct / 100):
                swept, sweep_wick, bars_ago = True, bar['low'], i + 1
                break
        retest = (current['close'] > level_price and current['close'] > current['open'])
        return {'swept': swept, 'retest': retest, 'sweep_wick': sweep_wick, 'bars_ago': bars_ago}

    else:  # resistance
        swept, sweep_wick, bars_ago = False, level_price, 0
        for i, bar in enumerate(reversed(prior)):
            if bar['high'] > level_price * (1 + sweep_pct / 100):
                swept, sweep_wick, bars_ago = True, bar['high'], i + 1
                break
        retest = (current['close'] < level_price and current['close'] < current['open'])
        return {'swept': swept, 'retest': retest, 'sweep_wick': sweep_wick, 'bars_ago': bars_ago}


def calc_orderbook_imbalance(orderbook: dict, depth_levels: int = 20) -> dict:
    """
    Bid/ask depth imbalance — who is in control at this level.
    imbalance in [-1, 1]: positive = more bids (buyers), negative = more asks (sellers)
    """
    if not orderbook:
        return {'imbalance': 0.0, 'bid_usd': 0.0, 'ask_usd': 0.0, 'signal': 'neutral'}

    bids = orderbook.get('bids', [])[:depth_levels]
    asks = orderbook.get('asks', [])[:depth_levels]
    bid_usd = sum(float(p) * float(q) for p, q in bids)
    ask_usd = sum(float(p) * float(q) for p, q in asks)
    total   = bid_usd + ask_usd
    if total == 0:
        return {'imbalance': 0.0, 'bid_usd': 0.0, 'ask_usd': 0.0, 'signal': 'neutral'}

    imbalance = (bid_usd - ask_usd) / total
    if   imbalance >  0.20: signal = 'bullish_strong'
    elif imbalance >  0.10: signal = 'bullish'
    elif imbalance < -0.20: signal = 'bearish_strong'
    elif imbalance < -0.10: signal = 'bearish'
    else:                   signal = 'neutral'

    return {
        'imbalance': round(imbalance, 3),
        'bid_usd':   round(bid_usd, 0),
        'ask_usd':   round(ask_usd, 0),
        'signal':    signal
    }


def calculate_delta_signal(taker_history: list, klines: list) -> dict:
    """CVD / cumulative delta signal."""
    if not taker_history or len(taker_history) < 6:
        return {'signal': 'neutral', 'cvd': 0, 'cvd_slope': 0, 'score': 0}

    deltas     = [b['buy_vol'] - b['sell_vol'] for b in taker_history]
    running    = 0.0
    cvd_series = []
    for d in deltas:
        running += d
        cvd_series.append(running)
    baseline   = cvd_series[0]
    cvd_series = [v - baseline for v in cvd_series]

    lookback   = min(6, len(cvd_series))
    cvd_slope  = cvd_series[-1] - cvd_series[-lookback]
    price_slope = 0.0
    if klines and len(klines) >= lookback:
        price_slope = klines[-1]['close'] - klines[-lookback]['close']

    cvd_rising   = cvd_slope   > 0
    price_rising = price_slope > 0
    divergence   = cvd_rising != price_rising

    if not divergence:
        signal = 'bullish_confirm' if cvd_rising else 'bearish_confirm'
        score  = 15
    else:
        signal = 'bullish_divergence' if cvd_rising else 'bearish_divergence'
        score  = 8

    return {'signal': signal, 'cvd': round(cvd_series[-1], 2),
            'cvd_slope': round(cvd_slope, 2), 'score': score}

def calc_ema(data: list, period: int) -> float:
    if len(data) < period:
        return data[-1] if data else 0.0
    k = 2 / (period + 1)
    ema = data[0]
    for v in data[1:]:
        ema = v * k + ema * (1 - k)
    return ema

def get_htf_trend(htf_klines: list) -> dict:
    """4H EMA50 vs EMA200 trend direction."""
    if not htf_klines or len(htf_klines) < 50:
        return {'trend': 'neutral', 'ema50': 0.0, 'ema200': 0.0}
    closes = [k['close'] for k in htf_klines]
    ema50  = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200) if len(closes) >= 200 else calc_ema(closes, len(closes))
    trend  = 'bullish' if ema50 > ema200 else 'bearish'
    return {'trend': trend, 'ema50': round(ema50, 4), 'ema200': round(ema200, 4)}


def generate_signal(market_data: dict, config: dict) -> Signal:
    """
    S/R + Order Flow signal generator (v2).

    Flow:
      1. Build S/R map from 4H klines (pivot highs/lows, clustered)
      2. Check if current price is within 0.4% of any level
      3. Confirm liquidity sweep + retest on 15m
      4. Score: order book imbalance + CVD delta + taker ratio
      5. Dynamic SL at sweep wick, TP at 2.5:1 R:R
    """
    klines_5m     = market_data.get('klines')     or []
    klines_15m    = market_data.get('klines_15m') or []
    htf_klines    = market_data.get('htf_klines') or []
    orderbook     = market_data.get('orderbook')  or {}
    taker_history = market_data.get('taker_ratio') or []
    ticker        = market_data.get('ticker')     or {}

    closes_5m     = [k['close'] for k in klines_5m]
    current_price = ticker.get('price') or (closes_5m[-1] if closes_5m else 0)

    if not current_price:
        return Signal('NEUTRAL', 0, 0, 0, 0, ['No price data'], {})

    reasons    = []
    indicators = {'current_price': current_price}

    # ── 1. S/R levels from 4H ──────────────────────────────────────────────────
    sr_levels = find_sr_levels(htf_klines, pivot_strength=3, max_levels=15)
    indicators['sr_levels'] = [
        {'price': round(l['price'], 4), 'type': l['type'], 'strength': l['strength']}
        for l in sr_levels[:6]
    ]
    if not sr_levels:
        return Signal('NEUTRAL', 0, current_price, 0, 0,
                      ['No S/R levels — insufficient 4H data'], indicators)

    # ── 2. Price at level? ────────────────────────────────────────────────────
    nearest = find_nearest_level(sr_levels, current_price, proximity_pct=0.4)
    if not nearest:
        closest = min(sr_levels, key=lambda l: abs(l['price'] - current_price) / current_price)
        dist    = abs(closest['price'] - current_price) / current_price * 100
        reasons.append(f"Price not near any S/R — closest {closest['type']} @ "
                       f"{closest['price']:.4f} ({dist:.2f}% away)")
        return Signal('NEUTRAL', 0, current_price, 0, 0, reasons, indicators)

    level_price = nearest['price']
    level_type  = nearest['type']
    level_dist  = abs(level_price - current_price) / current_price * 100
    indicators.update({'level_price': level_price, 'level_type': level_type,
                       'level_strength': nearest['strength']})
    reasons.append(f"Price at 4H {level_type} {level_price:.4f} "
                   f"({level_dist:.2f}% away, {nearest['strength']} touches)")

    # ── 3. Sweep + retest on 15m ──────────────────────────────────────────────
    sweep = detect_sweep(klines_15m, level_price, level_type)
    indicators.update({'sweep_detected': sweep['swept'], 'retest_confirmed': sweep['retest'],
                       'sweep_wick': sweep['sweep_wick']})

    if not sweep['swept']:
        reasons.append(f"No sweep yet — waiting for wick through {level_price:.4f}")
        return Signal('NEUTRAL', 0, current_price, 0, 0, reasons, indicators)

    if not sweep['retest']:
        direction_word = 'above' if level_type == 'support' else 'below'
        reasons.append(f"Sweep detected {sweep['bars_ago']}x 15m ago (wick to "
                       f"{sweep['sweep_wick']:.4f}) — waiting for close {direction_word} level")
        return Signal('NEUTRAL', 0, current_price, 0, 0, reasons, indicators)

    reasons.append(f"Liquidity sweep confirmed — wick to {sweep['sweep_wick']:.4f} "
                   f"({sweep['bars_ago']} bars ago), price reclaimed level")

    # ── 4. HTF trend filter ───────────────────────────────────────────────────
    direction = 'LONG' if level_type == 'support' else 'SHORT'

    htf = get_htf_trend(htf_klines)
    indicators.update({'htf_trend': htf['trend'], 'htf_ema50': htf['ema50'],
                       'htf_ema200': htf['ema200']})

    if direction == 'LONG' and htf['trend'] == 'bearish':
        reasons.append(f"HTF blocked: 4H bearish (EMA50={htf['ema50']:.2f} < EMA200={htf['ema200']:.2f}) — no LONGs in downtrend")
        return Signal('NEUTRAL', 0, current_price, 0, 0, reasons, indicators)
    elif direction == 'SHORT' and htf['trend'] == 'bullish':
        reasons.append(f"HTF blocked: 4H bullish (EMA50={htf['ema50']:.2f} > EMA200={htf['ema200']:.2f}) — no SHORTs in uptrend")
        return Signal('NEUTRAL', 0, current_price, 0, 0, reasons, indicators)

    reasons.append(f"4H trend: {htf['trend'].upper()} — aligned with {direction} (EMA50={htf['ema50']:.2f} vs EMA200={htf['ema200']:.2f})")

    # ── 5. Confirmations (need >= 2 of 3: OB + CVD + taker) ──────────────────
    score         = 50   # base: at level + sweep + HTF aligned
    confirmations = 0    # must reach >= 2 to trade

    # Order book imbalance
    ob = calc_orderbook_imbalance(orderbook)
    indicators.update({'ob_imbalance': ob['imbalance'], 'ob_bid_usd': ob['bid_usd'],
                       'ob_ask_usd': ob['ask_usd'], 'ob_signal': ob['signal']})
    ob_confirms = (direction == 'LONG'  and 'bullish' in ob['signal']) or \
                  (direction == 'SHORT' and 'bearish' in ob['signal'])
    ob_opposes  = (direction == 'LONG'  and 'bearish' in ob['signal']) or \
                  (direction == 'SHORT' and 'bullish' in ob['signal'])
    if ob_confirms:
        pts = 25 if 'strong' in ob['signal'] else 15
        score += pts
        confirmations += 1
        reasons.append(f"OB {'bullish' if direction=='LONG' else 'bearish'}: "
                       f"imbalance {ob['imbalance']:+.2f} "
                       f"(bids ${ob['bid_usd']:,.0f} vs asks ${ob['ask_usd']:,.0f})")
    elif ob_opposes:
        score -= 15
        reasons.append(f"OB opposing: imbalance {ob['imbalance']:+.2f} against {direction}")
    else:
        reasons.append(f"OB neutral: imbalance {ob['imbalance']:+.2f}")

    # CVD / Delta
    delta = calculate_delta_signal(taker_history, klines_5m)
    indicators.update({'cvd': delta['cvd'], 'cvd_slope': delta['cvd_slope'],
                       'cvd_signal': delta['signal']})
    delta_confirms = (direction == 'LONG'  and 'bullish' in delta['signal']) or \
                     (direction == 'SHORT' and 'bearish' in delta['signal'])
    if delta_confirms:
        score += delta['score']
        confirmations += 1
        kind = 'confirm' if 'confirm' in delta['signal'] else 'divergence'
        reasons.append(f"CVD {kind}: slope {delta['cvd_slope']:+.0f} supporting {direction}")
    else:
        cvd_opposes = (direction == 'LONG' and 'bearish' in delta['signal']) or \
                      (direction == 'SHORT' and 'bullish' in delta['signal'])
        if cvd_opposes:
            confirmations -= 1
            reasons.append(f"CVD opposing {direction}: slope {delta['cvd_slope']:+.0f} -- confirmation subtracted")
        else:
            reasons.append(f"CVD not confirming: slope {delta['cvd_slope']:+.0f}")

    # Taker ratio — confirming adds pts, opposing subtracts
    taker_ratio = taker_history[-1].get('buy_sell_ratio', 1.0) if taker_history else 1.0
    indicators['taker_ratio'] = taker_ratio

    # Spike cap: taker >= 3.0x = pump/dump candle, not sustained flow
    if (direction == 'LONG' and taker_ratio >= 3.0) or (direction == 'SHORT' and taker_ratio <= 0.33):
        reasons.append(f"Taker spike cap: {taker_ratio:.2f}x -- pump/dump candle, skipping")
        return Signal('NEUTRAL', score, current_price, 0, 0, reasons, indicators)

    if direction == 'LONG' and taker_ratio >= 1.3:
        score += 10
        confirmations += 1
        reasons.append(f"Taker buyers active: {taker_ratio:.2f}x")
    elif direction == 'SHORT' and taker_ratio <= 0.75:
        score += 10
        confirmations += 1
        reasons.append(f"Taker sellers active: {taker_ratio:.2f}x")
    elif direction == 'LONG' and taker_ratio < 0.85:
        score -= 15
        reasons.append(f"Taker opposing LONG: sellers active at {taker_ratio:.2f}x")
    elif direction == 'SHORT' and taker_ratio > 1.15:
        score -= 15
        reasons.append(f"Taker opposing SHORT: buyers active at {taker_ratio:.2f}x")
    else:
        reasons.append(f"Taker neutral: {taker_ratio:.2f}x")

    indicators['score']         = score
    indicators['confirmations'] = confirmations
    min_confidence = config.get('min_confidence', 75)

    # Gate 1: need >= 2 of 3 signals confirming
    if confirmations < 2:
        reasons.append(f"Only {confirmations}/3 signals confirming — need OB+CVD, OB+taker, or CVD+taker")
        return Signal('NEUTRAL', score, current_price, 0, 0, reasons, indicators)

    # Gate 2: score threshold
    if score < min_confidence:
        reasons.append(f"Score {score} < {min_confidence} — not enough conviction")
        return Signal('NEUTRAL', score, current_price, 0, 0, reasons, indicators)

    # ── 5. Dynamic SL / TP ────────────────────────────────────────────────────
    RR          = 2.5
    BUFFER_PCT  = 0.15
    MAX_SL_PCT  = 2.0

    if direction == 'LONG':
        sl_raw    = sweep['sweep_wick'] * (1 - BUFFER_PCT / 100)
        sl_dist   = (current_price - sl_raw) / current_price * 100
        if sl_dist > MAX_SL_PCT:
            sl_raw  = current_price * (1 - MAX_SL_PCT / 100)
            sl_dist = MAX_SL_PCT
        sl_price  = round(sl_raw, 4)
        tp_dist   = sl_dist * RR
        tp_price  = round(current_price * (1 + tp_dist / 100), 4)
    else:
        sl_raw    = sweep['sweep_wick'] * (1 + BUFFER_PCT / 100)
        sl_dist   = (sl_raw - current_price) / current_price * 100
        if sl_dist > MAX_SL_PCT:
            sl_raw  = current_price * (1 + MAX_SL_PCT / 100)
            sl_dist = MAX_SL_PCT
        sl_price  = round(sl_raw, 4)
        tp_dist   = sl_dist * RR
        tp_price  = round(current_price * (1 - tp_dist / 100), 4)

    # Min SL: if sweep wick too shallow, noise will stop us out
    MIN_SL_PCT = 0.6
    if sl_dist < MIN_SL_PCT:
        reasons.append(f"SL too tight: {sl_dist:.2f}% < {MIN_SL_PCT}% min -- sweep wick too shallow")
        indicators.update({'sl_pct': round(sl_dist, 3)})
        return Signal('NEUTRAL', score, current_price, 0, 0, reasons, indicators)

    indicators.update({'sl_pct': round(sl_dist, 3), 'tp_pct': round(tp_dist, 3),
                       'rr_ratio': RR})
    reasons.append(f"SL @ {sl_price:.4f} ({sl_dist:.2f}% risk) | "
                   f"TP @ {tp_price:.4f} (+{tp_dist:.2f}%, {RR}:1 R:R)")

    return Signal(
        direction=direction,
        confidence=min(int(score), 100),
        entry_price=current_price,
        tp_price=tp_price,
        sl_price=sl_price,
        reasons=reasons,
        indicators=indicators
    )
