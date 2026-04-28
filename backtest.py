#!/usr/bin/env python3
"""
Backtest for crypto-oi-strategy — 16 months of 4h bars
Signals used: RSI (1h closes), 4H EMA50/EMA200, taker ratio (from klines),
              CVD (from klines), funding rate (paginated history).
OI and LS ratio default to neutral when historical data is unavailable.

Run on VPS: python3 backtest.py
"""
import asyncio
import httpx
import json
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field

# ── Config ─────────────────────────────────────────────────────────────────────
SYMBOLS       = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT",
                 "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
                 "MATICUSDT", "LTCUSDT"]
TP_PCT        = 3.0
SL_PCT        = 1.2
MIN_CONF      = 65
MIN_SHORT_CONF = 75
BAD_HOURS_UTC = {0, 2, 4, 5, 11, 16, 17, 22}
BASE          = "https://fapi.binance.com"
MONTHS_BACK   = 16

# ── Data structures ────────────────────────────────────────────────────────────
@dataclass
class Bar:
    ts: datetime
    open: float; high: float; low: float; close: float
    volume: float; taker_buy_vol: float; quote_vol: float; taker_buy_quote_vol: float

@dataclass
class Trade:
    symbol: str; direction: str; entry: float; tp: float; sl: float
    entry_ts: datetime; conf: int; reasons: list = field(default_factory=list)
    exit_price: float = 0; exit_reason: str = ""; exit_ts: datetime = None
    pnl_pct: float = 0

# ── Fetch helpers ──────────────────────────────────────────────────────────────
async def fetch_klines(client, symbol, interval, start_ts, end_ts):
    """Fetch all klines between start_ts and end_ts in pages of 1500."""
    bars = []
    current = int(start_ts.timestamp() * 1000)
    end_ms  = int(end_ts.timestamp() * 1000)
    while current < end_ms:
        r = await client.get(f"{BASE}/fapi/v1/klines", params={
            "symbol": symbol, "interval": interval,
            "startTime": current, "endTime": end_ms, "limit": 1500
        })
        data = r.json()
        if not data: break
        for d in data:
            bars.append(Bar(
                ts=datetime.fromtimestamp(d[0]/1000, tz=timezone.utc),
                open=float(d[1]), high=float(d[2]), low=float(d[3]), close=float(d[4]),
                volume=float(d[5]), taker_buy_vol=float(d[9]),
                quote_vol=float(d[7]), taker_buy_quote_vol=float(d[10])
            ))
        current = data[-1][6] + 1  # next bar after close time
        await asyncio.sleep(0.05)
    return bars

async def fetch_funding_history(client, symbol, start_ts):
    """Paginate through all funding rate records from start_ts onwards."""
    records = {}
    start_ms = int(start_ts.timestamp() * 1000)
    current = start_ms
    while True:
        r = await client.get(f"{BASE}/fapi/v1/fundingRate", params={
            "symbol": symbol, "startTime": current, "limit": 1000
        })
        data = r.json()
        if not data: break
        for d in data:
            ts = datetime.fromtimestamp(d["fundingTime"]/1000, tz=timezone.utc)
            records[ts] = float(d["fundingRate"])
        if len(data) < 1000: break
        current = data[-1]["fundingTime"] + 1
        await asyncio.sleep(0.05)
    return records  # {datetime: rate}

# ── Signal logic ───────────────────────────────────────────────────────────────
def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0: return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))

def calc_ema(closes, period):
    if len(closes) < period: return closes[-1]
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema

def get_funding_rate(funding_records, bar_ts):
    """Get the most recent funding rate at or before bar_ts."""
    candidates = [v for t, v in funding_records.items() if t <= bar_ts]
    return candidates[-1] if candidates else 0.0

