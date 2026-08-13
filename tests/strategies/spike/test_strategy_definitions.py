from trading_platform.strategies.spike.definition import load_strategy_definition


def test_versioned_strategies_are_distinct_complete_implementations():
    v1 = load_strategy_definition("trading_platform.strategies.spike.v1:V1")
    v2 = load_strategy_definition("trading_platform.strategies.spike.v2:V2")
    v21 = load_strategy_definition("trading_platform.strategies.spike.v2_1:V21")

    assert len({v1.strategy_class, v2.strategy_class, v21.strategy_class}) == 3
    assert v1.defaults.exit_policy == "confirmed"
    assert v2.defaults.exit_policy == "candidate-v1"
    assert v2.defaults.profit_unlock_percent == 1.5
    assert v21.defaults.profit_unlock_percent == 3.0
    assert not v1.data_requirements.metrics_5m
    assert not v2.data_requirements.metrics_5m
    assert v21.data_requirements.metrics_5m
    assert not v1.supported_parameters
    assert not v2.supported_parameters
    assert "max_oi_change_pct" in v21.supported_parameters
    assert "metrics_series" in v21.internal_parameters


def test_strategy_data_requirements_include_execution_timeframe():
    definition = load_strategy_definition(
        "trading_platform.strategies.spike.v2_1:V21"
    )
    requirements = definition.data_requirements
    assert requirements.execution_timeframe in requirements.market_timeframes
