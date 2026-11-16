"""Telegram notifier: send + wait for text/button replies, with timeouts.

Uses a fake Bot (no network) and ``asyncio.run`` so the async conversation code
is exercised without ``pytest-asyncio``. ``python-telegram-bot`` is installed as
an extra, so button markup is built and asserted for real.
"""

import asyncio

import pytest

from execution_agent.notify import (
    APPROVE,
    REJECT,
    SHARES,
    STOP,
    USD,
    TelegramNotifier,
    is_stop,
    to_telegram_html,
)


class FakeMessage:
    def __init__(self, chat_id, text=None):
        self.chat_id = chat_id
        self.text = text


class FakeCallbackQuery:
    def __init__(self, query_id, chat_id, data):
        self.id = query_id
        self.data = data
        self.message = FakeMessage(chat_id)


class FakeUpdate:
    def __init__(self, update_id, message=None, callback_query=None):
        self.update_id = update_id
        self.message = message
        self.callback_query = callback_query


class FakeBot:
    """Scripts get_updates: each call pops and returns the next list."""

    def __init__(self, polls):
        self.polls = list(polls)
        self.sent = []
        self.answered = []
        self.allowed = []  # allowed_updates per get_updates call

    async def send_message(
        self, chat_id, text, reply_markup=None, read_timeout=None, parse_mode=None
    ):
        self.sent.append((chat_id, text, reply_markup))

    async def get_updates(self, offset=None, timeout=0, allowed_updates=None):
        self.allowed.append(allowed_updates)
        return self.polls.pop(0) if self.polls else []

    async def answer_callback_query(self, callback_query_id):
        self.answered.append(callback_query_id)


def _run(coro):
    return asyncio.run(coro)


# ---- send (retry policy) ----------------------------------------------------


def test_send_does_not_duplicate_on_timeout():
    """A read timeout means the message may already be delivered — never retry."""
    from telegram.error import TimedOut

    class TimeoutBot:
        def __init__(self):
            self.calls = 0

        async def send_message(
            self, chat_id, text, reply_markup=None, read_timeout=None, parse_mode=None
        ):
            self.calls += 1
            raise TimedOut("Timed out")

    bot = TimeoutBot()
    _run(TelegramNotifier("tok", 111, bot=bot).send("hi"))
    assert bot.calls == 1  # one attempt, no duplicate


def test_drain_confirms_pending_updates():
    """Startup drain advances past a stale message so it isn't replayed."""
    bot = FakeBot(polls=[[FakeUpdate(5, message=FakeMessage(111, "NVDA"))]])
    notifier = TelegramNotifier("tok", 111, bot=bot)

    _run(notifier.drain())

    assert notifier._offset == 6  # stale update confirmed; a fresh poll skips it


def test_send_retries_on_connection_failure():
    """A connection failure means nothing was sent — safe to retry a few times."""
    from telegram.error import NetworkError

    class FlakyBot:
        def __init__(self):
            self.calls = 0

        async def send_message(
            self, chat_id, text, reply_markup=None, read_timeout=None, parse_mode=None
        ):
            self.calls += 1
            if self.calls < 3:
                raise NetworkError("boom")
            return None

    bot = FlakyBot()
    _run(TelegramNotifier("tok", 111, bot=bot).send("hi"))
    assert bot.calls == 3  # retried the connection failure


# ---- HTML formatting -------------------------------------------------------


def test_to_telegram_html_renders_bold_and_code():
    assert (
        to_telegram_html("**Rating**: Buy — try `/execute`")
        == "<b>Rating</b>: Buy — try <code>/execute</code>"
    )


def test_to_telegram_html_escapes_html():
    assert to_telegram_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_to_telegram_html_plain_text_unchanged():
    assert to_telegram_html("just words") == "just words"


def test_send_formats_as_html():
    class RecordingBot(FakeBot):
        def __init__(self):
            super().__init__(polls=[])
            self.parse_modes = []

        async def send_message(
            self, chat_id, text, reply_markup=None, read_timeout=None, parse_mode=None
        ):
            self.parse_modes.append(parse_mode)
            self.sent.append((chat_id, text, reply_markup))

    bot = RecordingBot()
    _run(TelegramNotifier("tok", 111, bot=bot).send("**bold** and `code`"))

    assert bot.parse_modes == ["HTML"]
    assert bot.sent[0][1] == "<b>bold</b> and <code>code</code>"


