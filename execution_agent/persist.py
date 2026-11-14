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
from datetime import date

from execution_agent.decision import DecisionResult
from tradingagents.dataflows.files import locked
from tradingagents.dataflows.symbols import normalize_symbol

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


def decision_from_entry(decision) -> DecisionResult | None:
    """Rebuild a :class:`DecisionResult` from a persisted ``decision`` entry."""
    if not isinstance(decision, dict):
        return None
    try:
        return DecisionResult(
            yahoo=decision["yahoo"],
            moomoo=decision["moomoo"],
            signal=decision["signal"],
            side=decision["side"],
            summary=decision.get("summary", ""),
            cash=decision.get("cash"),
            position_quantity=decision.get("position_quantity"),
            research=decision.get("research") or {},
        )
    except (KeyError, TypeError, ValueError):
        return None


def load_today_decision(yahoo: str) -> DecisionResult | None:
    """A same-day research result for ``yahoo``, or None when absent or stale.

    The serve bot persists every interactive research run to the shared per-day
    history. The /auto loop reads the same file so a ticker researched earlier
    today is reused instead of re-running the full pipeline. Only today's entries
    count — a fresh date re-researches.
    """
    day = load_history()
    if not isinstance(day, dict) or day.get("date") != date.today().isoformat():
        return None
    raw = day.get("researches") or {}
    if not isinstance(raw, dict):
        return None
    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        decision = entry.get("decision")
        if isinstance(decision, dict) and normalize_symbol(str(decision.get("yahoo") or "")) == yahoo:
            return decision_from_entry(decision)
    return None
