#!/usr/bin/env python3
"""
Backtest v2 — compares current strategy vs 5 improvements:
  1. Confirmation candle (signal bar must be green before entry)
  2. ATR-based dynamic SL (max(1.2%, ATR14 x 1.5))
  3. Remove SHORTs entirely
  4. Partial TP: TP1=1.5% (50%), move SL to breakeven, TP2=3.0% (50%)
  5. BTC macro filter for AVAX/LTC (block alt longs if BTC 4H bearish)

Run on VPS: python3 backtest_v2.py
"""
import asyncio, httpx, json
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field

# ── Config ─────────────────────────────────────────────────────────────────────
SYMBOLS        = ["BTCUSDT", "ETHUSDT", "AVAXUSDT", "LTCUSDT"]
TP_PCT         = 3.0
TP1_PCT        = 1.5   # partial TP level
SL_PCT         = 1.2
MIN_CONF       = 65
MIN_SHORT_CONF = 75
BAD_HOURS_UTC  = {0, 2, 4, 5, 11, 16, 17, 22}
BASE           = "https://fapi.binance.com"
MONTHS_BACK    = 16

@dataclass
class Bar:
    ts: datetime
    open: float; high: float; low: float; close: float
    volume: float; taker_buy_vol: float; quote_vol: float; taker_buy_quote_vol: float

@dataclass
class Trade:
    symbol: str; direction: str; entry: float
    tp: float; sl: float; tp1: float
    entry_ts: datetime; conf: int; reasons: list = field(default_factory=list)
    exit_price: float = 0; exit_reason: str = ""; exit_ts: datetime = None
    pnl_pct: float = 0

# ── Fetch helpers ──────────────────────────────────────────────────────────────
async def fetch_klines(client, symbol, interval, start_ts, end_ts):
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
        current = data[-1][6] + 1
        await asyncio.sleep(0.05)
    return bars

async def fetch_funding_history(client, symbol, start_ts):
    records = {}
    current = int(start_ts.timestamp() * 1000)
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
    return records

# ── Indicators ─────────────────────────────────────────────────────────────────
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
    for c in closes[period:]: ema = c * k + ema * (1 - k)
    return ema

def calc_atr(bars, period=14):
    """Wilder's ATR from bar OHLC."""
    if len(bars) < period + 1:
        return bars[-1].close * 0.012
    trs = []
    for i in range(1, len(bars)):
        tr = max(bars[i].high - bars[i].low,
                 abs(bars[i].high - bars[i-1].close),
                 abs(bars[i].low  - bars[i-1].close))
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr

def get_funding_rate(funding_records, bar_ts):
    candidates = [v for t, v in funding_records.items() if t <= bar_ts]
    return candidates[-1] if candidates else 0.0

