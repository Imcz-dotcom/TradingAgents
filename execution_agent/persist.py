"""Per-day history: persist every research run and chat turn across restarts.

The serve bot keeps a day's worth of state in memory — every ticker researched
today (its decision, with the full research state, and whether it was executed)
plus the running ``/chat`` conversation. That in-memory state dies on restart,
which makes ``/chat`` and ``/execute`` forget everything. This module persists it
to disk keyed by the day's date, so a restart within the same day reloads it and
the bot picks up where it left off; a new day naturally starts fresh (the stored
date no longer matches today).

The full graph state cannot be JSON-serialized verbatim — it carries LangChain
message objects — so only the JSON-safe slice the ``/chat`` agent actually reads
is kept: the four analyst reports, the bull/bear and risk debates, the plans,
the final decision, and the past lessons.
"""

from __future__ import annotations

import json
import os

from tradingagents.dataflows.files import locked

DEFAULT_HISTORY_PATH = os.path.join(
    os.path.expanduser("~"), ".tradingagents", "execution", "research_history.json"
)

# final_state keys the /chat agent reasons over. Everything else — notably the
# LangChain ``messages`` list — is not JSON-safe and not needed, so it is dropped.
_RESEARCH_KEYS = (
    "instrument_context",
    "market_report",
    "sentiment_report",
    "news_report",
    "fundamentals_report",
    "trader_investment_plan",
    "investment_plan",
    "final_trade_decision",
    "past_context",
)


def history_path() -> str:
    """Where the day's history is remembered; overridable for tests."""
    return os.environ.get("EXECUTION_AGENT_HISTORY_PATH") or DEFAULT_HISTORY_PATH


def research_subset(final_state: dict | None) -> dict:
    """The JSON-safe slice of a final state: exactly what ``/chat`` needs."""
    if not final_state:
        return {}
    out: dict = {}
    for key in _RESEARCH_KEYS:
        value = final_state.get(key)
        if value:
            out[key] = value if isinstance(value, str) else str(value)
    # Debate/risk states hold a mix of transcripts and message objects; keep only
    # the string transcripts (what the /chat transcript reads).
    for state_key, string_keys in (
        ("investment_debate_state", ("bull_history", "bear_history")),
        ("risk_debate_state", ("aggressive_history", "conservative_history", "neutral_history")),
    ):
        state = final_state.get(state_key)
        if isinstance(state, dict):
            debate = {k: state[k] for k in string_keys if isinstance(state.get(k), str) and state[k]}
            if debate:
                out[state_key] = debate
    return out


def save_history(day: dict) -> None:
    """Atomically write the current day's history to disk."""
    path = history_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = json.dumps(day, ensure_ascii=False, default=str)
    with locked(path), open(path, "w", encoding="utf-8") as handle:
        handle.write(payload)


def load_history() -> dict | None:
    """Read the saved day's history, or None when absent or unreadable."""
    path = history_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return None
