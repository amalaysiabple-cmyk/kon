# CRS-Futures Calculator

An on-demand trade calculator for Binance USDT-margined perpetuals
(BTCUSDT, ETHUSDT, SOLUSDT, TRXUSDT, DOGEUSDT, ADAUSDT). Pick a pair, the
bot fetches live public data, runs the **Chronoflux Resonance Strategy**
gate, and prints a complete trade plan: entry, stop loss, take profit,
leverage, order type, and confidence score.

> This is a **calculator**, not an auto-trader. There is no signing logic,
> no API key requirement, and no order endpoint anywhere in the code.
> Every setup must be verified manually before trading.

## Features

- 4-pillar quadruple gate: LRM, FBPD, MP, MTS — all must pass
- Pure-Python pillars: math is testable without mocking the network
- Live OI, funding, basis, depth, aggTrades + 60s WebSocket liquidation buffer
- CLI menu (rich) **and** Telegram bot (inline keyboard); pick one or run both
- All thresholds in `config/thresholds.yaml` — zero magic numbers in code
- Every calculation written as one JSON line in `logs/calculations.jsonl`

## Setup

```bash
git clone <this repo>
cd crs_calculator
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in TELEGRAM_TOKEN if using telegram mode
```

Or just `./run.sh` — it bootstraps the venv on first run.

### Telegram bot setup

1. Open Telegram, message `@BotFather`, `/newbot`, follow the prompts
2. Copy the token into `.env` as `TELEGRAM_TOKEN=...`
3. `./run.sh --mode telegram`
4. Open the bot in Telegram, send `/start`, tap a pair button

### Running

```bash
./run.sh --mode cli         # interactive menu (default)
./run.sh --mode telegram    # Telegram bot polling
./run.sh --mode both        # both, in the same event loop
```

## Threshold tuning

`config/thresholds.yaml` is grouped per-pair. The most common knobs:

- `lrm.min_cluster_density` — raise to demand stronger liquidation magnets
- `lrm.proximity_band_atr` — how close (in ATR units) the cluster must sit
- `lrm.min_oi_usd` — per-pair OI floor (USD)
- `fbpd.min_divergence` — Itakura-Saito divergence threshold (perp vs basis)
- `mp.gate` — composite microstructure gate (0..1)
- `mp.weights.*` — per-component weights inside the MP score
- `mts.block_before_minutes / block_after_minutes` — hard-block window
  around scheduled high-impact events (CPI/FOMC/NFP/PCE/PPI)

Update `config/events.yaml` whenever the macro calendar changes — the file
ships with a default schedule but you'll need to keep it current.

## Testing

```bash
pytest
```

All four pillars and the trade plan have synthetic-data unit tests; none
of them hit the network.

## Sample output

### Valid setup

```
═══════════════════════════════════════
  CRS-FUTURES TRADE PLAN
═══════════════════════════════════════
Pair       : BTCUSDT
Timestamp  : 2026-05-10 14:23:17 UTC
Setup      : ✅ VALID
Confidence : 0.74

DIRECTION  : LONG
Conviction : 0.68

ENTRY
  Type     : LIMIT_POST_ONLY
  Price    : 67,380.50
  Market   : 67,420.00 (slippage: $40.00)

STOP LOSS  : 67,180.00 (-0.30%)

TAKE PROFIT
  TP1      : 67,580.00 | 50% size  (R:R 1.0)
  TP2      : 67,880.00 | 50% size runner  (R:R 2.5)

GATE SCORES
  LRM    : 0.82 ✅ (cluster @ 67,150.00, $145.0M OI, 0.38ATR below)
  FBPD   : -0.0018 ✅ (perp discount, mean revert UP)
  MP     : 0.71 ✅ (CVD +0.412, hazard 2.3/min, book +0.18)
  MTS    : NORMAL ✅ (next event: CPI 2026-05-13 12:30 UTC)
═══════════════════════════════════════
```

### Invalid setup

```
═══════════════════════════════════════
  CRS-FUTURES — NO SETUP
═══════════════════════════════════════
Pair       : ETHUSDT
Timestamp  : 2026-05-10 14:25:01 UTC
Setup      : ❌ INVALID

Failed Gates:
  LRM    : ❌ (distance 1.20ATR > 0.60ATR)
  FBPD   : ✅ (ok)
  MP     : ❌ (score 0.52 < gate 0.65)
  MTS    : ✅ (NORMAL: ok)
═══════════════════════════════════════
```

## Disclaimer

This software is provided for **educational and research purposes only**.
It is **not** financial advice. Cryptocurrency derivatives are extremely
risky and you can lose more than your initial deposit. The author makes no
guarantee of profitability and accepts no liability for any losses
incurred from acting on the calculator's output. **Always verify every
setup manually**, manage your own risk, and never trade with money you
cannot afford to lose.

## Project layout

```
crs_calculator/
├── config/                # thresholds + macro events (YAML)
├── src/
│   ├── core/              # config loader, JSONL logger, math helpers
│   ├── data/              # async Binance REST client + WS liq buffer
│   ├── pillars/           # LRM, FBPD, MP, MTS
│   ├── decision/          # gate, direction, trade_plan
│   ├── interface/         # CLI and Telegram bot
│   └── main.py            # mode dispatcher
├── tests/                 # pytest suite (no network)
├── logs/                  # JSONL calculation history
└── run.sh                 # convenience launcher
```