# ── Signal generation (same for both versions) ─────────────────────────────────
def generate_signal(bar_idx, h4_bars, h1_bars, funding_records, btc_h4_bars=None, symbol=""):
    bar = h4_bars[bar_idx]
    if bar.ts.hour in BAD_HOURS_UTC:
        return "NEUTRAL", 0, [], None

    if bar_idx < 50: return "NEUTRAL", 0, [], None

    h4_closes = [b.close for b in h4_bars[:bar_idx+1]]
    if len(h4_closes) < 200: return "NEUTRAL", 0, [], None

    ema50  = calc_ema(h4_closes[-200:], 50)
    ema200 = calc_ema(h4_closes[-200:], 200)
    htf_trend = "bullish" if ema50 > ema200 else "bearish"

    h1_aligned = [b for b in h1_bars if b.ts <= bar.ts]
    rsi = calc_rsi([b.close for b in h1_aligned[-28:]], period=14)

    recent_bars = h4_bars[max(0, bar_idx-23):bar_idx+1]
    taker_vals = []
    for b in recent_bars:
        if b.quote_vol > 0:
            r = b.taker_buy_quote_vol / b.quote_vol
            taker_vals.append(r / (1 - r) if r < 1 else r / max(1 - r, 1e-9))
    taker_ratio = taker_vals[-1] if taker_vals else 1.0

    funding_rate = get_funding_rate(funding_records, bar.ts)
    if   funding_rate < -0.0050: funding_sig = "bullish_extreme"
    elif funding_rate < -0.0010: funding_sig = "bullish_mild"
    elif funding_rate >  0.0050: funding_sig = "bearish_extreme"
    elif funding_rate >  0.0010: funding_sig = "bearish_mild"
    else:                        funding_sig = "neutral"
    funding_bullish = funding_sig in ("bullish_mild", "bullish_extreme")
    funding_bearish = funding_sig in ("bearish_mild", "bearish_extreme")

    cvd_bars = h4_bars[max(0, bar_idx-19):bar_idx+1]
    cvd = sum(b.taker_buy_quote_vol - (b.quote_vol - b.taker_buy_quote_vol) for b in cvd_bars)
    price_change = cvd_bars[-1].close - cvd_bars[0].close if cvd_bars else 0
    if   price_change < 0 and cvd > 0: delta_signal = "bullish_divergence"
    elif price_change > 0 and cvd < 0: delta_signal = "bearish_divergence"
    elif cvd > 0:                       delta_signal = "bullish_confirm"
    elif cvd < 0:                       delta_signal = "bearish_confirm"
    else:                               delta_signal = "neutral"
    delta_score = 15

    long_score = 0; short_score = 0; long_sigs = 0; short_sigs = 0; reasons = []

    if htf_trend == "bullish":
        long_score += 20; long_sigs += 1
    else:
        short_score += 20; short_sigs += 1

    if funding_bullish:
        long_score += (20 if "extreme" in funding_sig else 10); long_sigs += 1
    elif funding_bearish:
        short_score += (20 if "extreme" in funding_sig else 10); short_sigs += 1
    else:
        long_score += 5

    if   rsi < 30: long_score  += 30; long_sigs  += 1
    elif rsi < 40: long_score  += 20; long_sigs  += 1
    elif rsi < 50: long_score  += 10
    elif rsi > 70: short_score += 30; short_sigs += 1
    elif rsi > 60: short_score += 20; short_sigs += 1
    elif rsi > 50: short_score += 10

    if taker_ratio >= 3.0:
        pass  # spike ignored
    elif taker_ratio >= 1.5:
        score = 30 if taker_ratio >= 2.0 else 20
        long_score += score; long_sigs += 1
    elif taker_ratio <= 0.67:
        score = 30 if taker_ratio <= 0.50 else 20
        short_score += score; short_sigs += 1

    if   delta_signal == "bullish_confirm":    long_score  += delta_score; long_sigs  += 1
    elif delta_signal == "bearish_confirm":    short_score += delta_score; short_sigs += 1
    elif delta_signal == "bullish_divergence":
        if taker_ratio >= 0.80: long_score  += delta_score; long_sigs  += 1
    elif delta_signal == "bearish_divergence":
        if taker_ratio <= 1.20: short_score += delta_score; short_sigs += 1

    MIN_SIGNALS = 2
    long_ok  = (long_score  > short_score and long_score  >= MIN_CONF
                and long_sigs  >= MIN_SIGNALS and htf_trend != "bearish")
    short_ok = (short_score > long_score  and short_score >= MIN_SHORT_CONF
                and short_sigs >= MIN_SIGNALS and htf_trend != "bullish")

    if long_ok:
        if   rsi >= 80: long_ok = False
        elif rsi >= 62 and not funding_bullish: long_ok = False
        elif 40 <= rsi < 50: long_ok = False
        # NOTE: taker floor (< 1.3x) intentionally skipped in backtest
        # 4h taker averages ~1.0x (buy/sell cancel out). This gate only works on live 5m data.

    if short_ok and short_score < MIN_SHORT_CONF: short_ok = False

    if   long_ok:  return "LONG",  min(long_score,  100), reasons, h4_bars[bar_idx]
    elif short_ok: return "SHORT", min(short_score, 100), reasons, h4_bars[bar_idx]
    else:          return "NEUTRAL", max(long_score, short_score), reasons, None

