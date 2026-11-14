"""Persistent serve bot: command parsing and the research/execute/chat loop."""

import asyncio
import json
from datetime import date

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
        ("/research NVDA", "NVDA"),
        ("/research NVDA please", "NVDA"),
        ("/run NVDA", "NVDA"),
        ("/analyze 0700.HK", "0700.HK"),
        ("/start AAPL", "AAPL"),
        ("  NVDA  ", "NVDA"),
    ],
)
def test_parse_ticker_command(text, expected):
    assert serve_mod.parse_ticker_command(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "/research", "/run", "/analyze   ", None])
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


# ---- parse /execute and /chat ----------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["/execute", "/EXECUTE", "/execute now", "  /execute  "],
)
def test_is_execute_command_true(text):
    assert serve_mod.is_execute_command(text) is True


@pytest.mark.parametrize("text", ["execute", "NVDA", "/run NVDA", "/chat hi", "", None])
def test_is_execute_command_false(text):
    assert serve_mod.is_execute_command(text) is False


@pytest.mark.parametrize(
    "text",
    ["/auto", "/AUTO", "/auto now", "  /auto  "],
)
def test_is_auto_command_true(text):
    assert serve_mod.is_auto_command(text) is True


@pytest.mark.parametrize("text", ["auto", "NVDA", "/execute", "/autopilot", "", None])
def test_is_auto_command_false(text):
    assert serve_mod.is_auto_command(text) is False


# ---- parse_auto_command ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/auto", (True, None, None)),
        ("/AUTO", (True, None, None)),
        ("  /auto  ", (True, None, None)),
        ("/auto 500", (True, 500.0, None)),
        ("/auto $500", (True, 500.0, None)),
        ("/auto 500 usd", (True, 500.0, None)),
        ("/auto 10.5", (True, 10.5, None)),
    ],
)
def test_parse_auto_command(text, expected):
    assert serve_mod.parse_auto_command(text) == expected


@pytest.mark.parametrize("text", ["auto", "NVDA", "/execute", "/autopilot", "", None])
def test_parse_auto_command_not_auto(text):
    assert serve_mod.parse_auto_command(text) == (False, None, None)


def test_parse_auto_command_rejects_shares():
    is_auto, budget, err = serve_mod.parse_auto_command("/auto 10 shares")
    assert (is_auto, budget) == (True, None)
    assert "dollars" in err


def test_parse_auto_command_rejects_garbage():
    is_auto, budget, err = serve_mod.parse_auto_command("/auto loads of money")
    assert (is_auto, budget) == (True, None)
    assert "budget" in err


@pytest.mark.parametrize(
    ("text", "is_chat", "question"),
    [
        ("/chat what's the risk?", True, "what's the risk?"),
        ("/chat", True, ""),
        ("/chat   ", True, ""),
        ("/CHAT hello", True, "hello"),
        ("NVDA", False, ""),
        ("/execute", False, ""),
        ("", False, ""),
        (None, False, ""),
    ],
)
def test_parse_chat_command(text, is_chat, question):
    assert serve_mod.parse_chat_command(text) == (is_chat, question)


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


# ---- serve_loop ------------------------------------------------------------


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
        research={"final_trade_decision": "Buy NVDA", "market_report": "solid"},
    )


def _entry(
    yahoo="NVDA",
    moomoo="US.NVDA",
    signal="Buy",
    side="BUY",
    summary="Strong momentum.",
    executed=False,
    research=None,
):
    """A persisted research entry in the day format, matching _research_to_entry."""
    return {
        "executed": executed,
        "decision": {
            "yahoo": yahoo,
            "moomoo": moomoo,
            "signal": signal,
            "side": side,
            "summary": summary,
            "cash": None,
            "position_quantity": None,
            "research": research
            or {"final_trade_decision": "Buy NVDA", "market_report": "solid"},
        },
    }


