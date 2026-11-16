"""The /auto watchlist, editable from Telegram and persisted across restarts.

The watchlist used to live only in ``.env`` (``EXECUTION_AGENT_WATCHLIST``),
which meant editing a file and restarting the bot to add a ticker. This module
keeps the *live* watchlist in a small JSON file instead: the ``.env`` value
seeds it on first use, and ``/watchlist add AMD`` / ``/watchlist remove AMD``
edit it from the phone. ``/auto`` reads it fresh on every run, so an addition
takes effect on the very next scan — no restart.
"""

from __future__ import annotations

import json
import os

from execution_agent.config import ExecutionConfig
from execution_agent.symbols import to_moomoo
from tradingagents.dataflows.files import locked
from tradingagents.dataflows.symbols import normalize_symbol

DEFAULT_WATCHLIST_PATH = os.path.join(
    os.path.expanduser("~"), ".tradingagents", "execution", "watchlist.json"
)


def watchlist_path() -> str:
    """Where the live watchlist is stored; overridable for tests."""
    return os.environ.get("EXECUTION_AGENT_WATCHLIST_PATH") or DEFAULT_WATCHLIST_PATH


def load(config: ExecutionConfig) -> list[str]:
    """The live watchlist: the stored file, or the ``.env`` seed when absent.

    A corrupt or malformed file falls back to the seed rather than erasing the
    watchlist — the next save rewrites it cleanly.
    """
    path = watchlist_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("tickers"), list):
                return [str(t) for t in data["tickers"] if isinstance(t, str) and t]
        except (json.JSONDecodeError, OSError):
            pass
    return config.watchlist_tickers()


def save(tickers: list[str]) -> None:
    """Atomically persist the live watchlist."""
    path = watchlist_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = json.dumps({"tickers": tickers}, ensure_ascii=False)
    with locked(path), open(path, "w", encoding="utf-8") as handle:
        handle.write(payload)


def add(config: ExecutionConfig, ticker: str) -> tuple[list[str], bool]:
    """Add ``ticker`` to the live watchlist; returns ``(tickers, added)``.

    The ticker is validated the same way an order would be (``to_moomoo``
    rejects crypto/forex/futures/indices), so a typo fails loudly here instead
    of silently failing inside tomorrow's /auto run. Raises ``ValueError`` on
    an unsupported symbol.
    """
    symbol = normalize_symbol(ticker.strip())
    to_moomoo(symbol)  # raises ValueError on an unsupported/garbage symbol
    tickers = load(config)
    if any(t.upper() == symbol.upper() for t in tickers):
        return tickers, False
    tickers = [*tickers, symbol]
    save(tickers)
    return tickers, True


def remove(config: ExecutionConfig, ticker: str) -> tuple[list[str], bool]:
    """Remove ``ticker`` from the live watchlist; returns ``(tickers, removed)``."""
    wanted = normalize_symbol(ticker.strip()).upper()
    tickers = load(config)
    kept = [t for t in tickers if t.upper() != wanted]
    if len(kept) == len(tickers):
        return tickers, False
    save(kept)
    return kept, True
