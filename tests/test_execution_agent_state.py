"""Append-only audit log: one JSON object per line, in append order."""

import json

from execution_agent.state import append_event, audit_path


def test_append_event_writes_one_line(tmp_path, monkeypatch):
    path = str(tmp_path / "audit.jsonl")
    monkeypatch.setenv("EXECUTION_AGENT_AUDIT_PATH", path)

    append_event(audit_path(), {"event": "decision", "ticker": "NVDA"})

    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "decision"
    assert record["ticker"] == "NVDA"
    assert "ts" in record


def test_append_event_appends_in_order(tmp_path, monkeypatch):
    path = str(tmp_path / "audit.jsonl")
    monkeypatch.setenv("EXECUTION_AGENT_AUDIT_PATH", path)

    for i in range(3):
        append_event(audit_path(), {"event": "step", "n": i})

    with open(path, encoding="utf-8") as handle:
        events = [json.loads(line)["n"] for line in handle]
    assert events == [0, 1, 2]


def test_append_event_stringifies_non_json(tmp_path, monkeypatch):
    path = str(tmp_path / "audit.jsonl")
    monkeypatch.setenv("EXECUTION_AGENT_AUDIT_PATH", path)

    append_event(audit_path(), {"event": "error", "message": {"weird": object()}})

    with open(path, encoding="utf-8") as handle:
        record = json.load(handle)
    assert record["event"] == "error"
    # json.dumps(..., default=str) stringifies the stray object() inside the dict
    assert isinstance(record["message"]["weird"], str)


def test_audit_path_defaults_to_home(monkeypatch):
    monkeypatch.delenv("EXECUTION_AGENT_AUDIT_PATH", raising=False)
    assert ".tradingagents" in audit_path()
    assert audit_path().endswith("audit.jsonl")