class FakeNotifier:
    """Returns scripted replies in order, then breaks the loop with "stop"."""

    instances = []

    def __init__(self, token, chat_id, replies=("NVDA",)):
        self.sent = []
        self.replies = list(replies)
        self.confirmations = []
        FakeNotifier.instances.append(self)

    async def send(self, text, buttons=None):
        self.sent.append(text)

    async def drain(self):
        pass

    async def watch_for_cancel(self, cancelled):
        # No stop arrives: block until cancelled, like the real watcher with no
        # incoming messages. (Returning immediately would read as "stop seen".)
        while True:
            await asyncio.sleep(1)

    async def ask_button(self, prompt, buttons, timeout_seconds):
        self.confirmations.append((prompt, [b for b, _ in buttons]))
        return APPROVE

    async def ask_text(self, prompt, timeout_seconds):
        return "1000"

    async def wait_for_ticker(self):
        if self.replies:
            return self.replies.pop(0)
        raise RuntimeError("stop")


def _wire(monkeypatch, tmp_path, notifier_cls):
    FakeNotifier.instances.clear()
    monkeypatch.setattr(serve_mod, "TelegramNotifier", notifier_cls)
    monkeypatch.setattr(serve_mod, "TradingAgentsGraph", FakeGraph)
    monkeypatch.setattr(serve_mod, "run_decision_streaming", lambda *a, **k: _decision())
    monkeypatch.setattr(state_mod, "audit_path", lambda: str(tmp_path / "audit.jsonl"))
    # Persistence writes to the test tmp dir, never the real ~/.tradingagents.
    monkeypatch.setattr(serve_mod.persist, "history_path", lambda: str(tmp_path / "research_history.json"))


def _config():
    return ExecutionConfig(telegram_bot_token="tok", telegram_chat_id="123")


def test_serve_loop_research_stores_and_sends_summary(monkeypatch, tmp_path):
    """A bare ticker researches and sends the summary — it does NOT execute."""
    placed = []

    async def fake_approve_and_place(notifier, config, decision, audit, dry_run, trading_env=None):
        placed.append((decision.yahoo, trading_env))
        return "✅ done"

    monkeypatch.setattr(serve_mod, "approve_and_place", fake_approve_and_place)
    _wire(monkeypatch, tmp_path, FakeNotifier)

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    assert placed == []  # research is fire-and-forget; nothing executed
    sent = FakeNotifier.instances[0].sent
    assert any("TradingAgents says BUY NVDA" in s for s in sent)
    assert any("/execute" in s for s in sent)


def test_serve_loop_reuses_same_day_research(monkeypatch, tmp_path):
    """Re-asking the same ticker the same day reuses the run, not re-running."""
    calls = []
    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    class RepeatNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("NVDA", "NVDA"))

    _wire(monkeypatch, tmp_path, RepeatNotifier)
    monkeypatch.setattr(
        serve_mod,
        "run_decision_streaming",
        lambda *a, **k: calls.append(1) or _decision(),
    )

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    assert len(calls) == 1  # the second "NVDA" is a cache hit, no second pipeline
    sent = RepeatNotifier.instances[0].sent
    assert any("Already researched NVDA today" in s for s in sent)


def test_serve_loop_fresh_forces_rerun(monkeypatch, tmp_path):
    """/research NVDA fresh re-runs even though NVDA was already done today."""
    calls = []
    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    class FreshNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("NVDA", "NVDA fresh"))

    _wire(monkeypatch, tmp_path, FreshNotifier)
    monkeypatch.setattr(
        serve_mod,
        "run_decision_streaming",
        lambda *a, **k: calls.append(1) or _decision(),
    )

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    assert len(calls) == 2  # "fresh" bypassed the cache and re-ran
    assert not any("Already researched" in s for s in FreshNotifier.instances[0].sent)


