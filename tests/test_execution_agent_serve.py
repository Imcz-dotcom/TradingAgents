"""Persistent serve bot: chat-command parsing and the one-ticker loop."""

import asyncio

import pytest

import execution_agent.serve as serve_mod
import execution_agent.state as state_mod
from execution_agent.config import ExecutionConfig
from execution_agent.decision import DecisionResult
from execution_agent.notify import APPROVE, TelegramNotifier

# ---- parse_ticker_command --------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("NVDA", "NVDA"),
        ("nvda", "nvda"),
        ("/run NVDA", "NVDA"),
        ("/run NVDA please", "NVDA"),
        ("/analyze 0700.HK", "0700.HK"),
        ("/start AAPL", "AAPL"),
        ("  NVDA  ", "NVDA"),
    ],
)
def test_parse_ticker_command(text, expected):
    assert serve_mod.parse_ticker_command(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "/run", "/analyze   "])
def test_parse_ticker_command_empty(text):
    assert serve_mod.parse_ticker_command(text) == ""


# ---- parse_mode_command ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "is_mode", "arg"),
    [
        ("/mode", True, None),
        ("/MODE", True, None),
        ("/mode paper", True, "paper"),
        ("/mode real", True, "real"),
        ("/mode REAL", True, "real"),
        ("  /mode paper  ", True, "paper"),
        ("NVDA", False, None),
        ("/run NVDA", False, None),
        ("/mode fast", False, None),
        ("", False, None),
        (None, False, None),
    ],
)
def test_parse_mode_command(text, is_mode, arg):
    assert serve_mod.parse_mode_command(text) == (is_mode, arg)


# ---- wait_for_ticker -------------------------------------------------------


class FakeMessage:
    def __init__(self, chat_id, text=None):
        self.chat_id = chat_id
        self.text = text


class FakeUpdate:
    def __init__(self, update_id, message=None):
        self.update_id = update_id
        self.message = message


class FakeBot:
    def __init__(self, polls):
        self.polls = list(polls)

    async def get_updates(self, offset=None, timeout=0, allowed_updates=None):
        return self.polls.pop(0) if self.polls else []


def test_wait_for_ticker_returns_first_text():
    bot = FakeBot(polls=[[FakeUpdate(1, message=FakeMessage(111, "NVDA"))]])
    notifier = TelegramNotifier("tok", 111, bot=bot)

    assert asyncio.run(notifier.wait_for_ticker()) == "NVDA"


# ---- serve_loop (one ticker, then the loop is broken) -----------------------


class FakeGraph:
    def __init__(self, debug=False, config=None):
        pass


def _decision():
    return DecisionResult(
        yahoo="NVDA",
        moomoo="US.NVDA",
        signal="Buy",
        side="BUY",
        summary="Strong momentum.",
        cash=None,
        position_quantity=None,
    )


class FakeNotifier:
    """Returns one ticker, then breaks the loop on the next poll."""

    instances = []

    def __init__(self, token, chat_id, first_reply="NVDA"):
        self.sent = []
        self.calls = 0
        self.first_reply = first_reply
        FakeNotifier.instances.append(self)

    async def send(self, text, buttons=None):
        self.sent.append(text)

    async def drain(self):
        pass

    async def watch_for_cancel(self, cancelled):
        pass  # default: no stop arrives during research

    async def wait_for_ticker(self):
        self.calls += 1
        if self.calls == 1:
            return self.first_reply
        raise RuntimeError("stop")


def _wire(monkeypatch, tmp_path, notifier_cls):
    FakeNotifier.instances.clear()
    monkeypatch.setattr(serve_mod, "TelegramNotifier", notifier_cls)
    monkeypatch.setattr(serve_mod, "TradingAgentsGraph", FakeGraph)
    monkeypatch.setattr(serve_mod, "run_decision_streaming", lambda *a, **k: _decision())
    monkeypatch.setattr(state_mod, "audit_path", lambda: str(tmp_path / "audit.jsonl"))


def test_serve_loop_runs_one_ticker(monkeypatch, tmp_path):
    placed = []

    async def fake_approve_and_place(notifier, config, decision, audit, dry_run, trading_env=None):
        placed.append((decision.yahoo, dry_run, trading_env))
        return "✅ done"

    monkeypatch.setattr(serve_mod, "approve_and_place", fake_approve_and_place)
    _wire(monkeypatch, tmp_path, FakeNotifier)

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(
            serve_mod.serve_loop(ExecutionConfig(telegram_bot_token="tok", telegram_chat_id="123"))
        )

    assert placed == [("NVDA", False, "SIMULATE")]  # one run, paper, live (not dry-run)


