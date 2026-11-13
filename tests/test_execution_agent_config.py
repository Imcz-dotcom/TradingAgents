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
    assert cfg.telegram_bot_token is None
    assert cfg.telegram_chat_id is None


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
