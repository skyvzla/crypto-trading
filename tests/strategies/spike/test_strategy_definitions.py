from trading_platform.backtest.strategy_definition import FeatureSpec
from trading_platform.strategies.spike.definition import load_strategy_definition


def test_versioned_strategies_are_distinct_complete_implementations():
    v1 = load_strategy_definition("trading_platform.strategies.spike.v1:V1")
    v2 = load_strategy_definition("trading_platform.strategies.spike.v2:V2")
    v21 = load_strategy_definition("trading_platform.strategies.spike.v2_1:V21")
    v11 = load_strategy_definition("trading_platform.strategies.spike.v1_1:V11")

    assert len({v1.strategy_class, v2.strategy_class, v21.strategy_class, v11.strategy_class}) == 4
    assert v1.defaults.exit_policy == "confirmed"
    assert v2.defaults.exit_policy == "candidate-v1"
    assert v2.defaults.profit_unlock_percent == 1.5
    assert v21.defaults.profit_unlock_percent == 3.0
    assert not v1.data_requirements.metrics_5m
    assert not v2.data_requirements.metrics_5m
    assert v21.data_requirements.metrics_5m
    assert v11.defaults.exit_policy == "candidate-v1"
    assert v11.defaults.prior_high_lookback_hours == 4
    assert v11.defaults.entry_tier_mode == "three-tier"
    assert v11.data_requirements.metrics_5m
    assert "max_oi_change_pct" in v11.supported_parameters
    assert "prior_high_tolerance_percent" in v11.supported_parameters
    assert not v1.supported_parameters
    assert not v2.supported_parameters
    assert "max_oi_change_pct" in v21.supported_parameters
    assert "max_rise_5s_percent" in v21.supported_parameters
    assert "max_volume_multiple_5s" in v21.supported_parameters
    assert "min_td_sell_setup_5m" in v21.supported_parameters
    assert "min_volume_multiple_5m" in v21.supported_parameters
    assert "metrics_series" in v21.internal_parameters


def test_strategy_data_requirements_include_execution_timeframe():
    definition = load_strategy_definition(
        "trading_platform.strategies.spike.v1_1:V11"
    )
    requirements = definition.data_requirements
    assert requirements.execution_timeframe in requirements.market_timeframes
    assert requirements.shared_features == frozenset({
        FeatureSpec(name="rise_5s", timeframe="1s"),
        FeatureSpec(name="candidate_exit", timeframe="1m"),
    })


def test_shared_features_are_opt_in_per_strategy():
    unshared = [
        load_strategy_definition("trading_platform.strategies.spike.v1:V1"),
        load_strategy_definition(
            "trading_platform.strategies.spike.pullback:PullbackV3"
        ),
    ]
    assert all(definition.data_requirements.shared_features == frozenset()
               for definition in unshared)
    assert all(definition.shared_feature_provider is None for definition in unshared)


def test_v2_family_declares_the_shared_spike_provider_contract():
    rise = FeatureSpec(name="rise_5s", timeframe="1s")
    candidate = FeatureSpec(name="candidate_exit", timeframe="1m")

    for path in (
        "trading_platform.strategies.spike.v2:V2",
        "trading_platform.strategies.spike.v2_1:V21",
        "trading_platform.strategies.spike.v2_2:V22",
    ):
        definition = load_strategy_definition(path)
        assert definition.shared_feature_provider is not None
        assert not hasattr(definition, "bar1s_feature_columns")
        assert definition.data_requirements.bar1s_feature_columns == frozenset()
        assert definition.data_requirements.shared_features == frozenset(
            {rise, candidate}
        )

    for path in (
        "trading_platform.strategies.spike.v1:V1",
        "trading_platform.strategies.spike.pullback:PullbackV3",
    ):
        definition = load_strategy_definition(path)
        assert definition.shared_feature_provider is None
