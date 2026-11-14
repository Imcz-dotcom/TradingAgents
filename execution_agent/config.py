"""Execution-agent settings, overridable via ``EXECUTION_AGENT_*`` env vars.

Mirrors ``tradingagents.default_config``'s env-override pattern: a typed frozen
dataclass whose defaults can be overridden by environment variables (and a
``.env`` file, loaded via python-dotenv). Coercion is driven by the type of each
field's default, so a ``.env`` keeps plain strings. The defaults encode the
safety posture: paper trading (``SIMULATE``), approve-is-opt-in, MARKET orders
only.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields

from dotenv import load_dotenv

_ENV_PREFIX = "EXECUTION_AGENT_"

_BOOL_TRUE = ("true", "1", "yes", "on")
_BOOL_FALSE = ("false", "0", "no", "off")

_SIZING_MODES = ("notional", "shares", "percent_of_cash")
_TRADING_ENVS = ("SIMULATE", "REAL")


def _coerce(value: str, reference):
    """Coerce an env-var string to the type of the field's default value.

    Invalid values raise ``ValueError`` rather than silently falling back to a
    default — a misspelled boolean or non-numeric int should fail loudly at
    startup, not quietly misconfigure an unattended run.
    """
    if isinstance(reference, bool):
        normalized = value.strip().lower()
        if normalized in _BOOL_TRUE:
            return True
        if normalized in _BOOL_FALSE:
            return False
        raise ValueError(f"expected a boolean ({'/'.join(_BOOL_TRUE + _BOOL_FALSE)}), got {value!r}")
    if isinstance(reference, int) and not isinstance(reference, bool):
        return int(value)
    if isinstance(reference, float):
        return float(value)
    return value


@dataclass(frozen=True)
class ExecutionConfig:
    """Execution-agent settings. ``from_env`` folds in the environment."""

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    moomoo_host: str = "127.0.0.1"
    moomoo_port: int = 11111
    trading_env: str = "SIMULATE"  # "SIMULATE" (paper) or "REAL"
    sizing_mode: str = "notional"  # "notional" | "shares" | "percent_of_cash"
    sizing_amount: float = 1000.0
    approval_timeout_seconds: int = 300
    # Delete research logs older than this many days on bot startup.
    log_retention_days: int = 7
    # JSON-encoded rating -> side override, e.g. '{"Buy": "SELL"}' (testing only).
    side_mapping: str | None = None
    # Comma-separated Yahoo tickers the /auto loop researches each day.
    watchlist: str = ""
    # An LLM-provided price farther than this fraction from the live price is
    # rejected as a hallucination and the order falls back to MARKET.
    limit_price_band: float = 0.20
    # /auto: an "Underweight" sell trims to this fraction of the position (a
    # "Sell" is always a full exit). Lets /auto reduce without dumping it all.
    auto_underweight_trim: float = 0.50

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.trading_env not in _TRADING_ENVS:
            raise ValueError(f"trading_env must be one of {_TRADING_ENVS}, got {self.trading_env!r}")
        if self.sizing_mode not in _SIZING_MODES:
            raise ValueError(f"sizing_mode must be one of {_SIZING_MODES}, got {self.sizing_mode!r}")
        if self.sizing_amount <= 0:
            raise ValueError(f"sizing_amount must be positive, got {self.sizing_amount!r}")
        if self.log_retention_days <= 0:
            raise ValueError(f"log_retention_days must be positive, got {self.log_retention_days!r}")
        if self.limit_price_band <= 0:
            raise ValueError(f"limit_price_band must be positive, got {self.limit_price_band!r}")
        if not 0 < self.auto_underweight_trim <= 1:
            raise ValueError(
                f"auto_underweight_trim must be in (0, 1], got {self.auto_underweight_trim!r}"
            )
        if self.side_mapping is not None:
            self.parsed_side_mapping()  # raises on invalid JSON / shape

    def watchlist_tickers(self) -> list[str]:
        """The watchlist as a deduped, order-preserving list of tickers.

        Empty when ``watchlist`` is unset, so the /auto universe degrades to the
        current positions only.
        """
        if not self.watchlist:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for part in self.watchlist.split(","):
            ticker = part.strip()
            if ticker and ticker not in seen:
                seen.add(ticker)
                out.append(ticker)
        return out

    def parsed_side_mapping(self) -> dict[str, str] | None:
        """The rating -> side override as a dict, or None when unset."""
        if self.side_mapping is None:
            return None
        try:
            data = json.loads(self.side_mapping)
        except json.JSONDecodeError as exc:
            raise ValueError(f"side_mapping is not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            raise ValueError("side_mapping must be a JSON object mapping rating -> side")
        return data

    @classmethod
    def from_env(cls) -> ExecutionConfig:
        """Defaults with ``EXECUTION_AGENT_*`` environment (and ``.env``) folded in."""
        load_dotenv()  # real environment wins over .env (override=False)
        kwargs: dict = {}
        for f in fields(cls):
            raw = os.environ.get(f"{_ENV_PREFIX}{f.name.upper()}")
            if raw is None or raw == "":
                continue
            try:
                kwargs[f.name] = _coerce(raw, f.default)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid value for {_ENV_PREFIX}{f.name.upper()}: {exc}"
                ) from exc
        return cls(**kwargs)


DEFAULT_CONFIG = ExecutionConfig()


def mode_label(trading_env: str) -> str:
    """A short human label for a trading_env value, for Telegram messages."""
    return "🔴 REAL (live)" if trading_env == "REAL" else "📄 PAPER (simulate)"
