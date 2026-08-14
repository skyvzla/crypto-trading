import pytest

from trading_platform.backtest.strategy_definition import (
    FeatureSpec,
    MarketDataRequirements,
)


def test_market_data_requirements_default_to_no_shared_features():
    requirements = MarketDataRequirements(
        market_timeframes=("1m",), execution_timeframe="1m"
    )

    assert requirements.shared_features == frozenset()


def test_shared_features_are_hashable_and_deduplicated():
    feature = FeatureSpec(name="rise_5s", timeframe="1s")
    requirements = MarketDataRequirements(
        market_timeframes=("1s", "1m"),
        execution_timeframe="1s",
        shared_features=(feature, FeatureSpec(name="rise_5s", timeframe="1s")),
    )

    assert hash(feature) == hash(FeatureSpec(name="rise_5s", timeframe="1s"))
    assert requirements.shared_features == frozenset({feature})


def test_shared_feature_timeframe_must_be_a_market_timeframe():
    with pytest.raises(ValueError, match="shared feature timeframes"):
        MarketDataRequirements(
            market_timeframes=("1m",),
            execution_timeframe="1m",
            shared_features=(FeatureSpec(name="rise_5s", timeframe="1s"),),
        )
