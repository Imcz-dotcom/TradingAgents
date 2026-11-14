"""Extract price levels from a research run's rendered reports.

The trader and portfolio manager already emit *structured* price levels
(``TraderProposal.entry_price``/``stop_loss`` and
``PortfolioDecision.price_target`` in ``tradingagents/agents/schemas.py``). Those
are rendered into the report strings as ``**Entry Price**: 189.5``,
``**Stop Loss**: 172.0`` and ``**Price Target**: 200.0``. This module pulls those
numbers back out so the auto loop can place limit orders at the research's own
levels instead of guessing from prose.
"""

from __future__ import annotations

import re

_ENTRY_LABEL = "Entry Price"
_STOP_LABEL = "Stop Loss"
_TARGET_LABEL = "Price Target"
_HORIZON_LABEL = "Time Horizon"

# A bare number. The label line is produced by our own renderer, so the first
# float token after the colon is the level (or "not provided").
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

# A time horizon phrase that means the move is expected *soon* — the order need
# only rest for the day. Anything else (months, years, or unreadable) defaults
# to GTC so a resting limit survives the agent being offline.
_SHORT_HORIZON_TERMS = (
    "day",
    "week",
    "short",
    "near",
    "intraday",
    "immediate",
    "overnight",
)


def _level(text, label: str) -> float | None:
    if not text:
        return None
    match = re.search(rf"\*\*{re.escape(label)}\*\*:\s*(.+)", str(text))
    if not match:
        return None
    value = match.group(1).strip()
    if value.lower() in ("not provided", "none", "n/a"):
        return None
    num = _NUM_RE.search(value)
    if num is None:
        return None
    try:
        return float(num.group(0))
    except ValueError:
        return None


def parse_prices(final_state: dict) -> dict:
    """Extract ``{"entry_price", "stop_loss", "price_target"}`` from ``final_state``.

    Entry and stop levels come from the trader's plan (``trader_investment_plan``);
    the target comes from the portfolio manager (``investment_plan``). Each value
    is a float or ``None`` when absent or unparseable.
    """
    final_state = final_state or {}
    return {
        "entry_price": _level(final_state.get("trader_investment_plan"), _ENTRY_LABEL),
        "stop_loss": _level(final_state.get("trader_investment_plan"), _STOP_LABEL),
        "price_target": _level(final_state.get("investment_plan"), _TARGET_LABEL),
    }


def _str_level(text, label: str) -> str | None:
    """The free-text value after a ``**Label**: value`` line, or None."""
    if not text:
        return None
    match = re.search(rf"\*\*{re.escape(label)}\*\*:\s*(.+)", str(text))
    if not match:
        return None
    value = match.group(1).strip()
    if not value or value.lower() in ("not provided", "none", "n/a"):
        return None
    return value


def parse_time_horizon(final_state: dict) -> str | None:
    """Extract the PM's holding period (``**Time Horizon**: 3-6 months``).

    This is the answer to *"how long does the research expect to hold this
    position?"* — a short free-text phrase like ``"3-6 months"`` or
    ``"short-term"``, or None when the PM left it out.
    """
    final_state = final_state or {}
    return _str_level(final_state.get("investment_plan"), _HORIZON_LABEL)


def time_in_force_for(time_horizon: str | None) -> str:
    """The order lifetime (``"GTC"`` or ``"DAY"``) for a PM time horizon.

    A long horizon (months+) means the fill may take many days, so the limit
    should rest as ``GTC``. A short horizon (days/weeks) means the move is
    expected soon, so ``DAY`` is enough. An absent or unreadable horizon defaults
    to ``GTC`` — these resting limits exist precisely to survive the agent being
    offline, and a silently-dead DAY order would defeat that.
    """
    if not time_horizon:
        return "GTC"
    horizon = time_horizon.lower()
    if any(term in horizon for term in _SHORT_HORIZON_TERMS):
        return "DAY"
    return "GTC"  # months, years, or anything unrecognized -> rest
