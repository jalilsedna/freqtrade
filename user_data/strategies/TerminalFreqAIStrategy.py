"""TerminalFreqAIStrategy — a FreqAI crypto strategy that learns on the Market-Terminal's
signals.

This is the freqtrade half of the terminal ↔ freqtrade crypto sibling. The model is trained
on the usual candle-derived features (RSI/MFI/ADX/…) **plus** the terminal's lookahead-free
``mt_*`` structural reads, pulled over HTTP by ``terminal_features.merge_terminal_features``
(see that module for the boundary rules — crypto only, terminal stays signal-only, exchange
keys never leave the freqtrade side).

Target: the mean forward return over ``label_period_candles`` (regression), same shape as
freqtrade's FreqAI example, so it runs with ``LightGBMRegressor`` out of the box:

    freqtrade backtesting --strategy TerminalFreqAIStrategy \
        --freqaimodel LightGBMRegressor --config user_data/config_terminal_freqai.example.json

Showcase / research scaffold — not tuned for live capital. Validate a pair's edge before
trusting it, exactly as with the terminal's own signals.
"""
import logging
from functools import reduce

import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

# Absolute import: freqtrade loads a strategy via spec_from_file_location with its directory
# on sys.path (no package context), so a sibling helper is imported by bare module name.
from terminal_features import merge_terminal_features

from freqtrade.strategy import IStrategy


logger = logging.getLogger(__name__)


class TerminalFreqAIStrategy(IStrategy):
    minimal_roi = {"0": 0.1, "240": -1}

    process_only_new_candles = True
    stoploss = -0.05
    use_exit_signal = True
    startup_candle_count: int = 40
    can_short = False  # spot by default; flip to True only for a futures/margin config

    # Pull the terminal's macro columns too? Off by default (keeps the panel light); a
    # subclass or config-driven flag can enable it.
    terminal_include_macro: bool = False

    def feature_engineering_expand_all(
        self, dataframe: DataFrame, period: int, metadata: dict, **kwargs
    ) -> DataFrame:
        """Candle-derived features, auto-expanded across the config's periods/timeframes.
        These stand alone, so the model still trains if the terminal is unreachable."""
        dataframe["%-rsi-period"] = ta.RSI(dataframe, timeperiod=period)
        dataframe["%-mfi-period"] = ta.MFI(dataframe, timeperiod=period)
        dataframe["%-adx-period"] = ta.ADX(dataframe, timeperiod=period)
        dataframe["%-sma-period"] = ta.SMA(dataframe, timeperiod=period)
        dataframe["%-ema-period"] = ta.EMA(dataframe, timeperiod=period)

        bollinger = qtpylib.bollinger_bands(
            qtpylib.typical_price(dataframe), window=period, stds=2.2
        )
        dataframe["bb_lowerband-period"] = bollinger["lower"]
        dataframe["bb_middleband-period"] = bollinger["mid"]
        dataframe["bb_upperband-period"] = bollinger["upper"]
        dataframe["%-bb_width-period"] = (
            dataframe["bb_upperband-period"] - dataframe["bb_lowerband-period"]
        ) / dataframe["bb_middleband-period"]
        dataframe["%-close-bb_lower-period"] = dataframe["close"] / dataframe["bb_lowerband-period"]

        dataframe["%-roc-period"] = ta.ROC(dataframe, timeperiod=period)
        dataframe["%-relative_volume-period"] = (
            dataframe["volume"] / dataframe["volume"].rolling(period).mean()
        )
        return dataframe

    def feature_engineering_expand_basic(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        dataframe["%-pct-change"] = dataframe["close"].pct_change()
        dataframe["%-raw_volume"] = dataframe["volume"]
        dataframe["%-raw_price"] = dataframe["close"]
        return dataframe

    def feature_engineering_standard(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        """Final, once-per-base-timeframe pass — the right place to join the terminal panel
        (daily, not period-expandable) onto the candles as ``%-mt_*`` features. Lookahead-free
        (merge_asof backward). A no-op if the terminal is down."""
        dataframe["%-day_of_week"] = dataframe["date"].dt.dayofweek
        dataframe["%-hour_of_day"] = dataframe["date"].dt.hour

        pair = metadata.get("pair", "")
        try:
            dataframe = merge_terminal_features(
                dataframe, pair, include_macro=self.terminal_include_macro
            )
        except Exception as exc:  # never let a signal-source hiccup break feature building
            logger.warning("terminal features skipped for %s: %s", pair, exc)
        return dataframe

    def set_freqai_targets(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        """Predict the mean forward return over the label window (regression)."""
        label_period = self.freqai_info["feature_parameters"]["label_period_candles"]
        dataframe["&-s_close"] = (
            dataframe["close"].shift(-label_period).rolling(label_period).mean()
            / dataframe["close"]
            - 1
        )
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return self.freqai.start(dataframe, metadata, self)

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        enter_long_conditions = [df["do_predict"] == 1, df["&-s_close"] > 0.01]
        df.loc[
            reduce(lambda x, y: x & y, enter_long_conditions), ["enter_long", "enter_tag"]
        ] = (1, "long")

        if self.can_short:
            enter_short_conditions = [df["do_predict"] == 1, df["&-s_close"] < -0.01]
            df.loc[
                reduce(lambda x, y: x & y, enter_short_conditions), ["enter_short", "enter_tag"]
            ] = (1, "short")
        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        exit_long_conditions = [df["do_predict"] == 1, df["&-s_close"] < 0]
        df.loc[reduce(lambda x, y: x & y, exit_long_conditions), "exit_long"] = 1

        if self.can_short:
            exit_short_conditions = [df["do_predict"] == 1, df["&-s_close"] > 0]
            df.loc[reduce(lambda x, y: x & y, exit_short_conditions), "exit_short"] = 1
        return df