# ── Simulation ─────────────────────────────────────────────────────────────────
def simulate(signals, h4_bars, use_improvements, btc_h4_bars=None, symbol=""):
    """
    use_improvements=False → current strategy
    use_improvements=True  → all 5 improvements applied
    """
    trades = []
    next_allowed_bar = 0
    MAX_HOLD = 100

    for bar_idx, direction, conf, reasons, signal_bar in signals:
        if bar_idx < next_allowed_bar: continue
        if direction == "SHORT" and use_improvements: continue  # Improvement 3: no shorts

        bar = h4_bars[bar_idx]
        entry = bar.close

        # ── Improvement 1: Confirmation candle ──
        if use_improvements and direction == "LONG":
            if bar.close <= bar.open:  # red candle = still falling, skip
                continue

        # ── Improvement 2: ATR-based dynamic SL ──
        if use_improvements:
            recent = h4_bars[max(0, bar_idx-14):bar_idx+1]
            atr = calc_atr(recent)
            atr_sl_pct = (atr * 1.5 / entry) * 100
            sl_pct_used = max(SL_PCT, min(atr_sl_pct, 3.0))  # cap at 3% max
        else:
            sl_pct_used = SL_PCT

        # ── Improvement 5: BTC macro filter for alts ──
        if use_improvements and symbol not in ("BTCUSDT", "ETHUSDT") and btc_h4_bars:
            btc_closes = [b.close for b in btc_h4_bars if b.ts <= bar.ts]
            if len(btc_closes) >= 200:
                btc_ema50  = calc_ema(btc_closes[-200:], 50)
                btc_ema200 = calc_ema(btc_closes[-200:], 200)
                if btc_ema50 < btc_ema200:  # BTC bearish, skip alt long
                    continue

        if direction == "LONG":
            tp_price  = entry * (1 + TP_PCT / 100)
            tp1_price = entry * (1 + TP1_PCT / 100)
            sl_price  = entry * (1 - sl_pct_used / 100)
        else:
            tp_price  = entry * (1 - TP_PCT / 100)
            tp1_price = entry * (1 - TP1_PCT / 100)
            sl_price  = entry * (1 + sl_pct_used / 100)

        trade = Trade(symbol=symbol, direction=direction, entry=entry,
                      tp=tp_price, sl=sl_price, tp1=tp1_price,
                      entry_ts=bar.ts, conf=conf, reasons=reasons)

        hit = False
        tp1_hit = False
        be_sl = entry  # breakeven SL after TP1

        for j, fb in enumerate(h4_bars[bar_idx+1 : bar_idx+MAX_HOLD+1]):
            exit_bar_idx = bar_idx + 1 + j

            if direction == "LONG":
                if not tp1_hit:
                    if use_improvements and fb.high >= tp1_price and fb.low <= sl_price:
                        # Both in same candle — use open direction
                        if fb.open > entry:
                            tp1_hit = True; continue
                        else:
                            trade.exit_price = sl_price; trade.exit_reason = "SL"
                            trade.pnl_pct = -sl_pct_used
                    elif fb.low <= sl_price:
                        trade.exit_price = sl_price; trade.exit_reason = "SL"
                        trade.pnl_pct = -sl_pct_used
                    elif use_improvements and fb.high >= tp1_price:
                        tp1_hit = True; continue  # TP1 hit, ride to TP2
                    elif not use_improvements and fb.high >= tp_price:
                        trade.exit_price = tp_price; trade.exit_reason = "TP"
                        trade.pnl_pct = TP_PCT
                    else:
                        continue
                else:
                    # TP1 already hit — SL moved to breakeven
                    if fb.low <= be_sl:
                        trade.exit_price = be_sl; trade.exit_reason = "TP1_BE"
                        trade.pnl_pct = 0.5 * TP1_PCT + 0.5 * 0  # half at TP1, half at BE
                    elif fb.high >= tp_price:
                        trade.exit_price = tp_price; trade.exit_reason = "TP1+TP2"
                        trade.pnl_pct = 0.5 * TP1_PCT + 0.5 * TP_PCT  # both targets
                    else:
                        continue
            else:  # SHORT (only in non-improved version)
                if fb.high >= sl_price:
                    trade.exit_price = sl_price; trade.exit_reason = "SL"
                    trade.pnl_pct = -sl_pct_used
                elif fb.low <= tp_price:
                    trade.exit_price = tp_price; trade.exit_reason = "TP"
                    trade.pnl_pct = TP_PCT
                else:
                    continue

            trade.exit_ts = fb.ts
            next_allowed_bar = exit_bar_idx + 1
            hit = True; break

        if not hit:
            close_bar = h4_bars[min(bar_idx + MAX_HOLD, len(h4_bars)-1)]
            next_allowed_bar = min(bar_idx + MAX_HOLD, len(h4_bars)-1) + 1
            trade.exit_reason = "TIMEOUT"
            trade.exit_ts = close_bar.ts
            trade.exit_price = close_bar.close
            if tp1_hit:
                mk_pnl = (close_bar.close - entry) / entry * 100
                trade.pnl_pct = 0.5 * TP1_PCT + 0.5 * mk_pnl
            else:
                trade.pnl_pct = (close_bar.close - entry) / entry * 100 * (1 if direction=="LONG" else -1)

        trades.append(trade)
    return trades

