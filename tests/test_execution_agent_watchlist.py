"""Live watchlist: .env seed, add/remove from chat, persistence across loads."""

import pytest

from execution_agent import watchlist
from execution_agent.config import ExecutionConfig


@pytest.fixture
def wl_path(monkeypatch, tmp_path):
    path = str(tmp_path / "watchlist.json")
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: path)
    return path


def _config(seed="NVDA,AAPL"):
    return ExecutionConfig(watchlist=seed)


def test_load_seeds_from_env_when_no_file(wl_path):
    assert watchlist.load(_config()) == ["NVDA", "AAPL"]


def test_add_persists_and_normalizes(wl_path):
    tickers, added = watchlist.add(_config(), " amd ")

    assert added is True
    assert tickers == ["NVDA", "AAPL", "AMD"]
    # A later load reads the file, not the seed.
    assert watchlist.load(_config(seed="")) == ["NVDA", "AAPL", "AMD"]


def test_add_duplicate_is_noop(wl_path):
    tickers, added = watchlist.add(_config(), "nvda")

    assert added is False
    assert tickers == ["NVDA", "AAPL"]


def test_add_rejects_unsupported_symbol(wl_path):
    with pytest.raises(ValueError):
        watchlist.add(_config(), "BTC-USD")


def test_remove_persists(wl_path):
    tickers, removed = watchlist.remove(_config(), "aapl")

    assert removed is True
    assert tickers == ["NVDA"]
    assert watchlist.load(_config(seed="")) == ["NVDA"]


def test_remove_missing_is_noop(wl_path):
    tickers, removed = watchlist.remove(_config(), "TSLA")

    assert removed is False
    assert tickers == ["NVDA", "AAPL"]


def test_load_corrupt_file_falls_back_to_seed(wl_path, tmp_path):
    (tmp_path / "watchlist.json").write_text("{not json")

    assert watchlist.load(_config()) == ["NVDA", "AAPL"]
