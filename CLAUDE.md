# OI Trading Bot (crypto-oi-strategy) — CLAUDE.md

## CRITICAL: Communication & Output Rules

### Token-Saving Communication
- NO preamble. No "Great question!", "Certainly!", "Here's what I found:", "Let me analyze..."
- Lead with the answer, the code, or the action. Not the explanation.
- Show only the relevant diff or function — never the whole file unless asked.
- Explain only if something is non-obvious or I specifically ask. 1-2 sentences max.
- If something is a rule backed by evidence, state it and move on. Don't debate it.
- When I say "yes" or "do it", proceed without asking for confirmation.

### SSH Rules (reinforcing the mandatory workflow above)
- NEVER make file edits directly on VPS via SSH — the workflow below is MANDATORY.
- SSH is ONLY for: git pull + systemctl restart (step 5), status check (step 6), or SQL queries.
- If any SSH command fails, DO NOT retry automatically. Report the error and wait for my input.

### Session Efficiency
- Reference the evidence in this file — don't re-analyze or re-litigate settled decisions.
- The "WHY THESE RULES EXIST" and "DO NOT CHANGE" sections are non-negotiable unless I explicitly ask.

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
**Always update `main.py` CONFIG dict, NOT `strategy.py` defaults.**
`strategy.py` defaults are NEVER reached — `main.py` always passes explicit values via CONFIG dict.
To verify live settings: `curl http://localhost:8000/api/config`

## Current Live Settings (as of 2026-05-05)
| Parameter | Value | Location |
|-----------|-------|----------|
| v2_sr symbols | **ETHUSDT, AVAXUSDT, LTCUSDT** | main.py CONFIG["symbols"] |
| v3_ema symbols | **ETHUSDT, AVAXUSDT only** | main.py EMA_SYMBOLS |
| v2_sr position size | **$100** | main.py CONFIG |
| v3_ema position size | **$50** | main.py EMA_POSITION_SIZE |
| v2_sr min_confidence | **75** | main.py CONFIG / paper_trader.py |
| v3_ema min_confidence | **80** | paper_trader.py (hardcoded, not CONFIG) |
| v2_sr SL/TP | **dynamic** from sweep wick | strategy.py |
| v3_ema SL/TP | **dynamic** 1.5:1 R:R from bar low/high | strategy_ema.py |
| v3_ema 30-min cooldown | same-direction re-entry blocked after any close | paper_trader.py |
| Telegram SR alerts | **DISABLED** — sr_alerts.py exists but check_sr_alerts() call removed from run_strategy_cycle() | sr_alerts.py / main.py |

## DB Column Names (important for SQL queries)
`pnl_usd`, `pnl_pct`, `opened_at`, `closed_at`, `strategy_version`, `confidence`, `exit_reason`
NOT: `pnl`, `created_at`. Use cmd (not PowerShell) for SSH piping — PS breaks on `<` and `*`.

## Live Performance (as of 2026-05-05)
### v3_ema — 49 trades total
| Symbol | Trades | WR% | PnL | Status |
|--------|--------|-----|-----|--------|
| AVAX | 22 | 63.6% | +$2.10 | ✅ Star |
| ETH | 16 | 50.0% | +$0.56 | ✅ |
| LTC | 11 | 27.3% | -$0.57 | ❌ CUT |
- LONG: 58.6% WR, +$2.70 | SHORT: 40.0% WR, -$0.61 (borderline — monitor)
- Post quality-gates (May 4+): 44.4% WR / 18 trades
- New scoring only (May 5+): 55.6% WR / 9 trades — strong early signal

### v2_sr — 13 trades total (too few for conclusions)
| Symbol | Trades | WR% | PnL |
|--------|--------|-----|-----|
| LTC | 5 | 60.0% | +$3.82 | ✅ Best |
| ETH | 4 | 50.0% | +$4.05 | ✅ |
| AVAX | 4 | 25.0% | -$0.35 | ⚠️ Watch |

## Key Architecture Facts
- **Confidence=100 in v3_ema is expected** — max possible score is 125 (capped at 100). When market trends cleanly all 5 dimensions fire. Not a bug. Range 75–100 seen in live data.
- **had_recent_sl() is strategy-aware** (fixed 2026-05-05) — each strategy's SL cooldown only scans its own trades. Bug was: v3_ema SL was blocking v2_sr same-direction entries.
- **LTC removed from v3_ema only** — kept in v2_sr (60% WR there). EMA_SYMBOLS controls v3_ema symbols independently of CONFIG["symbols"].
- **v3_ema CVD is NOT a gate** — CVD is naturally negative during pullbacks. Adding CVD gate killed 100% of LONG setups in early testing. Do NOT add it back.
- **v3_ema S/R-based TP was backtested and rejected** — 4H levels dropped WR 37%→22%. Fixed 1.5:1 is correct for 5m scalp natural move size.
- **BTC fetched but not traded** — still in binance_client. If BTC macro filter needed, inject btc_htf_klines into alt market_data in main.py.
- **sr_alerts.py is DISABLED** — file exists with full logic (4H/Daily pivot proximity, signal summary, trade bias). Was too noisy. Removed from run_strategy_cycle() in main.py. Re-enable by adding `await check_sr_alerts(symbol, market_data, ...)` back into the loop. Uses same TELEGRAM_TOKEN/TELEGRAM_CHAT creds as paper_trader.py.

## What to Watch Next
- v3_ema: 30+ post-LTC-removal trades (ETH+AVAX only) before any conclusions
- v3_ema SHORTs: cut if WR stays below 40% after 20 more trades
- v2_sr AVAX: 25% WR on 4 trades — watch after 15 trades (target ≥50%)
- v2_sr overall: need 20+ trades per symbol before any strategy changes
- Do NOT increase v3_ema position size ($50→$100) until 50+ post-gates trades analyzed
- **SR alert → shadow trade strategy**: deferred. Use alerts to manually observe 15m confluence (engulfing, OI, CVD) at 4H/Daily levels for 2-3 weeks before coding. Not enough evidence yet to justify a third strategy.

## DO NOT CHANGE (evidence-locked)
- LTC in v3_ema — 27.3% WR / 11 trades is definitive
- v3_ema CVD gate — kills LONG setups, backtested and rejected
- v3_ema S/R TP — backtested and rejected (WR 37%→22%)
- v3_ema R:R below 1.5:1 — backtest showed 1.5 matches natural 5m move size
- v2_sr R:R 2.5:1 — at 37% WR, 2.5:1 EV = +0.35%/trade vs 2:1 = +0.13%/trade
- Add SOLUSDT — 0/8 all-time is definitive
- Add XAUUSDT — no Binance Futures perpetual contract, no OI/funding/taker data available

## Deferred (do not implement until criteria met)
- **Partial close** (50% at S/R + breakeven SL + runner) — deferred, needs base strategy validated
- **Self-learning / mistake logging** — deferred, need more live trades to define the patterns
- **v3_ema position size $50→$100** — after 50+ post-gates trades
- **v2_sr ATR dynamic SL** — after live WR > 40% for 20+ trades