# ── Stats printer ──────────────────────────────────────────────────────────────
def print_stats(label, all_trades, symbols):
    closed = [t for t in all_trades if t.exit_reason not in ("TIMEOUT",)]
    tp_trades = [t for t in closed if "TP" in t.exit_reason]
    sl_trades = [t for t in closed if t.exit_reason == "SL"]
    total = len(closed)
    if not total:
        print(f"\n{label}: No closed trades"); return

    wr  = len(tp_trades) / total * 100
    pnl = sum(t.pnl_pct for t in closed)
    avg_win  = sum(t.pnl_pct for t in tp_trades) / len(tp_trades) if tp_trades else 0
    avg_loss = sum(t.pnl_pct for t in sl_trades) / len(sl_trades) if sl_trades else 0
    ev = (len(tp_trades)/total * avg_win) + (len(sl_trades)/total * avg_loss) if total else 0

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Trades:    {total} ({len(tp_trades)} TP / {len(sl_trades)} SL)")
    print(f"  Win rate:  {wr:.1f}%")
    print(f"  Avg win:   +{avg_win:.2f}%   Avg loss: {avg_loss:.2f}%")
    print(f"  EV/trade:  {ev:.3f}%")
    print(f"  Total PnL: {pnl:+.2f}%")
    print()
    for sym in symbols:
        sym_t  = [t for t in closed if t.symbol == sym]
        sym_tp = [t for t in sym_t  if "TP" in t.exit_reason]
        if not sym_t: continue
        sym_pnl = sum(t.pnl_pct for t in sym_t)
        print(f"  {sym:12s}: {len(sym_t):3d} trades | {len(sym_tp):2d} TP | WR={len(sym_tp)*100//len(sym_t):3d}% | PnL={sym_pnl:+.2f}%")
    print()
    for direction in ["LONG", "SHORT"]:
        d_t  = [t for t in closed if t.direction == direction]
        d_tp = [t for t in d_t if "TP" in t.exit_reason]
        if d_t:
            print(f"  {direction:6s}: {len(d_t)} trades | WR={len(d_tp)*100//len(d_t)}%")
    print()
    for q in sorted({f"{t.entry_ts.year}-Q{(t.entry_ts.month-1)//3+1}" for t in closed}):
        q_t  = [t for t in closed if f"{t.entry_ts.year}-Q{(t.entry_ts.month-1)//3+1}" == q]
        q_tp = [t for t in q_t if "TP" in t.exit_reason]
        q_pnl = sum(t.pnl_pct for t in q_t)
        print(f"  {q}: {len(q_t):3d} trades | WR={len(q_tp)*100//len(q_t) if q_t else 0:3d}% | PnL={q_pnl:+.2f}%")

