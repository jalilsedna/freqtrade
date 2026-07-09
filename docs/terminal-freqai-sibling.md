# Market-Terminal FreqAI sibling (crypto)

This fork carries a small integration that lets a FreqAI model **learn on the signals of the
Market-Terminal** (`jalilsedna/market-terminal`) — the multi-asset signal/research brain —
and trade **crypto** on an exchange. It mirrors the terminal's Intelligent-Trading-Bot
sibling: freqtrade is the more-mature, crypto-native learner/executor.

## What it is

- **`user_data/strategies/terminal_features.py`** — an HTTP provider that pulls the
  terminal's lookahead-free daily feature panel (`GET /factor-history/{symbol}`: SMC
  structure, market profile, price-regime, realized vol, momentum, optional FRED macro) and
  merges the numeric `mt_*` columns onto a strategy dataframe as FreqAI features (`%-mt_*`),
  aligned lookahead-free with `merge_asof(direction="backward")`.
- **`user_data/strategies/TerminalFreqAIStrategy.py`** — a FreqAI strategy that trains on the
  usual candle features **plus** those terminal features, targeting the mean forward return.
- **`user_data/config_terminal_freqai.example.json`** — a crypto, `dry_run` config wired to
  the strategy.

## The boundary (read this)

- **Crypto only.** The terminal generates signals across equities/futures/FX/crypto;
  freqtrade can only trade crypto (CCXT), so this sibling touches crypto pairs and nothing
  else. All other terminal signals are untouched.
- **The terminal stays signal-only.** It never receives an exchange key and never places an
  order. It exposes read-only signal endpoints; this sibling consumes them over HTTP.
- **Exchange/trade keys live ONLY on the freqtrade side** (this config / your `.env`), never
  in the terminal repo — the same key boundary as the Alice and ITB siblings. `dry_run` stays
  `true` until you deliberately go live with your own keys.
- **The signal source is never a hard dependency.** If the terminal is unreachable the merge
  is a no-op and the model trains on candle features alone — a trade never blocks on it.

## Configure

Set these before running (the provider reads them once):

| Env var | Meaning | Default |
|---|---|---|
| `MT_API_URL` | Terminal base URL | `http://127.0.0.1:8000` |
| `MT_API_TOKEN` | `Authorization: Bearer` token (terminal's programmatic gate) | unset (keyless local) |
| `MT_QUOTE` | Fiat leg used to address the terminal (`BTC/USDT` → `BTC-USD`) | `USD` |

## Run

```bash
export MT_API_URL="https://<your-terminal-host>"
export MT_API_TOKEN="<terminal bearer token>"   # omit for keyless local dev

# Backtest (downloads candles as usual; terminal features join by date)
freqtrade backtesting \
  --strategy TerminalFreqAIStrategy \
  --freqaimodel LightGBMRegressor \
  --config user_data/config_terminal_freqai.example.json \
  --timerange 20240101-20240601

# Dry-run live
freqtrade trade \
  --strategy TerminalFreqAIStrategy \
  --freqaimodel LightGBMRegressor \
  --config user_data/config_terminal_freqai.example.json
```

## Deploy as a VPS sibling (Docker + Caddy) + go-live runbook

Run freqtrade as its own always-on container behind the shared Caddy edge — the same pattern
as the OpenAlice / ITB siblings. It stays **disarmed (`dry_run: true`)** until you deliberately
flip it.

```bash
cp .env.terminal-freqai.example .env      # fill in on THIS box only (never commit it)
#   - MT_API_URL / MT_API_TOKEN            → your terminal
#   - FREQTRADE__API_SERVER__USERNAME/PASSWORD + JWT_SECRET_KEY (openssl rand -hex 32)
#   - leave FREQTRADE__DRY_RUN=true and the exchange keys blank for now
docker compose -f docker-compose.terminal-freqai.yml up -d
docker compose -f docker-compose.terminal-freqai.yml logs -f     # watch it train + trade (paper)
```

Caddy site block (gitignored on the VPS, e.g. `deploy/sites/siblings.caddy`):

```
freqtrade.<yourdomain> {
    reverse_proxy terminal-freqai:8080     # FreqUI + REST
}
```

**Connect the terminal's Crypto Bot panel:** in the terminal, set Settings → Crypto Bot →
URL = `https://freqtrade.<yourdomain>`, username/password = the `FREQTRADE__API_SERVER__*`
creds above. The terminal then shows the bot's status/trades/P&L (read-only; it never controls
the bot).

### Staged go-live (do this yourself, deliberately)

1. **Paper (default):** `dry_run: true` — live Binance data, simulated wallet. Watch FreqUI /
   the terminal panel for a few days; confirm the model trains and the `%-mt_*` features carry
   non-zero importance.
2. **Live:** create Binance **spot** API keys — **trade enabled, withdrawals DISABLED,
   IP-restricted to this box**. Put them in `.env` (`FREQTRADE__EXCHANGE__KEY/SECRET`), set
   `FREQTRADE__DRY_RUN=false`, and redeploy. Start with small `stake_amount` and
   `max_open_trades`. Keys never leave this box; the terminal never sees them.

## Notes

- **Pair selection is dynamic (prod config).** `config_terminal_freqai.prod.json` uses a
  `VolumePairList` — top-20 USDT spot pairs by 24h quote volume (refreshed every 30m), filtered
  by age/price/spread/range-stability/volatility, with stablecoins + leveraged tokens
  blacklisted. `stake_amount: "unlimited"` splits the wallet across up to `max_open_trades: 10`
  concurrent positions. To trade a fixed set instead, swap the first pairlist back to
  `StaticPairList` + a `pair_whitelist`. Tune `number_assets` down if the VPS is CPU-strained
  (FreqAI trains a model per pair every `live_retrain_hours`). `include_corr_pairlist` stays
  static (BTC/ETH) as informative anchors. **Terminal `mt_*` features only enrich pairs also in
  the terminal's registry** (others train candle-only, gracefully) — mirror the majors you
  expect to trade into the terminal registry as `BASE-USD`.
- Terminal reads are **daily**; they forward-fill across intraday candles (slower macro /
  structural context, not a per-candle trigger). The categorical label columns
  (`mt_structure_trend`, `mt_price_regime`) are dropped — FreqAI features must be numeric and
  their sign/score is already carried by `mt_structure_bias` / `mt_regime_score`.
- This is a research scaffold, not a tuned live system. Validate a pair's edge (walk-forward)
  before trusting it — the same discipline the terminal applies to its own factors.