def test_send_falls_back_to_plain_on_bad_html():
    """Telegram rejecting the HTML must not lose the message — resend plain."""
    from telegram.error import BadRequest

    class PickyBot(FakeBot):
        def __init__(self):
            super().__init__(polls=[])
            self.calls = []

        async def send_message(
            self, chat_id, text, reply_markup=None, read_timeout=None, parse_mode=None
        ):
            self.calls.append((text, parse_mode))
            if parse_mode == "HTML":
                raise BadRequest("can't parse entities")

    bot = PickyBot()
    _run(TelegramNotifier("tok", 111, bot=bot).send("**odd** markup"))

    assert bot.calls[0][1] == "HTML"
    assert bot.calls[1] == ("**odd** markup", None)  # original text, plain


# ---- wait_for_ticker button taps -------------------------------------------


def test_wait_for_ticker_returns_command_from_button_tap():
    """A tapped button whose data is a command acts exactly like typing it."""
    bot = FakeBot(
        polls=[[FakeUpdate(1, callback_query=FakeCallbackQuery("cq1", 111, "/execute NVDA"))]]
    )
    notifier = TelegramNotifier("tok", 111, bot=bot)

    assert _run(notifier.wait_for_ticker()) == "/execute NVDA"
    assert bot.answered == ["cq1"]  # spinner stopped
    assert "callback_query" in bot.allowed[0]


def test_wait_for_ticker_ignores_non_command_callback():
    bot = FakeBot(
        polls=[
            [FakeUpdate(1, callback_query=FakeCallbackQuery("cq1", 111, "approve"))],
            [FakeUpdate(2, message=FakeMessage(111, "NVDA"))],
        ]
    )
    notifier = TelegramNotifier("tok", 111, bot=bot)

    # The stray non-command callback (stale button) is skipped, not returned.
    assert _run(notifier.wait_for_ticker()) == "NVDA"


# ---- ask_text --------------------------------------------------------------


def test_ask_text_returns_typed_reply():
    bot = FakeBot(polls=[[], [FakeUpdate(1, message=FakeMessage(111, "500"))]])
    notifier = TelegramNotifier("tok", 111, bot=bot)

    result = _run(notifier.ask_text("How much?", 300))

    assert result == "500"
    assert bot.sent[0][1] == "How much?"
    assert bot.sent[0][2] is None  # no buttons on a text prompt


def test_ask_text_timeout_returns_none():
    bot = FakeBot(polls=[[]])
    notifier = TelegramNotifier("tok", 111, bot=bot)

    assert _run(notifier.ask_text("How much?", 0)) is None


def test_ask_text_ignores_other_chat():
    bot = FakeBot(
        polls=[
            [],
            [
                FakeUpdate(1, message=FakeMessage(999, "500")),
                FakeUpdate(2, message=FakeMessage(111, "600")),
            ],
        ]
    )
    notifier = TelegramNotifier("tok", 111, bot=bot)

    assert _run(notifier.ask_text("How much?", 300)) == "600"


# ---- ask_button ------------------------------------------------------------


def test_ask_button_returns_data_and_answers():
    bot = FakeBot(polls=[[], [FakeUpdate(1, callback_query=FakeCallbackQuery("cq1", 111, USD))]])
    notifier = TelegramNotifier("tok", 111, bot=bot)

    result = _run(
        notifier.ask_button(
            "Dollars or shares?", [("Dollars", USD), ("Shares", SHARES)], 300
        )
    )

    assert result == USD
    assert bot.answered == ["cq1"]

    text, markup = bot.sent[0][1], bot.sent[0][2]
    assert text == "Dollars or shares?"
    row = markup.inline_keyboard[0]
    assert [(b.text, b.callback_data) for b in row] == [("Dollars", USD), ("Shares", SHARES)]


