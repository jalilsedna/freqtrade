"""Market-Terminal signal provider for FreqAI strategies (crypto sibling).

This is the seam between **jalilsedna/market-terminal** (the signal/research brain) and this
freqtrade fork (the crypto learner/executor). It pulls the terminal's lookahead-free daily
feature panel — the ``mt_*`` reads from ``GET /factor-history/{symbol}`` (SMC structure,
market profile, price-regime, realized vol, momentum, optional FRED macro) — over HTTP and
merges it onto a strategy's candle dataframe as FreqAI features (``%-mt_*``).

Boundary (mirrors ``docs/intelligent-trading-bot.md`` in the terminal repo):

  * **Crypto only.** The terminal generates multi-asset signals; freqtrade can only trade
    crypto, so this sibling touches crypto pairs and nothing else.
  * **The terminal stays signal-only.** It never sees an exchange key and never places an
    order. Exchange/trade credentials live on the freqtrade side (config / ``.env``) — the
    same key boundary as the Alice / ITB siblings.
  * **Read-only, fault-tolerant.** If the terminal is unreachable the merge is a no-op: the
    model simply trains on the candle-derived features alone. A signal source is never a
    hard dependency of a trade.

Configuration (environment, read once):

  * ``MT_API_URL``   — terminal base URL (default ``http://127.0.0.1:8000``).
  * ``MT_API_TOKEN`` — optional ``Authorization: Bearer`` token (the terminal's programmatic
    gate; unset for keyless local dev).
  * ``MT_QUOTE``     — quote leg used to address the terminal (default ``USD``): a freqtrade
    pair ``BTC/USDT`` maps to the terminal's canonical ``BTC-USD``.

Pure enough to unit-test without freqtrade or a network: the one HTTP call funnels through
``_http_get_json``, which tests monkeypatch.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import date

import pandas as pd


logger = logging.getLogger(__name__)

# Numeric columns of the /factor-history panel worth feeding a model. The two categorical
# label columns (mt_structure_trend, mt_price_regime) are intentionally dropped — FreqAI
# features must be numeric, and their sign/score is already captured by mt_structure_bias /
# mt_regime_score. mt_macro_* appear only when the panel is requested with include_macro.
_NUMERIC_COLS = (
    "mt_structure_bias",
    "mt_profile_bias",
    "mt_regime_score",
    "mt_vol_pct",
    "mt_momentum",
    "mt_realized_vol",
    "mt_macro_dgs10",
    "mt_macro_usdbroad",
)

_DEFAULT_URL = "http://127.0.0.1:8000"
_HTTP_TIMEOUT = 20

# Per-process cache: {(symbol, lookback_days, include_macro, YYYY-MM-DD): DataFrame}. The
# panel is daily and only extends once per day, so one fetch per pair per run is plenty and
# keeps FreqAI's repeated feature passes from hammering the terminal.
_CACHE: dict[tuple, pd.DataFrame] = {}


def _base_url() -> str:
    return (os.getenv("MT_API_URL") or _DEFAULT_URL).rstrip("/")


def pair_to_symbol(pair: str, quote: str | None = None) -> str:
    """Map a freqtrade pair to the terminal's canonical crypto symbol.

    ``BTC/USDT`` , ``BTC/USDT:USDT`` , ``BTC/USD`` all map to ``BTC-USD`` (the dash form the
    terminal's symbol map expects). The quote leg the terminal is addressed with is
    configurable via ``MT_QUOTE`` (default ``USD``) since the terminal keys crypto off fiat
    USD regardless of the stablecoin a venue quotes.
    """
    q = (quote or os.getenv("MT_QUOTE") or "USD").upper()
    base = (pair or "").split("/")[0].strip().upper()
    return f"{base}-{q}"


def _http_get_json(url: str, token: str | None) -> dict:
    """Single HTTP GET returning parsed JSON. Isolated so tests can monkeypatch it. Uses
    stdlib urllib so the provider adds no import-time dependency beyond pandas."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310 — our own URL
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310 — our own URL
        return json.loads(resp.read().decode("utf-8"))


def fetch_panel(
    symbol: str,
    *,
    lookback_days: int = 1460,
    include_macro: bool = False,
) -> pd.DataFrame:
    """Fetch the terminal's ``/factor-history`` panel for a canonical ``symbol`` and return a
    numeric, date-indexed DataFrame (``mt_*`` columns). Empty DataFrame on any failure — the
    caller degrades to candle-only features. Cached per (symbol, params, day)."""
    key = (symbol, int(lookback_days), bool(include_macro), date.today().isoformat())
    if key in _CACHE:
        return _CACHE[key]

    macro = "true" if include_macro else "false"
    url = (
        f"{_base_url()}/factor-history/{symbol}"
        f"?lookback_days={int(lookback_days)}&include_macro={macro}"
    )
    token = os.getenv("MT_API_TOKEN") or None
    try:
        payload = _http_get_json(url, token)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        logger.warning("terminal_features: %s unreachable (%s) — features skipped", symbol, exc)
        df = pd.DataFrame()
        _CACHE[key] = df
        return df

    data = payload.get("data", payload)  # unwrap the terminal's {data, provider, ...} Envelope
    rows = data.get("rows") if isinstance(data, dict) else None
    if not rows:
        logger.warning("terminal_features: %s returned no rows (%s)", symbol,
                       (data or {}).get("error") if isinstance(data, dict) else None)
        df = pd.DataFrame()
        _CACHE[key] = df
        return df

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    keep = [c for c in _NUMERIC_COLS if c in df.columns]
    for c in keep:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[["date", *keep]].sort_values("date").reset_index(drop=True)
    _CACHE[key] = df
    return df


def merge_terminal_features(
    dataframe: pd.DataFrame,
    pair: str,
    *,
    lookback_days: int = 1460,
    include_macro: bool = False,
    prefix: str = "%-",
) -> pd.DataFrame:
    """Merge the terminal's daily ``mt_*`` panel onto a strategy `dataframe` as FreqAI
    features, aligned lookahead-free.

    Uses ``pd.merge_asof(direction="backward")`` on the ``date`` column, so each candle only
    ever sees terminal reads dated **at or before** it (the panel is itself lookahead-free and
    stamped at its own bar). Daily terminal values forward-fill across intraday candles until
    the next daily read. Columns are prefixed (default ``%-``) so FreqAI treats them as
    features. A missing/unreachable terminal leaves `dataframe` untouched.
    """
    if "date" not in dataframe.columns or dataframe.empty:
        return dataframe

    symbol = pair_to_symbol(pair)
    panel = fetch_panel(symbol, lookback_days=lookback_days, include_macro=include_macro)
    if panel.empty:
        return dataframe

    left = dataframe.copy()
    left_dt = pd.to_datetime(left["date"], utc=True)
    order = left_dt.argsort(kind="stable")  # merge_asof needs a sorted key; restore order after
    feature_cols = [c for c in panel.columns if c != "date"]
    renamed = panel.rename(columns={c: f"{prefix}{c}" for c in feature_cols})

    merged = pd.merge_asof(
        pd.DataFrame({"date": left_dt.iloc[order].reset_index(drop=True)}),
        renamed,
        on="date",
        direction="backward",
    )
    merged.index = left.index[order]
    merged = merged.sort_index()
    for c in (f"{prefix}{col}" for col in feature_cols):
        left[c] = merged[c].to_numpy()
    return left