def test_serve_loop_drains_stale_updates_before_listening(monkeypatch, tmp_path):
    """A restart must drop pending messages first, not replay an old ticker."""
    class TrackingNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id)
            self.order = []

        async def drain(self):
            self.order.append("drain")

        async def wait_for_ticker(self):
            self.order.append("wait")
            self.calls += 1
            if self.calls == 1:
                return "NVDA"
            raise RuntimeError("stop")

    _wire(monkeypatch, tmp_path, TrackingNotifier)
    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(
            serve_mod.serve_loop(ExecutionConfig(telegram_bot_token="tok", telegram_chat_id="123"))
        )

    # drain must run before the first wait_for_ticker poll.
    assert TrackingNotifier.instances[0].order[0] == "drain"
    assert TrackingNotifier.instances[0].order[1] == "wait"


def test_serve_loop_nudges_on_empty_command(monkeypatch, tmp_path):
    class EmptyNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, first_reply="   ")

    _wire(monkeypatch, tmp_path, EmptyNotifier)
    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(
            serve_mod.serve_loop(ExecutionConfig(telegram_bot_token="tok", telegram_chat_id="123"))
        )

    # A blank message produces a "send a ticker" nudge, not a run.
    assert "ticker" in FakeNotifier.instances[0].sent[1]


def test_serve_loop_stop_word_does_not_run(monkeypatch, tmp_path):
    class StopNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, first_reply="stop")

    _wire(monkeypatch, tmp_path, StopNotifier)
    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(
            serve_mod.serve_loop(ExecutionConfig(telegram_bot_token="tok", telegram_chat_id="123"))
        )

    # "stop" while idle is acknowledged, and never starts a research run.
    assert any("Stopped" in s for s in StopNotifier.instances[0].sent)


def test_serve_loop_switches_to_real_mode(monkeypatch, tmp_path):
    """``/mode real`` asks for confirmation, then the next trade runs live."""
    placed = []

    async def fake_approve_and_place(notifier, config, decision, audit, dry_run, trading_env=None):
        placed.append(trading_env)
        return "✅ done"

    class ModeNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, first_reply="/mode real")
            self.replies = ["/mode real", "NVDA"]
            self.confirmations = []

        async def wait_for_ticker(self):
            if self.replies:
                return self.replies.pop(0)
            raise RuntimeError("stop")

        async def ask_button(self, prompt, buttons, timeout_seconds):
            self.confirmations.append((prompt, [b for b, _ in buttons]))
            return APPROVE

    monkeypatch.setattr(serve_mod, "approve_and_place", fake_approve_and_place)
    _wire(monkeypatch, tmp_path, ModeNotifier)

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(
            serve_mod.serve_loop(ExecutionConfig(telegram_bot_token="tok", telegram_chat_id="123"))
        )

    # The trade after the switch runs in REAL, not the paper default.
    assert placed == ["REAL"]
    # Going live required an explicit "go live" confirmation, not a silent switch.
    assert ModeNotifier.instances[0].confirmations
    assert any("REAL trading" in p for p, _ in ModeNotifier.instances[0].confirmations)
    assert any("Switched to 🔴 REAL" in s for s in ModeNotifier.instances[0].sent)


def test_serve_loop_stop_during_research_cancels(monkeypatch, tmp_path):
    """A "stop" typed while research runs skips the order, instead of being lost."""
    placed = []

    async def fake_approve_and_place(notifier, config, decision, audit, dry_run, trading_env=None):
        placed.append(trading_env)
        return "✅ done"

    class StopDuringResearchNotifier(FakeNotifier):
        async def watch_for_cancel(self, cancelled):
            cancelled.set()  # the user sent "stop" mid-research

    monkeypatch.setattr(serve_mod, "approve_and_place", fake_approve_and_place)
    _wire(monkeypatch, tmp_path, StopDuringResearchNotifier)

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(
            serve_mod.serve_loop(ExecutionConfig(telegram_bot_token="tok", telegram_chat_id="123"))
        )

    # No approval flow ran — the run was cancelled before any order was proposed.
    assert placed == []
    assert any("Stopped" in s for s in StopDuringResearchNotifier.instances[0].sent)