# ── Main ────────────────────────────────────────────────────────────────────────
async def main():
    end_ts   = datetime.now(timezone.utc)
    start_ts = end_ts - timedelta(days=int(MONTHS_BACK * 30.5))
    print(f"Backtest period: {start_ts.strftime('%Y-%m-%d')} → {end_ts.strftime('%Y-%m-%d')}")
    print(f"Symbols: {SYMBOLS}  TP={TP_PCT}%  TP1={TP1_PCT}%  SL={SL_PCT}%  MinConf={MIN_CONF}\n")

    async with httpx.AsyncClient(timeout=30) as client:
        # Fetch BTC data once for macro filter
        print("[BTCUSDT] Fetching for macro filter...")
        btc_h4 = await fetch_klines(client, "BTCUSDT", "4h", start_ts, end_ts)

        all_current = []; all_improved = []

        for symbol in SYMBOLS:
            print(f"[{symbol}] Fetching data...")
            h4_bars = await fetch_klines(client, symbol, "4h", start_ts, end_ts)
            h1_bars = await fetch_klines(client, symbol, "1h", start_ts, end_ts)
            funding = await fetch_funding_history(client, symbol, start_ts)

            if not h4_bars:
                print(f"[{symbol}] No data, skipping"); continue

            print(f"[{symbol}] {len(h4_bars)}x4h | {len(h1_bars)}x1h | {len(funding)} funding records")

            # Generate all signals
            signals = []
            for i in range(len(h4_bars)):
                direction, conf, reasons, sig_bar = generate_signal(
                    i, h4_bars, h1_bars, funding,
                    btc_h4_bars=btc_h4 if symbol not in ("BTCUSDT","ETHUSDT") else None,
                    symbol=symbol
                )
                if direction != "NEUTRAL":
                    signals.append((i, direction, conf, reasons, sig_bar))

            print(f"[{symbol}] {len(signals)} raw signals")

            # Version A: current
            trades_a = simulate(signals, h4_bars, use_improvements=False,
                                btc_h4_bars=btc_h4, symbol=symbol)
            for t in trades_a: t.symbol = symbol
            all_current.extend(trades_a)

            # Version B: all 5 improvements
            trades_b = simulate(signals, h4_bars, use_improvements=True,
                                btc_h4_bars=btc_h4, symbol=symbol)
            for t in trades_b: t.symbol = symbol
            all_improved.extend(trades_b)

            print(f"[{symbol}] A={len(trades_a)} trades  B={len(trades_b)} trades")

    print_stats("VERSION A — Current Strategy (baseline)", all_current, SYMBOLS)
    print_stats("VERSION B — With All 5 Improvements",     all_improved, SYMBOLS)

    # Side-by-side summary
    def summary(trades):
        closed = [t for t in trades if t.exit_reason not in ("TIMEOUT",)]
        tp = [t for t in closed if "TP" in t.exit_reason]
        sl = [t for t in closed if t.exit_reason == "SL"]
        if not closed: return "0 trades"
        wr  = len(tp)*100//len(closed)
        pnl = sum(t.pnl_pct for t in closed)
        ev  = pnl / len(closed)
        return f"{len(closed)} trades | WR={wr}% | PnL={pnl:+.2f}% | EV={ev:+.3f}%"

    print("\n" + "="*60)
    print("  COMPARISON SUMMARY")
    print("="*60)
    print(f"  Current:     {summary(all_current)}")
    print(f"  Improved:    {summary(all_improved)}")

    # Save
    with open("/tmp/backtest_v2.json", "w") as f:
        json.dump({
            "current":  [{"symbol":t.symbol,"direction":t.direction,"entry_ts":t.entry_ts.isoformat(),
                          "pnl_pct":round(t.pnl_pct,3),"exit_reason":t.exit_reason,"conf":t.conf} for t in all_current],
            "improved": [{"symbol":t.symbol,"direction":t.direction,"entry_ts":t.entry_ts.isoformat(),
                          "pnl_pct":round(t.pnl_pct,3),"exit_reason":t.exit_reason,"conf":t.conf} for t in all_improved]
        }, f, indent=2)
    print(f"\nDetailed results saved to /tmp/backtest_v2.json")

if __name__ == "__main__":
    asyncio.run(main())
