"""Append-only audit trail for every execution-agent action.

One JSON object per line, appended under a cross-process lock so a decision,
proposal, approval outcome, and order result are never interleaved by two runs.
Written to ``~/.tradingagents/execution/audit.jsonl`` by default; override the
path with ``EXECUTION_AGENT_AUDIT_PATH`` (useful for tests). Secrets are never
written here — the caller is responsible for not passing tokens.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

from tradingagents.dataflows.files import locked

DEFAULT_AUDIT_PATH = os.path.join(
    os.path.expanduser("~"), ".tradingagents", "execution", "audit.jsonl"
)


def audit_path() -> str:
    """The audit log path, overridable via ``EXECUTION_AGENT_AUDIT_PATH``."""
    return os.environ.get("EXECUTION_AGENT_AUDIT_PATH") or DEFAULT_AUDIT_PATH


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def append_event(path: str, event: dict) -> None:
    """Append ``event`` as one JSON line, safe under concurrent writers.

    A ``ts`` field (UTC ISO-8601) is prepended automatically. Any non-JSON value
    in ``event`` is stringified via ``default=str`` so a stray object still logs.
    """
    record = {"ts": _utc_now(), **event}
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with locked(path), open(path, "a", encoding="utf-8") as handle:
        handle.write(line)
