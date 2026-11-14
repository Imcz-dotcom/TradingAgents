"""Extracting entry/stop/target levels from the rendered research reports."""

from execution_agent.prices import parse_prices, parse_time_horizon, time_in_force_for


def _state(trader="", pm=""):
    return {
        "trader_investment_plan": trader,
        "investment_plan": pm,
    }


def test_parse_prices_extracts_all_three():
    trader = "**Action**: BUY\n\n**Entry Price**: 189.5\n\n**Stop Loss**: 172.0"
    pm = "**Price Target**: 200.0"
    result = parse_prices(_state(trader, pm))

    assert result == {
        "entry_price": 189.5,
        "stop_loss": 172.0,
        "price_target": 200.0,
    }


def test_parse_prices_not_provided_is_none():
    trader = "**Entry Price**: not provided\n\n**Stop Loss**: not provided"
    pm = "**Price Target**: not provided"
    result = parse_prices(_state(trader, pm))

    assert result == {"entry_price": None, "stop_loss": None, "price_target": None}


def test_parse_prices_missing_fields_is_none():
    assert parse_prices({}) == {
        "entry_price": None,
        "stop_loss": None,
        "price_target": None,
    }


def test_parse_prices_garbage_is_none():
    trader = "**Entry Price**: somewhere around two hundred"
    pm = "**Price Target**: n/a"
    result = parse_prices(_state(trader, pm))

    assert result == {"entry_price": None, "stop_loss": None, "price_target": None}


def test_parse_prices_partial_levels():
    trader = "**Entry Price**: 189.5"
    result = parse_prices(_state(trader, ""))

    assert result["entry_price"] == 189.5
    assert result["stop_loss"] is None
    assert result["price_target"] is None


def test_parse_prices_tolerates_dollar_sign():
    trader = "**Entry Price**: $189.5"
    assert parse_prices(_state(trader, ""))["entry_price"] == 189.5


# --- time horizon ---------------------------------------------------------


def test_parse_time_horizon_reads_phrase():
    pm = "**Rating**: Buy\n\n**Price Target**: 200.0\n\n**Time Horizon**: 3-6 months"
    assert parse_time_horizon({"investment_plan": pm}) == "3-6 months"


def test_parse_time_horizon_not_provided_is_none():
    pm = "**Time Horizon**: not provided"
    assert parse_time_horizon({"investment_plan": pm}) is None


def test_parse_time_horizon_missing_is_none():
    assert parse_time_horizon({}) is None
    assert parse_time_horizon(None) is None


def test_time_in_force_long_horizon_is_gtc():
    assert time_in_force_for("3-6 months") == "GTC"
    assert time_in_force_for("long-term") == "GTC"
    assert time_in_force_for("1-2 years") == "GTC"


def test_time_in_force_short_horizon_is_day():
    assert time_in_force_for("short-term") == "DAY"
    assert time_in_force_for("1-2 weeks") == "DAY"
    assert time_in_force_for("intraday") == "DAY"


def test_time_in_force_absent_defaults_to_gtc():
    # A resting limit exists to survive the agent being offline: never default
    # to a DAY order that would silently die.
    assert time_in_force_for(None) == "GTC"
    assert time_in_force_for("") == "GTC"
    assert time_in_force_for("something unusual") == "GTC"