def test_ask_button_ignores_unexpected_callback_data():
    bot = FakeBot(
        polls=[
            [],
            [
                FakeUpdate(1, callback_query=FakeCallbackQuery("cq1", 111, "other")),
                FakeUpdate(2, callback_query=FakeCallbackQuery("cq2", 111, APPROVE)),
            ],
        ]
    )
    notifier = TelegramNotifier("tok", 111, bot=bot)

    result = _run(
        notifier.ask_button("Approve?", [("Approve", APPROVE), ("Reject", REJECT)], 300)
    )

    assert result == APPROVE
    assert bot.answered == ["cq2"]  # only the matching button is acknowledged


def test_ask_button_timeout_returns_none():
    bot = FakeBot(polls=[[]])
    notifier = TelegramNotifier("tok", 111, bot=bot)

    assert _run(notifier.ask_button("Approve?", [("Approve", APPROVE)], 0)) is None
    assert bot.answered == []


# ---- stop words -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["stop", "STOP", " Stop ", "/stop", "/cancel", "cancel", "abort", "quit"],
)
def test_is_stop_true(text):
    assert is_stop(text) is True


@pytest.mark.parametrize("text", ["", "  ", "stocks", "NVDA", "500", "/run NVDA", None])
def test_is_stop_false(text):
    assert is_stop(text) is False


def test_ask_text_returns_stop_on_stop_word():
    bot = FakeBot(polls=[[], [FakeUpdate(1, message=FakeMessage(111, "stop"))]])
    notifier = TelegramNotifier("tok", 111, bot=bot)

    assert _run(notifier.ask_text("How much?", 300)) == STOP


def test_ask_button_returns_stop_on_stop_word():
    bot = FakeBot(polls=[[], [FakeUpdate(1, message=FakeMessage(111, "/cancel"))]])
    notifier = TelegramNotifier("tok", 111, bot=bot)

    assert _run(notifier.ask_button("Approve?", [("Approve", APPROVE)], 300)) == STOP


def test_drain_pending_surfaces_a_pending_stop():
    """A stop already in the queue (typed during research) aborts the next prompt."""
    bot = FakeBot(polls=[[FakeUpdate(1, message=FakeMessage(111, "stop"))]])
    notifier = TelegramNotifier("tok", 111, bot=bot)

    assert _run(notifier._drain_pending()) == STOP
    assert notifier._offset == 2  # consumed, so a later poll won't re-see it


def test_drain_pending_drops_non_stop_messages():
    """A stale ticker in the queue is still dropped, not surfaced as an answer."""
    bot = FakeBot(polls=[[FakeUpdate(1, message=FakeMessage(111, "NVDA"))]])
    notifier = TelegramNotifier("tok", 111, bot=bot)

    assert _run(notifier._drain_pending()) is None
    assert notifier._offset == 2


def test_send_fits_overlong_text_to_telegram_cap():
    """send() enforces the 4096 cap itself — no caller can crash the bot."""
    bot = FakeBot(polls=[])
    _run(TelegramNotifier("tok", 111, bot=bot).send("x" * 10_000))

    assert len(bot.sent[0][1]) <= 4096


def test_send_uses_plain_when_escaping_overflows_cap():
    """HTML escaping can grow text past 4096 — then the plain text is sent."""

    class RecordingBot(FakeBot):
        def __init__(self):
            super().__init__(polls=[])
            self.parse_modes = []

        async def send_message(
            self, chat_id, text, reply_markup=None, read_timeout=None, parse_mode=None
        ):
            self.parse_modes.append(parse_mode)
            self.sent.append((chat_id, text, reply_markup))

    bot = RecordingBot()
    _run(TelegramNotifier("tok", 111, bot=bot).send("&" * 4000))

    assert bot.parse_modes == [None]  # plain — the escaped form would be 5x the cap
    assert bot.sent[0][1] == "&" * 4000


def test_send_renders_multi_row_buttons():
    bot = FakeBot(polls=[])
    _run(
        TelegramNotifier("tok", 111, bot=bot).send(
            "orders", buttons=[[("❌ #1", "/cancel 1")], [("❌ #2", "/cancel 2")]]
        )
    )

    markup = bot.sent[0][2]
    assert [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard] == [
        [("❌ #1", "/cancel 1")],
        [("❌ #2", "/cancel 2")],
    ]
