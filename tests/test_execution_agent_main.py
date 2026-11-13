"""main: full summary to Telegram, and the approve_and_place chat flow."""

import asyncio

import pytest

import execution_agent.main as main_mod
from execution_agent.config import ExecutionConfig
from execution_agent.decision import DecisionResult
from execution_agent.main import TELEGRAM_MAX_CHARS, _fit_telegram
from execution_agent.notify import APPROVE, BUY, SELL, SHARES, SKIP, STOP, USD


# ---- _fit_telegram (no 300-char truncation) ---------------------------------


def test_fit_telegram_passes_full_summary_through():
    # A summary longer than the old 300-char "preview" must arrive intact.
    summary = "a" * 2000
    assert _fit_telegram(summary) == summary


def test_fit_telegram_strips_surrounding_whitespace():
    assert _fit_telegram("  hi  ") == "hi"


def test_fit_telegram_truncates_only_at_telegram_limit():
    out = _fit_telegram("b" * 5000)
    assert len(out) <= TELEGRAM_MAX_CHARS  # never exceeds Telegram's hard cap
    assert out.endswith("…")


def test_fit_telegram_respects_a_custom_budget():
    # Callers subtract their own header/footer before passing the limit in.
    out = _fit_telegram("c" * 100, limit=50)
    assert len(out) == 50
    assert out.endswith("…")


# ---- approve_and_place (AI decides, human can override) ---------------------


def _decision(side="BUY", signal="Buy"):
    return DecisionResult(
        yahoo="NVDA",
        moomoo="US.NVDA",
        signal=signal,
        side=side,
        summary="Strong momentum.",
        cash=None,
        position_quantity=None,
    )


class ScriptedNotifier:
    """Returns scripted button/text replies and records every prompt sent."""

    def __init__(self, buttons, texts=()):
        self._buttons = list(buttons)
        self._texts = list(texts)
        self.prompts = []

    async def ask_button(self, prompt, buttons, timeout_seconds):
        self.prompts.append((prompt, [b for b, _ in buttons]))
        return self._buttons.pop(0) if self._buttons else None

    async def ask_text(self, prompt, timeout_seconds):
        self.prompts.append((prompt,))
        return self._texts.pop(0) if self._texts else None


class FakeBroker:
    instances = []

    def __init__(self, *, host, port, trading_env):
        self.host, self.port, self.trading_env = host, port, trading_env
        self.placed = []
        FakeBroker.instances.append(self)

    def last_price(self, symbol):
        return 45.0

    def place_order(self, symbol, side, quantity):
        self.placed.append((symbol, side, quantity))
        return {"order_id": 123, "order_status": "SUBMITTED"}


@pytest.fixture
def broker(monkeypatch):
    FakeBroker.instances.clear()
    monkeypatch.setattr(main_mod, "MoomooBroker", FakeBroker)
    return FakeBroker


def _config():
    return ExecutionConfig(telegram_bot_token="tok", telegram_chat_id="1")


def test_approve_and_place_flips_direction_to_sell(broker, tmp_path):
    notifier = ScriptedNotifier(buttons=[SELL, SHARES, APPROVE], texts=["10"])
    audit = str(tmp_path / "a.jsonl")

    summary = asyncio.run(
        main_mod.approve_and_place(notifier, _config(), _decision(side="BUY"), audit, False)
    )

    assert "Order placed" in summary
    assert FakeBroker.instances[-1].placed == [("US.NVDA", "SELL", 10)]
    # The first prompt offers Buy/Sell/Skip and shows the AI's BUY recommendation.
    assert notifier.prompts[0][1] == ["🟢 Buy", "🔴 Sell", "⏭ Skip"]


def test_approve_and_place_confirms_ai_direction(broker, tmp_path):
    notifier = ScriptedNotifier(buttons=[BUY, USD, APPROVE], texts=["1000"])
    audit = str(tmp_path / "b.jsonl")

    summary = asyncio.run(
        main_mod.approve_and_place(notifier, _config(), _decision(side="BUY"), audit, False)
    )

    assert "Order placed" in summary
    # notional sizing: 1000 USD / 45 = 22 shares
    assert FakeBroker.instances[-1].placed == [("US.NVDA", "BUY", 22)]


def test_approve_and_place_skip_places_nothing(broker, tmp_path):
    notifier = ScriptedNotifier(buttons=[SKIP])
    audit = str(tmp_path / "c.jsonl")

    summary = asyncio.run(
        main_mod.approve_and_place(notifier, _config(), _decision(side="BUY"), audit, False)
    )

    assert summary == ""
    assert FakeBroker.instances == []  # skip short-circuits before any broker call


def test_approve_and_place_stop_word_cancels(broker, tmp_path):
    # Confirm Buy, then type "stop" instead of an amount -> no order, no broker.
    notifier = ScriptedNotifier(buttons=[BUY], texts=[STOP])
    audit = str(tmp_path / "d.jsonl")

    summary = asyncio.run(
        main_mod.approve_and_place(notifier, _config(), _decision(side="BUY"), audit, False)
    )

    # A stop is acknowledged as "Stopped", not silently folded into a blank cancel.
    assert "Stopped" in summary
    assert FakeBroker.instances == []


def test_approve_and_place_uses_live_mode_override(broker, tmp_path):
    # A REAL override routes the order to a live broker and says so up front.
    notifier = ScriptedNotifier(buttons=[BUY, USD, APPROVE], texts=["1000"])
    audit = str(tmp_path / "e.jsonl")

    summary = asyncio.run(
        main_mod.approve_and_place(
            notifier, _config(), _decision(side="BUY"), audit, False, trading_env="REAL"
        )
    )

    assert "Order placed" in summary
    assert FakeBroker.instances[-1].trading_env == "REAL"
    # Both the direction prompt and the final approve prompt restate the mode.
    assert "REAL" in notifier.prompts[0][0]
    assert "REAL" in notifier.prompts[-1][0]
