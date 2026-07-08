"""Network-free tests for the Market-Terminal FreqAI provider (crypto sibling).

The HTTP call funnels through terminal_features._http_get_json, which these tests monkeypatch,
so nothing here touches a network or a running terminal.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest


# terminal_features lives beside the strategies (freqtrade loads it by bare name off sys.path);
# put that directory on the path so we can import it directly here.
_STRAT_DIR = Path(__file__).resolve().parents[2] / "user_data" / "strategies"
sys.path.insert(0, str(_STRAT_DIR))

import terminal_features as tf  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    tf._CACHE.clear()
    yield
    tf._CACHE.clear()


def _panel_payload():
    """A minimal /factor-history Envelope with two daily rows, string labels included so we
    can assert they're dropped."""
    return {
        "provider": "computed",
        "data": {
            "ok": True,
            "rows": [
                {"date": "2024-01-01", "mt_structure_bias": 1, "mt_profile_bias": 0,
                 "mt_regime_score": 0.4, "mt_vol_pct": 55.0, "mt_momentum": 1,
                 "mt_realized_vol": 0.6, "mt_structure_trend": "bullish",
                 "mt_price_regime": "trending"},
                {"date": "2024-01-02", "mt_structure_bias": -1, "mt_profile_bias": 1,
                 "mt_regime_score": 0.7, "mt_vol_pct": 60.0, "mt_momentum": -1,
                 "mt_realized_vol": 0.5, "mt_structure_trend": "bearish",
                 "mt_price_regime": "ranging"},
            ],
        },
    }


@pytest.mark.parametrize(
    "pair,expected",
    [("BTC/USDT", "BTC-USD"), ("ETH/USDT:USDT", "ETH-USD"), ("SOL/USD", "SOL-USD")],
)
def test_pair_to_symbol(pair, expected, monkeypatch):
    monkeypatch.delenv("MT_QUOTE", raising=False)
    assert tf.pair_to_symbol(pair) == expected


def test_pair_to_symbol_respects_mt_quote(monkeypatch):
    monkeypatch.setenv("MT_QUOTE", "USDT")
    assert tf.pair_to_symbol("BTC/USDT") == "BTC-USDT"


def test_fetch_panel_keeps_numeric_drops_labels(monkeypatch):
    monkeypatch.setattr(tf, "_http_get_json", lambda url, token: _panel_payload())
    df = tf.fetch_panel("BTC-USD")
    assert not df.empty
    assert "mt_structure_bias" in df.columns and "mt_regime_score" in df.columns
    # categorical label columns are dropped (features must be numeric)
    assert "mt_structure_trend" not in df.columns
    assert "mt_price_regime" not in df.columns
    assert pd.api.types.is_numeric_dtype(df["mt_regime_score"])


def test_fetch_panel_caches_one_call_per_day(monkeypatch):
    calls = {"n": 0}

    def _fake(url, token):
        calls["n"] += 1
        return _panel_payload()

    monkeypatch.setattr(tf, "_http_get_json", _fake)
    tf.fetch_panel("BTC-USD")
    tf.fetch_panel("BTC-USD")
    assert calls["n"] == 1


def test_fetch_panel_degrades_on_error(monkeypatch):
    def _boom(url, token):
        raise OSError("terminal down")

    monkeypatch.setattr(tf, "_http_get_json", _boom)
    assert tf.fetch_panel("BTC-USD").empty


def test_merge_is_lookahead_free_and_forward_fills(monkeypatch):
    monkeypatch.setattr(tf, "_http_get_json", lambda url, token: _panel_payload())
    # Six hourly candles spanning the two daily terminal rows.
    candles = pd.DataFrame({
        "date": pd.to_datetime([
            "2024-01-01 00:00", "2024-01-01 12:00", "2024-01-01 23:00",
            "2024-01-02 00:00", "2024-01-02 06:00", "2024-01-02 18:00",
        ], utc=True),
        "close": [100, 101, 102, 103, 104, 105],
    })
    out = tf.merge_terminal_features(candles, "BTC/USDT")
    assert list(out["close"]) == [100, 101, 102, 103, 104, 105]  # untouched, order preserved
    # Jan-1 candles see the Jan-1 read (bias +1); Jan-2 candles see Jan-2 (bias -1).
    assert list(out["%-mt_structure_bias"]) == [1, 1, 1, -1, -1, -1]
    # A candle strictly before the first daily read would have no terminal data (NaN) — proven
    # by prepending one:
    pre = pd.concat([
        pd.DataFrame({"date": pd.to_datetime(["2023-12-31 12:00"], utc=True), "close": [99]}),
        candles,
    ], ignore_index=True)
    out2 = tf.merge_terminal_features(pre, "BTC/USDT")
    assert pd.isna(out2["%-mt_structure_bias"].iloc[0])   # no lookahead into 2024-01-01


def test_merge_noop_when_terminal_down(monkeypatch):
    monkeypatch.setattr(tf, "_http_get_json", lambda url, token: (_ for _ in ()).throw(OSError()))
    candles = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-01 00:00"], utc=True), "close": [100],
    })
    out = tf.merge_terminal_features(candles, "BTC/USDT")
    assert list(out.columns) == ["date", "close"]   # unchanged, no %-mt_* columns
