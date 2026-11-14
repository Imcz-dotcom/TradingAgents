"""Execution-agent settings: defaults, env override, coercion, validation."""

import os

import pytest

import execution_agent.config as config_mod
from execution_agent.config import ExecutionConfig, mode_label


def _clear_exec_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("EXECUTION_AGENT_")]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def isolated_env(monkeypatch):
    _clear_exec_env(monkeypatch)
    monkeypatch.setattr(config_mod, "load_dotenv", lambda: None)  # no .env in tests
    return monkeypatch


def test_defaults_are_safe():
    cfg = ExecutionConfig()
    assert cfg.trading_env == "SIMULATE"          # paper first, never live by default
    assert cfg.sizing_mode == "notional"
    assert cfg.sizing_amount == 1000.0
    assert cfg.moomoo_host == "127.0.0.1"
    assert cfg.moomoo_port == 11111
    assert cfg.approval_timeout_seconds == 300
    assert cfg.log_retention_days == 7
    assert cfg.telegram_bot_token is None
    assert cfg.telegram_chat_id is None
    assert cfg.watchlist == ""
    assert cfg.watchlist_tickers() == []
    assert cfg.limit_price_band == 0.20
    assert cfg.auto_underweight_trim == 0.50


def test_watchlist_tickers_splits_strips_dedups():
    cfg = ExecutionConfig(watchlist="NVDA, AAPL , NVDA, , 0700.HK")
    assert cfg.watchlist_tickers() == ["NVDA", "AAPL", "0700.HK"]


def test_watchlist_tickers_empty_when_unset():
    assert ExecutionConfig(watchlist="").watchlist_tickers() == []
    assert ExecutionConfig(watchlist=" , , ").watchlist_tickers() == []


def test_env_override_watchlist_and_band(isolated_env):
    isolated_env.setenv("EXECUTION_AGENT_WATCHLIST", "NVDA,AAPL")
    isolated_env.setenv("EXECUTION_AGENT_LIMIT_PRICE_BAND", "0.10")
    cfg = ExecutionConfig.from_env()
    assert cfg.watchlist_tickers() == ["NVDA", "AAPL"]
    assert cfg.limit_price_band == 0.10


def test_validate_rejects_non_positive_limit_band():
    with pytest.raises(ValueError):
        ExecutionConfig(limit_price_band=0)


def test_env_override_auto_underweight_trim(isolated_env):
    isolated_env.setenv("EXECUTION_AGENT_AUTO_UNDERWEIGHT_TRIM", "0.25")
    assert ExecutionConfig.from_env().auto_underweight_trim == 0.25


def test_validate_rejects_out_of_range_auto_underweight_trim():
    with pytest.raises(ValueError):
        ExecutionConfig(auto_underweight_trim=0)
    with pytest.raises(ValueError):
        ExecutionConfig(auto_underweight_trim=1.5)


def test_env_override_and_coercion(isolated_env):
    isolated_env.setenv("EXECUTION_AGENT_MOOMOO_PORT", "22222")      # -> int
    isolated_env.setenv("EXECUTION_AGENT_SIZING_AMOUNT", "500")      # -> float
    isolated_env.setenv("EXECUTION_AGENT_TRADING_ENV", "REAL")       # -> str

    cfg = ExecutionConfig.from_env()

    assert cfg.moomoo_port == 22222
    assert cfg.sizing_amount == 500.0
    assert cfg.trading_env == "REAL"
    # untouched defaults survive
    assert cfg.approval_timeout_seconds == 300


def test_env_override_empty_is_ignored(isolated_env):
    isolated_env.setenv("EXECUTION_AGENT_SIZING_AMOUNT", "")
    cfg = ExecutionConfig.from_env()
    assert cfg.sizing_amount == 1000.0


def test_env_override_bad_type_raises(isolated_env):
    isolated_env.setenv("EXECUTION_AGENT_MOOMOO_PORT", "not-a-port")
    with pytest.raises(ValueError):
        ExecutionConfig.from_env()


def test_validate_rejects_bad_trading_env():
    with pytest.raises(ValueError):
        ExecutionConfig(trading_env="LIVE")  # not in {SIMULATE, REAL}


def test_validate_rejects_bad_sizing_mode():
    with pytest.raises(ValueError):
        ExecutionConfig(sizing_mode="dollars")


def test_validate_rejects_non_positive_sizing_amount():
    with pytest.raises(ValueError):
        ExecutionConfig(sizing_amount=0)


def test_validate_rejects_non_positive_log_retention():
    with pytest.raises(ValueError):
        ExecutionConfig(log_retention_days=0)


def test_env_override_log_retention(isolated_env):
    isolated_env.setenv("EXECUTION_AGENT_LOG_RETENTION_DAYS", "14")
    assert ExecutionConfig.from_env().log_retention_days == 14


def test_side_mapping_parses_json():
    cfg = ExecutionConfig(side_mapping='{"Buy": "SELL"}')
    assert cfg.parsed_side_mapping() == {"Buy": "SELL"}


def test_side_mapping_none_when_unset():
    assert ExecutionConfig().parsed_side_mapping() is None


def test_side_mapping_rejects_invalid_json():
    with pytest.raises(ValueError):
        ExecutionConfig(side_mapping="not json")


def test_side_mapping_rejects_non_string_values():
    with pytest.raises(ValueError):
        ExecutionConfig(side_mapping='{"Buy": 1}')


def test_mode_label_paper_and_real():
    assert "PAPER" in mode_label("SIMULATE")
    assert "REAL" in mode_label("REAL")
