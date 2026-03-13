"""Tests for the indicator registry auto-discovery."""

from __future__ import annotations

from hydra.strategy.indicator_registry import IndicatorInfo, ParamInfo, get_all_indicators


class TestGetAllIndicators:
    """Test the get_all_indicators function."""

    def test_returns_non_empty_list(self) -> None:
        """get_all_indicators should return a non-empty list."""
        indicators = get_all_indicators()
        assert len(indicators) > 0

    def test_each_indicator_has_name(self) -> None:
        """Every indicator should have a non-empty name."""
        indicators = get_all_indicators()
        for ind in indicators:
            assert isinstance(ind, IndicatorInfo)
            assert ind.name
            assert len(ind.name) > 0

    def test_each_indicator_has_category(self) -> None:
        """Every indicator should have a category."""
        indicators = get_all_indicators()
        valid_categories = {"trend", "momentum", "volatility", "volume", "other"}
        for ind in indicators:
            assert ind.category in valid_categories

    def test_each_indicator_has_params(self) -> None:
        """Every indicator should have a params list (possibly empty)."""
        indicators = get_all_indicators()
        for ind in indicators:
            assert isinstance(ind.params, list)

    def test_rsi_present(self) -> None:
        """RSI should be in the indicator list."""
        indicators = get_all_indicators()
        names = {ind.name for ind in indicators}
        assert "rsi" in names

    def test_macd_present(self) -> None:
        """MACD should be in the indicator list."""
        indicators = get_all_indicators()
        names = {ind.name for ind in indicators}
        assert "macd" in names

    def test_sma_present(self) -> None:
        """SMA should be in the indicator list."""
        indicators = get_all_indicators()
        names = {ind.name for ind in indicators}
        assert "sma" in names

    def test_bollinger_bands_present(self) -> None:
        """Bollinger Bands should be in the indicator list."""
        indicators = get_all_indicators()
        names = {ind.name for ind in indicators}
        assert "bollinger_bands" in names

    def test_known_indicators_complete(self) -> None:
        """All known library indicators should be discovered."""
        indicators = get_all_indicators()
        names = {ind.name for ind in indicators}
        expected = {
            "sma",
            "ema",
            "macd",
            "supertrend",
            "ichimoku",
            "rsi",
            "stochastic",
            "cci",
            "williams_r",
            "atr",
            "bollinger_bands",
            "keltner_channels",
            "obv",
            "vwap",
            "mfi",
        }
        assert expected.issubset(names)


class TestParamSchemas:
    """Test that parameter schemas are correctly extracted."""

    def test_rsi_has_period_param(self) -> None:
        """RSI should have a 'period' parameter."""
        indicators = get_all_indicators()
        rsi = next(ind for ind in indicators if ind.name == "rsi")
        param_names = {p.name for p in rsi.params}
        assert "period" in param_names

    def test_rsi_period_has_default(self) -> None:
        """RSI period parameter should have a default of 14."""
        indicators = get_all_indicators()
        rsi = next(ind for ind in indicators if ind.name == "rsi")
        period = next(p for p in rsi.params if p.name == "period")
        assert period.default == 14

    def test_param_has_name_and_type(self) -> None:
        """Each parameter should have a name and type."""
        indicators = get_all_indicators()
        for ind in indicators:
            for param in ind.params:
                assert isinstance(param, ParamInfo)
                assert param.name
                assert param.type in ("int", "float")

    def test_param_has_default(self) -> None:
        """Params with defaults in the function signature should have them set."""
        indicators = get_all_indicators()
        macd = next(ind for ind in indicators if ind.name == "macd")
        fast_param = next(p for p in macd.params if p.name == "fast")
        assert fast_param.default == 12
        slow_param = next(p for p in macd.params if p.name == "slow")
        assert slow_param.default == 26
        signal_param = next(p for p in macd.params if p.name == "signal")
        assert signal_param.default == 9

    def test_sma_has_no_default_period(self) -> None:
        """SMA period has no default in the function signature."""
        indicators = get_all_indicators()
        sma_ind = next(ind for ind in indicators if ind.name == "sma")
        period = next(p for p in sma_ind.params if p.name == "period")
        assert period.default is None

    def test_data_params_excluded(self) -> None:
        """Data array parameters (data, close, high, low, volume) should be excluded."""
        indicators = get_all_indicators()
        data_names = {"data", "close", "high", "low", "volume"}
        for ind in indicators:
            param_names = {p.name for p in ind.params}
            assert param_names.isdisjoint(data_names), (
                f"Indicator {ind.name} should not expose data params: {param_names & data_names}"
            )


class TestCategories:
    """Test that indicators are correctly categorized."""

    def test_rsi_is_momentum(self) -> None:
        """RSI should be categorized as momentum."""
        indicators = get_all_indicators()
        rsi = next(ind for ind in indicators if ind.name == "rsi")
        assert rsi.category == "momentum"

    def test_sma_is_trend(self) -> None:
        """SMA should be categorized as trend."""
        indicators = get_all_indicators()
        sma_ind = next(ind for ind in indicators if ind.name == "sma")
        assert sma_ind.category == "trend"

    def test_atr_is_volatility(self) -> None:
        """ATR should be categorized as volatility."""
        indicators = get_all_indicators()
        atr_ind = next(ind for ind in indicators if ind.name == "atr")
        assert atr_ind.category == "volatility"

    def test_obv_is_volume(self) -> None:
        """OBV should be categorized as volume."""
        indicators = get_all_indicators()
        obv_ind = next(ind for ind in indicators if ind.name == "obv")
        assert obv_ind.category == "volume"