def generate_signal(bar_idx, h4_bars, h1_bars, funding_records):
    """
    Generate signal for bar at bar_idx in h4_bars.
    Returns: (direction, confidence, reasons)
    """
    bar = h4_bars[bar_idx]

    # Time filter
    if bar.ts.hour in BAD_HOURS_UTC:
        return "NEUTRAL", 0, ["Time filter: bad hour"]

    # Need enough history
    if bar_idx < 50: return "NEUTRAL", 0, ["Not enough history"]

    # ── 4H trend (EMA50 vs EMA200) ──
    h4_closes = [b.close for b in h4_bars[:bar_idx+1]]
    if len(h4_closes) < 200:
        return "NEUTRAL", 0, ["Not enough 4H history for EMA200"]
    ema50  = calc_ema(h4_closes[-200:], 50)
    ema200 = calc_ema(h4_closes[-200:], 200)
    htf_trend = "bullish" if ema50 > ema200 else "bearish"

    # ── RSI (from 1h closes aligned to this 4h bar) ──
    h1_aligned = [b for b in h1_bars if b.ts <= bar.ts]
    h1_closes  = [b.close for b in h1_aligned[-28:]]  # last 28 1h closes
    rsi = calc_rsi(h1_closes, period=14)

    # ── Taker ratio (from 4h kline: taker_buy_quote / quote_vol) ──
    recent_bars = h4_bars[max(0, bar_idx-23):bar_idx+1]  # last 24 bars
    taker_vals = []
    for b in recent_bars:
        if b.quote_vol > 0:
            ratio = b.taker_buy_quote_vol / b.quote_vol
            taker_vals.append(ratio / (1 - ratio) if ratio < 1 else ratio / (1 - ratio + 1e-9))
    taker_ratio = taker_vals[-1] if taker_vals else 1.0
    taker_baseline = sum(taker_vals) / len(taker_vals) if taker_vals else 1.0

    # ── Funding rate ──
    funding_rate = get_funding_rate(funding_records, bar.ts)
    # Classify funding
    if funding_rate < -0.0050:  funding_sig = "bullish_extreme"
    elif funding_rate < -0.0010: funding_sig = "bullish_mild"
    elif funding_rate > 0.0050:  funding_sig = "bearish_extreme"
    elif funding_rate > 0.0010:  funding_sig = "bearish_mild"
    else:                        funding_sig = "neutral"
    funding_bullish = funding_sig in ("bullish_mild", "bullish_extreme")
    funding_bearish = funding_sig in ("bearish_mild", "bearish_extreme")

    # ── CVD (cumulative volume delta from last 20 bars) ──
    cvd_bars = h4_bars[max(0, bar_idx-19):bar_idx+1]
    cvd = sum(b.taker_buy_quote_vol - (b.quote_vol - b.taker_buy_quote_vol) for b in cvd_bars)
    price_change = cvd_bars[-1].close - cvd_bars[0].close if cvd_bars else 0
    if price_change < 0 and cvd > 0:   delta_signal = "bullish_divergence"
    elif price_change > 0 and cvd < 0: delta_signal = "bearish_divergence"
    elif cvd > 0:                       delta_signal = "bullish_confirm"
    elif cvd < 0:                       delta_signal = "bearish_confirm"
    else:                               delta_signal = "neutral"
    delta_score = 15

    # ── Scoring ──
    long_score  = 0; short_score  = 0
    long_sigs   = 0; short_sigs   = 0
    reasons = []

    # HTF trend bonus
    if htf_trend == "bullish":
        long_score += 20; long_sigs += 1
        reasons.append(f"4H trend: BULLISH (EMA50={ema50:.0f} vs EMA200={ema200:.0f})")
    else:
        short_score += 20; short_sigs += 1
        reasons.append(f"4H trend: BEARISH (EMA50={ema50:.0f} vs EMA200={ema200:.0f})")

    # Funding
    if funding_bullish:
        bonus = 20 if "extreme" in funding_sig else 10
        long_score += bonus; long_sigs += 1
        reasons.append(f"Funding bullish ({funding_rate*100:.4f}%)")
    elif funding_bearish:
        bonus = 20 if "extreme" in funding_sig else 10
        short_score += bonus; short_sigs += 1
        reasons.append(f"Funding bearish ({funding_rate*100:.4f}%)")
    else:
        long_score += 5  # slight long bias on neutral
        reasons.append(f"Funding neutral ({funding_rate*100:.4f}%)")

    # RSI scoring
    if rsi < 30:
        long_score += 30; long_sigs += 1
        reasons.append(f"RSI oversold ({rsi:.1f})")
    elif rsi < 40:
        long_score += 20; long_sigs += 1
        reasons.append(f"RSI low ({rsi:.1f})")
    elif rsi < 50:
        long_score += 10
        reasons.append(f"RSI below midline ({rsi:.1f})")
    elif rsi > 70:
        short_score += 30; short_sigs += 1
        reasons.append(f"RSI overbought ({rsi:.1f})")
    elif rsi > 60:
        short_score += 20; short_sigs += 1
        reasons.append(f"RSI elevated ({rsi:.1f})")
    elif rsi > 50:
        short_score += 10
        reasons.append(f"RSI above midline ({rsi:.1f})")

    # Taker pressure
    if taker_ratio >= 3.0:
        reasons.append(f"Taker spike IGNORED ({taker_ratio:.2f}x >= 3.0)")
    elif taker_ratio >= 1.5:
        score = 30 if taker_ratio >= 2.0 else 20
        long_score += score; long_sigs += 1
        reasons.append(f"Taker bullish ({taker_ratio:.2f}x)")
    elif taker_ratio <= 0.67:
        score = 30 if taker_ratio <= 0.50 else 20
        short_score += score; short_sigs += 1
        reasons.append(f"Taker bearish ({taker_ratio:.2f}x)")
    else:
        reasons.append(f"Taker neutral ({taker_ratio:.2f}x)")

    # CVD
    if delta_signal == "bullish_confirm":
        long_score += delta_score; long_sigs += 1
        reasons.append("CVD confirming bullish")
    elif delta_signal == "bearish_confirm":
        short_score += delta_score; short_sigs += 1
        reasons.append("CVD confirming bearish")
    elif delta_signal == "bullish_divergence":
        if taker_ratio >= 0.80:
            long_score += delta_score; long_sigs += 1
            reasons.append("CVD bullish divergence")
        else:
            reasons.append("CVD bullish divergence IGNORED (taker bearish)")
    elif delta_signal == "bearish_divergence":
        if taker_ratio <= 1.20:
            short_score += delta_score; short_sigs += 1
            reasons.append("CVD bearish divergence")
        else:
            reasons.append("CVD bearish divergence IGNORED (taker bullish)")

    # ── Direction determination ──
    MIN_SIGNALS = 2
    long_ok  = (long_score > short_score and long_score >= MIN_CONF
                and long_sigs >= MIN_SIGNALS and htf_trend != "bearish")
    short_ok = (short_score > long_score and short_score >= MIN_SHORT_CONF
                and short_sigs >= MIN_SIGNALS and htf_trend != "bullish")

    # RSI quality gates
    if long_ok:
        if rsi >= 80:
            long_ok = False
            reasons.append(f"Gate: RSI {rsi:.0f} >= 80 blocked")
        elif rsi >= 62 and not funding_bullish:
            long_ok = False
            reasons.append(f"Gate: RSI {rsi:.0f} elevated, no funding support")
        elif 40 <= rsi < 50:
            long_ok = False
            reasons.append(f"Gate: RSI {rsi:.0f} in dead zone 40-50")

    if short_ok:
        if short_score < MIN_SHORT_CONF:
            short_ok = False
            reasons.append(f"Gate: SHORT conf {short_score} < {MIN_SHORT_CONF}")
        elif rsi <= 20:
            short_ok = False
            reasons.append(f"Gate: RSI {rsi:.0f} <= 20 blocked")
        elif rsi <= 38 and not funding_bearish:
            short_ok = False
            reasons.append(f"Gate: RSI {rsi:.0f} low, no funding support for short")

    if long_ok:
        return "LONG",  min(long_score, 100),  reasons
    elif short_ok:
        return "SHORT", min(short_score, 100), reasons
    else:
        return "NEUTRAL", max(long_score, short_score), reasons