def test_serve_loop_execute_uses_stored_research(monkeypatch, tmp_path):
    """/execute runs the approval flow on the last stored decision."""
    placed = []

    async def fake_approve_and_place(notifier, config, decision, audit, dry_run, trading_env=None):
        placed.append((decision.yahoo, trading_env))
        return "✅ Order placed: 123 (SUBMITTED)"

    monkeypatch.setattr(serve_mod, "approve_and_place", fake_approve_and_place)

    class TwoStepNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("NVDA", "/execute"))

    _wire(monkeypatch, tmp_path, TwoStepNotifier)

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    assert placed == [("NVDA", "SIMULATE")]


def test_serve_loop_execute_without_research_nudges(monkeypatch, tmp_path):
    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    class ExecuteOnlyNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("/execute",))

    _wire(monkeypatch, tmp_path, ExecuteOnlyNotifier)

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    assert any("No research yet" in s for s in ExecuteOnlyNotifier.instances[0].sent)


def test_serve_loop_chat_answers(monkeypatch, tmp_path):
    monkeypatch.setattr(
        serve_mod.chat, "answer", lambda *a, **k: "The biggest risk is concentration."
    )
    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    class ChatNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("NVDA", "/chat what's the risk?"))

    _wire(monkeypatch, tmp_path, ChatNotifier)

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    assert any("biggest risk is concentration" in s for s in ChatNotifier.instances[0].sent)


def test_serve_loop_auto_dispatches(monkeypatch, tmp_path):
    """/auto runs the autonomous loop with the current (paper) mode + budget."""
    calls = []

    def fake_run_auto(config, *, budget=None, trading_env=None, audit=None, on_step=None, stop_check=None):
        calls.append((budget, trading_env))
        return "📊 Auto done"

    monkeypatch.setattr(serve_mod, "run_auto", fake_run_auto)
    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    class AutoNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("/auto 500",))

    _wire(monkeypatch, tmp_path, AutoNotifier)

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    assert calls == [(500.0, "SIMULATE")]
    assert any("Auto done" in s for s in AutoNotifier.instances[0].sent)


def test_serve_loop_chat_without_research_nudges(monkeypatch, tmp_path):
    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    class ChatOnlyNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("/chat hello",))

    _wire(monkeypatch, tmp_path, ChatOnlyNotifier)

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    assert any("No research yet" in s for s in ChatOnlyNotifier.instances[0].sent)


def test_serve_loop_restores_todays_research_on_startup(monkeypatch, tmp_path):
    """A restart within the same day reloads the saved research for /chat."""
    day = {
        "date": date.today().isoformat(),
        "researches": {"NVDA": _entry()},
        "chat_history": [],
    }
    (tmp_path / "research_history.json").write_text(json.dumps(day))

    monkeypatch.setattr(serve_mod.chat, "answer", lambda *a, **k: "The biggest risk is concentration.")
    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    class ChatOnlyNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("/chat what's the risk?",))

    _wire(monkeypatch, tmp_path, ChatOnlyNotifier)

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    sent = ChatOnlyNotifier.instances[0].sent
    assert any("biggest risk is concentration" in s for s in sent)
    assert not any("No research yet" in s for s in sent)


def test_serve_loop_restores_all_tickers_and_chat_from_today(monkeypatch, tmp_path):
    """A restart reloads every ticker and the chat history, not just the last one."""
    day = {
        "date": date.today().isoformat(),
        "researches": {
            "NVDA": _entry(yahoo="NVDA", moomoo="US.NVDA"),
            "INTC": _entry(yahoo="INTC", moomoo="US.INTC", summary="Chip play."),
        },
        "chat_history": [{"ticker": "NVDA", "q": "risk?", "a": "concentration"}],
    }
    (tmp_path / "research_history.json").write_text(json.dumps(day))

    answers = []

    def fake_answer(final_state, ticker, history, question):
        answers.append((ticker, list(history), question))
        return "got it"

    monkeypatch.setattr(serve_mod.chat, "answer", fake_answer)
    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    class ChatNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("/chat INTC what now?",))

    _wire(monkeypatch, tmp_path, ChatNotifier)

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    # Both tickers were restored; /chat targeted INTC and carried the old history.
    assert answers[0][0] == "INTC"
    assert answers[0][1] == [{"ticker": "NVDA", "q": "risk?", "a": "concentration"}]
    assert answers[0][2] == "what now?"
    # The chat answer still goes out.
    assert any("got it" in s for s in ChatNotifier.instances[0].sent)


