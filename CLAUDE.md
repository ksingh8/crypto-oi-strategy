# OI Trading Bot (crypto-oi-strategy) — CLAUDE.md

## What This Is
Crypto futures trading bot using Open Interest, CVD divergence, RSI, and taker flow signals. Trades ETH and AVAX on Binance (v3_ema), ETH/AVAX/LTC (v2_sr).

## VPS & Access
- **Server:** 46.62.246.19 (root SSH key)
- **Bot path:** `/root/crypto-oi-strategy/`
- **Entry point:** `/root/crypto-oi-strategy/backend/strategy.py` (logic), `main.py` (runner)
- **Service:** `systemctl restart trading-bot` / `systemctl status trading-bot`
- **DB:** `/root/crypto-oi-strategy/backend/trading.db` (table: `trades`)
- **Dashboard:** https://trading.kuldeeps.com (Cloudflare Access)
- **Fallback:** http://46.62.246.19:8082 → localhost:8000
- **GitHub:** https://github.com/ksingh8/crypto-oi-strategy

## ⚠️ MANDATORY DEVELOPMENT WORKFLOW — NEVER BYPASS
All code changes MUST follow this workflow. No exceptions, even for small fixes.

### Local repo path
`G:\My Drive\projects\crypto-oi-strategy\`

### Step 1 — Sync before starting
```
git -C "G:\My Drive\projects\crypto-oi-strategy" pull origin master
```

### Step 2 — Make all changes locally
Edit files in `G:\My Drive\projects\crypto-oi-strategy\backend\` directly.
NEVER edit files directly on VPS via SSH.

### Step 3 — Review diff before committing
```
git -C "G:\My Drive\projects\crypto-oi-strategy" diff
```
Show the full diff to the user and confirm before proceeding.

### Step 4 — Commit and push
```
git -C "G:\My Drive\projects\crypto-oi-strategy" add backend/<changed_files>
git -C "G:\My Drive\projects\crypto-oi-strategy" commit -m "descriptive-message"
git -C "G:\My Drive\projects\crypto-oi-strategy" push origin master
```

### Step 5 — Deploy to VPS
```
ssh root@46.62.246.19 "cd /root/crypto-oi-strategy && git pull origin master && systemctl restart trading-bot"
```

### Step 6 — Verify
```
ssh root@46.62.246.19 "sleep 3 && systemctl status trading-bot --no-pager | head -5"
```

### Trade data analysis (no SSH needed)
Run SQL locally in cmd, save to G: drive, Claude reads directly:
```
ssh root@46.62.246.19 "sqlite3 /root/crypto-oi-strategy/backend/trading.db" < "G:\My Drive\projects\crypto-oi-strategy\analyze.sql" > "G:\My Drive\projects\crypto-oi-strategy\trade_analysis.txt"
```

## ⚠️ CRITICAL ARCHITECTURE NOTE
**Always update `main.py` CONFIG dict (lines 21–27), NOT `strategy.py` defaults.**
`strategy.py` defaults like `config.get("sl_pct", 1.2)` are NEVER reached — `main.py` always passes explicit values via the CONFIG dict.
To verify live settings: `curl http://localhost:8000/api/config`

## Current Live Settings (as of 2026-04-21)
| Parameter | Value | Location |
|-----------|-------|----------|
| SL % | **1.2%** | main.py CONFIG dict |
| TP % | **3.0%** | main.py CONFIG dict |
| R:R | **2.5:1** | intentional — do NOT change to 2:1 |
| Min confidence (LONG) | **65** | main.py CONFIG dict |
| Min confidence (SHORT) | **75** | strategy.py MIN_SHORT_CONFIDENCE |
| Symbols | **BTCUSDT, ETHUSDT only** | main.py |
| Bad hours (UTC) | 0,2,4,5,11,16,17,22 | main.py BAD_HOURS_UTC |

## Active Quality Gates (all in strategy.py unless noted)
| Gate | Rule |
|------|------|
| RSI gate (LONG) | Block RSI≥80; block RSI 62–80 without bullish funding; block RSI 40–50 dead zone |
| RSI gate (SHORT) | Block RSI≤20; block RSI 20–38 without bearish funding |
| Taker spike gate | Taker ratio ≥3.0x → ignored (panic pump, not sustained pressure) |
| Taker entry floor | LONG blocked if taker < 1.3 (no real buyers = falling knife) |
| CVD divergence gate | Bullish div ignored if taker < 0.80; bearish div ignored if taker > 1.20 |
| Confirmation candle | LONG blocked if last 5m bar is red (close ≤ open) |
| BTC macro filter | AVAX/LTC LONG blocked if BTC 4H EMA50 < EMA200 |
| SHORTs disabled | Hard-blocked — historical SHORT WR = 13% (3/22 trades) |
| Time filter | Entire signal cycle skipped during BAD_HOURS_UTC |
| SL cooldown | 30 min cooldown per symbol after SL hit |
| HTF trend filter | 4H EMA50 vs EMA200 directional filter |

## Why These Rules Exist (Evidence-Based)
- **R:R 2.5:1 not 2:1:** At 37% WR, 2.5:1 EV = +0.35%/trade vs 2:1 = +0.13%/trade
- **Time filter:** 25/25 bad-hour trades hit SL, 0 TP — overwhelming evidence
- **No SOLUSDT:** 0/8 all-time (4 LONG + 4 SHORT) — every single one lost
- **Taker spike cap ≥3.0x:** All 5 spike trades hit SL — panic pump not sustained
- **SHORTs disabled:** 13% WR (3/22) vs 36% for LONGs — fundamentally a LONG strategy
- **Conf floor 65:** conf=55 trades have 31% WR; conf≥65 has 37% WR and +0.25% EV/trade

## Winner Profile (from 68-trade analysis)
- Avg RSI: 43.5, Median RSI: 37.5 (oversold territory)
- Taker sweet spot: 1.5–3.0x
- ETH wins: 13, BTC wins: 7, SOL wins: 0
- Best LONG RSI range: 30–40 (66% WR)

## DO NOT CHANGE (without 20+ trades of new evidence)
- Min confidence below 65 for LONGs
- SHORT confidence below 75
- Add SOLUSDT — 0/8 all-time is definitive
- Change R:R to 2:1 — analysed and rejected 2026-04-16
- Remove time filter — 25 bad-hour SLs, 0 TPs
- Widen taker cap above 3.0x — all 5 spike trades lost
- Remove confirmation candle gate — zero-cost filter
- Remove BTC macro filter — alts bleed in BTC downtrends
- Re-enable SHORTs — 13% WR is definitive

## Deferred Improvements (do not implement until WR criteria met)
- **ATR dynamic SL** — deferred until live WR > 40% (backtest avg loss went -1.20% → -2.33% with 4h ATR)
- **Partial TP** (TP1=1.5% at 50%, SL→BE, TP2=3.0%) — deferred until live WR > 45%

## What to Watch
- After ~15–20 new trades under v3 gates, re-analyse WR (target: ≥45%)
- If WR > 45%: implement partial TP (#4)
- If WR > 40% for 20+ trades: implement ATR dynamic SL (#2)
- If WR stays below 35%: investigate if SL 1.2% is still too tight

## BTC Macro Filter Architecture
BTC htf_klines injected into alt market_data by `main.py run_strategy_cycle`. Loop processes BTCUSDT first, caches its htf_klines, injects for AVAX/LTC. Zero extra API calls.