# ── Trade simulation ────────────────────────────────────────────────────────────
def simulate_trades(signals, h4_bars):
    """
    Simulate trades: for each signal, find TP/SL hit in subsequent bars.
    Skip signals that overlap with an already-open trade (realistic: one trade at a time).
    Max hold = 100 bars (~16 days) before force-closing at market.
    """
    trades = []
    next_allowed_bar = 0  # index after which new trades are allowed

    MAX_HOLD = 100  # max 100 x 4h bars = ~16 days before force-close

    for bar_idx, direction, conf, reasons in signals:
        # Skip if a previous trade is still open
        if bar_idx < next_allowed_bar:
            continue

        bar = h4_bars[bar_idx]
        entry = bar.close
        if direction == "LONG":
            tp = entry * (1 + TP_PCT / 100)
            sl = entry * (1 - SL_PCT / 100)
        else:
            tp = entry * (1 - TP_PCT / 100)
            sl = entry * (1 + SL_PCT / 100)

        trade = Trade(
            symbol="", direction=direction, entry=entry, tp=tp, sl=sl,
            entry_ts=bar.ts, conf=conf, reasons=reasons
        )

        # Check subsequent bars for TP/SL hit
        hit = False
        exit_bar_idx = bar_idx
        for j, future_bar in enumerate(h4_bars[bar_idx+1 : bar_idx+MAX_HOLD+1]):
            exit_bar_idx = bar_idx + 1 + j
            if direction == "LONG":
                # Check SL first (conservative — assume worst case within candle)
                if future_bar.low <= sl and future_bar.high >= tp:
                    # Both hit same candle — use open direction to decide
                    if future_bar.open <= entry:
                        trade.exit_price = sl; trade.exit_reason = "SL"
                        trade.pnl_pct = (sl - entry) / entry * 100
                    else:
                        trade.exit_price = tp; trade.exit_reason = "TP"
                        trade.pnl_pct = (tp - entry) / entry * 100
                elif future_bar.low <= sl:
                    trade.exit_price = sl; trade.exit_reason = "SL"
                    trade.pnl_pct = (sl - entry) / entry * 100
                elif future_bar.high >= tp:
                    trade.exit_price = tp; trade.exit_reason = "TP"
                    trade.pnl_pct = (tp - entry) / entry * 100
                else:
                    continue
            else:  # SHORT
                if future_bar.high >= sl and future_bar.low <= tp:
                    if future_bar.open >= entry:
                        trade.exit_price = sl; trade.exit_reason = "SL"
                        trade.pnl_pct = (sl - entry) / entry * 100
                    else:
                        trade.exit_price = tp; trade.exit_reason = "TP"
                        trade.pnl_pct = (tp - entry) / entry * 100
                elif future_bar.high >= sl:
                    trade.exit_price = sl; trade.exit_reason = "SL"
                    trade.pnl_pct = (sl - entry) / entry * 100
                elif future_bar.low <= tp:
                    trade.exit_price = tp; trade.exit_reason = "TP"
                    trade.pnl_pct = (tp - entry) / entry * 100
                else:
                    continue
            trade.exit_ts = future_bar.ts
            hit = True
            break

        if not hit:
            # Force close at max hold
            close_bar = h4_bars[min(bar_idx + MAX_HOLD, len(h4_bars)-1)]
            exit_bar_idx = min(bar_idx + MAX_HOLD, len(h4_bars)-1)
            trade.exit_reason = "TIMEOUT"
            trade.exit_price = close_bar.close
            trade.exit_ts = close_bar.ts
            trade.pnl_pct = (close_bar.close - entry) / entry * 100 * (1 if direction == "LONG" else -1)

        next_allowed_bar = exit_bar_idx + 1  # don't overlap trades
        trades.append(trade)
    return trades

