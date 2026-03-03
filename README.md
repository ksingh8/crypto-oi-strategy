# OI Strategy — Crypto Futures Paper Trader

A paper trading bot for crypto futures using Open Interest, funding rate, RSI, and L/S ratio signals. Runs on Railway with a real-time dashboard.

## Strategy Logic

| Indicator | Signal |
|---|---|
| OI rising + accelerating | New money entering (neutral, directional confirmation needed) |
| Funding rate > 0.08% | Crowded longs → SHORT bias |
| Funding rate < -0.03% | Crowded shorts → LONG bias |
| RSI < 35 | Oversold → LONG |
| RSI > 65 | Overbought → SHORT |
| L/S ratio flipping | Trend confirmation |

Signals scored 0-100. Trade opens only when score ≥ `MIN_CONFIDENCE`.

## Local Development

```bash
cd backend
pip install -r ../requirements.txt
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000

## Deploy to Railway

1. Push this repo to GitHub
2. Create new Railway project → **Deploy from GitHub repo**
3. Set environment variables (see `.env.example`)
4. Add a **Volume** mounted at `/app/backend` so `trading.db` persists across deploys
5. Railway auto-detects `railway.toml` — done

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SYMBOLS` | `BTCUSDT,ETHUSDT` | Comma-separated trading pairs |
| `TP_PCT` | `1.5` | Take profit % |
| `SL_PCT` | `0.8` | Stop loss % |
| `MIN_CONFIDENCE` | `45` | Minimum score to open trade |
| `POSITION_SIZE_USD` | `100` | Simulated position size |
| `SIGNAL_INTERVAL` | `5` | Minutes between signal runs |
| `DB_PATH` | `trading.db` | SQLite path |

## Dashboard Features

- Live price + OI charts
- Open positions with TP/SL progress bar
- Full trade history (entry, exit, PnL, reason, duration)
- Signal feed with indicator breakdown
- Live indicators: OI, funding rate, L/S ratio, 24h change
- Stats: win rate, total PnL, best/worst trade
- Manual "Run Now" trigger

## Important Notes

- **No real money** — this is a paper trader. No exchange API keys needed.
- TP/SL are checked on each cycle against the mark price from Binance. This means intra-candle hits won't be detected until the next cycle runs.
- Railway's free tier may sleep the service — use Hobby plan ($5/mo) for continuous operation.
- SQLite is fine for a few weeks of testing. For longer runs, swap for Postgres using Railway's Postgres plugin and update `database.py`.

## Extending

- Add more symbols in `SYMBOLS` env var
- Tune `TP_PCT`, `SL_PCT`, `MIN_CONFIDENCE` to test different risk profiles
- Add liquidation cascade detection in `strategy.py` using `bc.get_liquidations()`
- Swap RSI for EMA crossover in `strategy.py`
