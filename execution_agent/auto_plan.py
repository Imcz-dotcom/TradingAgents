"""Persistent, date-aware plan for the autonomous paper-trading loop.

``/auto`` writes a "verdict" each day it runs: the account snapshot, the
universe it looked at, and the per-ticker decision/order. The next ``/auto``
refer-backs to it — if the stored date is today it reuses the verdict; if it is
stale it re-researches. The file is date-keyed (not accumulated), so the agent
can tell "outdated" from "fresh" at a glance, exactly as intended.

The file is a single JSON document (one day, overwritten each run), not a log.
See ``execution_agent/state.py`` for the append-only audit trail.
"""

from __future__ import annotations

import json
import os

from tradingagents.dataflows.files import locked

DEFAULT_AUTO_PLAN_PATH = os.path.join(
    os.path.expanduser("~"), ".tradingagents", "execution", "auto_plan.json"
)


def auto_plan_path() -> str:
    """Where the auto plan is remembered; overridable for tests."""
    return os.environ.get("EXECUTION_AGENT_AUTO_PLAN_PATH") or DEFAULT_AUTO_PLAN_PATH


def save_plan(day: dict) -> None:
    """Atomically write the day's auto plan to disk."""
    path = auto_plan_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = json.dumps(day, ensure_ascii=False, default=str)
    with locked(path), open(path, "w", encoding="utf-8") as handle:
        handle.write(payload)


def load_plan() -> dict | None:
    """Read the saved auto plan, or None when absent or unreadable."""
    path = auto_plan_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return None