def test_serve_loop_ignores_stale_memory_from_previous_day(monkeypatch, tmp_path):
    """Memory from an earlier day is not restored — a new day starts fresh."""
    day = {
        "date": "2000-01-01",
        "researches": {"NVDA": _entry(summary="Old.")},
        "chat_history": [],
    }
    (tmp_path / "research_history.json").write_text(json.dumps(day))

    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    class ChatOnlyNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("/chat hello",))

    _wire(monkeypatch, tmp_path, ChatOnlyNotifier)

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    assert any("No research yet" in s for s in ChatOnlyNotifier.instances[0].sent)


def test_serve_loop_drains_stale_updates_before_listening(monkeypatch, tmp_path):
    """A restart must drop pending messages first, not replay an old ticker."""
    class TrackingNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("NVDA",))
            self.order = []

        async def drain(self):
            self.order.append("drain")

        async def wait_for_ticker(self):
            self.order.append("wait")
            return await super().wait_for_ticker()

    _wire(monkeypatch, tmp_path, TrackingNotifier)
    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    # drain must run before the first wait_for_ticker poll.
    assert TrackingNotifier.instances[0].order[0] == "drain"
    assert TrackingNotifier.instances[0].order[1] == "wait"


def test_serve_loop_purges_stale_logs_on_startup(monkeypatch, tmp_path):
    """Startup deletes research logs older than the retention window, once."""
    purged = []
    monkeypatch.setattr(
        serve_mod.cleanup,
        "purge_old_logs",
        lambda results_dir, **k: purged.append(results_dir) or 3,
    )
    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    class OneShotNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("NVDA",))

    _wire(monkeypatch, tmp_path, OneShotNotifier)

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    assert len(purged) == 1  # ran once at startup, before the listen loop


def test_serve_loop_nudges_on_empty_command(monkeypatch, tmp_path):
    class EmptyNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("   ",))

    _wire(monkeypatch, tmp_path, EmptyNotifier)
    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    # A blank message produces a "send a ticker" nudge, not a run.
    assert "ticker" in EmptyNotifier.instances[0].sent[1]


def test_serve_loop_stop_word_does_not_run(monkeypatch, tmp_path):
    class StopNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("stop",))

    _wire(monkeypatch, tmp_path, StopNotifier)
    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    # "stop" while idle is acknowledged, and never starts a research run.
    assert any("Stopped" in s for s in StopNotifier.instances[0].sent)


def test_serve_loop_switches_to_real_mode(monkeypatch, tmp_path):
    """``/mode real`` asks for confirmation, then the next execute runs live."""
    placed = []

    async def fake_approve_and_place(notifier, config, decision, audit, dry_run, trading_env=None):
        placed.append(trading_env)
        return "✅ Order placed: 123 (SUBMITTED)"

    class ModeNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("/mode real", "NVDA", "/execute"))

    monkeypatch.setattr(serve_mod, "approve_and_place", fake_approve_and_place)
    _wire(monkeypatch, tmp_path, ModeNotifier)

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

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
        asyncio.run(serve_mod.serve_loop(_config()))

    # No approval flow ran — the run was cancelled before any order was proposed.
    assert placed == []
    assert any("Stopped" in s for s in StopDuringResearchNotifier.instances[0].sent)


# ---- resolve_chat_target ---------------------------------------------------


def test_resolve_chat_target_leading_ticker():
    researches = {"NVDA": object(), "INTC": object()}
    assert serve_mod.resolve_chat_target("NVDA what's the risk?", researches) == (
        "NVDA",
        "what's the risk?",
    )


def test_resolve_chat_target_named_anywhere():
    researches = {"NVDA": object(), "INTC": object()}
    assert serve_mod.resolve_chat_target("compare it to intc", researches) == (
        "INTC",
        "compare it to intc",
    )