# ── Main ────────────────────────────────────────────────────────────────────────
async def main():
    end_ts   = datetime.now(timezone.utc)
    start_ts = end_ts - timedelta(days=int(MONTHS_BACK * 30.5))
    print(f"Backtest period: {start_ts.strftime('%Y-%m-%d')} → {end_ts.strftime('%Y-%m-%d')}")
    print(f"Symbols: {SYMBOLS}  TP={TP_PCT}%  SL={SL_PCT}%  MinConf={MIN_CONF}\n")

    async with httpx.AsyncClient(timeout=30) as client:
        all_trades = []

        for symbol in SYMBOLS:
            print(f"[{symbol}] Fetching 4h klines...")
            h4_bars = await fetch_klines(client, symbol, "4h", start_ts, end_ts)
            print(f"[{symbol}] Fetching 1h klines...")
            h1_bars = await fetch_klines(client, symbol, "1h", start_ts, end_ts)
            print(f"[{symbol}] Fetching funding history...")
            funding = await fetch_funding_history(client, symbol, start_ts)
            print(f"[{symbol}] Bars: {len(h4_bars)} x 4h | {len(h1_bars)} x 1h | {len(funding)} funding records")

            # Generate signals on each 4h bar — no blocking, simulate all then filter overlaps
            signals = []
            for i in range(len(h4_bars)):
                direction, conf, reasons = generate_signal(i, h4_bars, h1_bars, funding)
                if direction != "NEUTRAL":
                    signals.append((i, direction, conf, reasons))

            print(f"[{symbol}] {len(signals)} signals generated → simulating trades...")
            trades = simulate_trades(signals, h4_bars)
            for t in trades:
                t.symbol = symbol
            all_trades.extend(trades)
            print(f"[{symbol}] Done: {len(trades)} trades\n")

    # ── Results ──────────────────────────────────────────────────────────────
    closed = [t for t in all_trades if t.exit_reason in ("TP","SL")]
    tp_trades = [t for t in closed if t.exit_reason == "TP"]
    sl_trades = [t for t in closed if t.exit_reason == "SL"]
    total_pnl = sum(t.pnl_pct for t in closed)
    win_rate  = len(tp_trades) / len(closed) * 100 if closed else 0
    ev = (len(tp_trades)/len(closed) * TP_PCT - len(sl_trades)/len(closed) * SL_PCT) if closed else 0

    print("=" * 60)
    print(f"BACKTEST RESULTS — {MONTHS_BACK} months")
    print("=" * 60)
    print(f"Total trades:  {len(closed)} closed + {len(all_trades)-len(closed)} open/expired")
    print(f"TP hits:       {len(tp_trades)}")
    print(f"SL hits:       {len(sl_trades)}")
    print(f"Win rate:      {win_rate:.1f}%")
    print(f"Total PnL:     {total_pnl:.2f}%")
    print(f"EV per trade:  {ev:.3f}%")
    print()

    # By symbol
    for sym in SYMBOLS:
        sym_t = [t for t in closed if t.symbol == sym]
        sym_tp = [t for t in sym_t if t.exit_reason == "TP"]
        wr = len(sym_tp)/len(sym_t)*100 if sym_t else 0
        pnl = sum(t.pnl_pct for t in sym_t)
        print(f"  {sym}: {len(sym_t)} trades | {len(sym_tp)} TP | WR={wr:.1f}% | PnL={pnl:.2f}%")

    # By direction
    print()
    for direction in ["LONG", "SHORT"]:
        d_t  = [t for t in closed if t.direction == direction]
        d_tp = [t for t in d_t if t.exit_reason == "TP"]
        wr   = len(d_tp)/len(d_t)*100 if d_t else 0
        print(f"  {direction}: {len(d_t)} trades | {len(d_tp)} TP | WR={wr:.1f}%")

    # By quarter
    print()
    print("  By quarter:")
    quarters = {}
    for t in closed:
        q = f"{t.entry_ts.year}-Q{(t.entry_ts.month-1)//3+1}"
        quarters.setdefault(q, []).append(t)
    for q in sorted(quarters):
        q_t  = quarters[q]
        q_tp = [t for t in q_t if t.exit_reason == "TP"]
        wr   = len(q_tp)/len(q_t)*100 if q_t else 0
        pnl  = sum(t.pnl_pct for t in q_t)
        print(f"    {q}: {len(q_t):3d} trades | WR={wr:5.1f}% | PnL={pnl:+.2f}%")

    # Save full trade log
    log = []
    for t in sorted(all_trades, key=lambda x: x.entry_ts):
        log.append({
            "symbol": t.symbol, "direction": t.direction,
            "entry_ts": t.entry_ts.isoformat(), "exit_ts": t.exit_ts.isoformat() if t.exit_ts else None,
            "entry": t.entry, "tp": t.tp, "sl": t.sl,
            "exit_price": t.exit_price, "exit_reason": t.exit_reason,
            "pnl_pct": round(t.pnl_pct, 3), "confidence": t.conf,
            "reasons": t.reasons
        })
    with open("/tmp/backtest_results.json", "w") as f:
        json.dump(log, f, indent=2)
    print(f"\nFull trade log saved to /tmp/backtest_results.json ({len(log)} trades)")

if __name__ == "__main__":
    asyncio.run(main())