def test_resolve_chat_target_defaults_to_latest():
    researches = {"NVDA": object(), "INTC": object()}
    assert serve_mod.resolve_chat_target("what's the risk?", researches) == (
        "INTC",
        "what's the risk?",
    )


def test_resolve_chat_target_empty():
    assert serve_mod.resolve_chat_target("anything", {}) == (None, "anything")


# ---- multi-ticker day ------------------------------------------------------


def _decision_for(ticker):
    return DecisionResult(
        yahoo=ticker,
        moomoo=f"US.{ticker}",
        signal="Buy",
        side="BUY",
        summary=f"Buy {ticker}.",
        cash=None,
        position_quantity=None,
        research={"final_trade_decision": f"Buy {ticker}"},
    )


def test_serve_loop_keeps_multiple_tickers_and_executes_latest(monkeypatch, tmp_path):
    """Researching NVDA then INTC keeps both; /execute trades the latest (INTC)."""
    placed = []

    async def fake_approve_and_place(notifier, config, decision, audit, dry_run, trading_env=None):
        placed.append(decision.yahoo)
        return "✅ Order placed: 123 (SUBMITTED)"

    monkeypatch.setattr(serve_mod, "approve_and_place", fake_approve_and_place)

    class TwoTickersNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("NVDA", "INTC", "/execute"))

    _wire(monkeypatch, tmp_path, TwoTickersNotifier)
    monkeypatch.setattr(
        serve_mod, "run_decision_streaming", lambda graph, ticker, *a, **k: _decision_for(ticker)
    )

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    assert placed == ["INTC"]  # latest wins
    sent = TwoTickersNotifier.instances[0].sent
    assert any("TradingAgents says BUY NVDA" in s for s in sent)
    assert any("TradingAgents says BUY INTC" in s for s in sent)


def test_serve_loop_chat_targets_named_ticker(monkeypatch, tmp_path):
    """``/chat NVDA ...`` answers from NVDA even after INTC is the latest."""
    targets = []

    def fake_answer(final_state, ticker, history, question):
        targets.append(ticker)
        return f"answered for {ticker}"

    monkeypatch.setattr(serve_mod.chat, "answer", fake_answer)
    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    class NamedChatNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("NVDA", "INTC", "/chat NVDA what's the risk?"))

    _wire(monkeypatch, tmp_path, NamedChatNotifier)
    monkeypatch.setattr(
        serve_mod, "run_decision_streaming", lambda graph, ticker, *a, **k: _decision_for(ticker)
    )

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    assert targets == ["NVDA"]


def test_serve_loop_drops_raw_messages_from_in_memory_research(monkeypatch, tmp_path):
    """The raw agent ``messages`` are dead weight in RAM; /chat sees only the subset."""
    seen = []

    def fake_answer(final_state, ticker, history, question):
        seen.append(final_state)
        return "ok"

    def decision_with_messages():
        d = _decision()
        return DecisionResult(
            yahoo=d.yahoo,
            moomoo=d.moomoo,
            signal=d.signal,
            side=d.side,
            summary=d.summary,
            cash=d.cash,
            position_quantity=d.position_quantity,
            research={
                "final_trade_decision": "Buy NVDA",
                "market_report": "solid",
                "messages": ["raw agent chatter"],
            },
        )

    monkeypatch.setattr(serve_mod.chat, "answer", fake_answer)
    monkeypatch.setattr(
        serve_mod, "run_decision_streaming", lambda graph, ticker, *a, **k: decision_with_messages()
    )
    monkeypatch.setattr(serve_mod, "approve_and_place", lambda *a, **k: "unused")

    class ChatNotifier(FakeNotifier):
        def __init__(self, token, chat_id):
            super().__init__(token, chat_id, replies=("NVDA", "/chat what's the risk?"))

    _wire(monkeypatch, tmp_path, ChatNotifier)

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(serve_mod.serve_loop(_config()))

    assert seen and "messages" not in seen[0]
    assert "market_report" in seen[0]
