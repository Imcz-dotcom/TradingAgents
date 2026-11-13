"""Telegram approval channel for the execution agent.

Turns a proposed order into a short conversation with the human:

    1. "How much?"          — wait for a typed number.
    2. "dollars or shares?" — wait for an inline-button tap.
    3. "Approve / Reject"   — wait for an inline-button tap.

Every wait has a timeout; on timeout it returns ``None`` and the caller treats
that as *cancel* (approve is opt-in, never implicit). The ``python-telegram-bot``
SDK is imported lazily so this module imports and tests cleanly without it
installed. The bot token is never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

# Inline-button callback data, so the orchestrator and tests share one spelling.
APPROVE = "approve"
REJECT = "reject"
USD = "usd"
SHARES = "shares"
BUY = "buy"
SELL = "sell"
SKIP = "skip"
# Sentinel returned by ask_text/ask_button when the user types a stop word
# instead of answering (or tapping) — never a real button callback.
STOP = "stop"

# Words that abort whatever the bot is waiting on. A leading "/" is stripped so
# Telegram slash commands ("/stop", "/cancel") work too.
STOP_WORDS = ("stop", "cancel", "abort", "quit")


def is_stop(text: str) -> bool:
    """Whether a typed message is a stop request (e.g. "stop", "/cancel")."""
    if not isinstance(text, str):
        return False
    word = text.strip().lower()
    if word.startswith("/"):
        word = word[1:].lstrip()
    return word in STOP_WORDS


class TelegramNotifier:
    """A single-chat conversation driver over the Telegram Bot API."""

    def __init__(self, token: str, chat_id: str | int, bot=None):
        self.token = token
        # Telegram reports chat ids as ints; the .env value arrives as a string.
        # Normalise to int so ``msg.chat_id == self.chat_id`` actually matches.
        try:
            self.chat_id = int(chat_id)
        except (TypeError, ValueError):
            self.chat_id = chat_id
        self._bot = bot  # injected fake in tests; else built lazily
        self._offset = None  # last update_id + 1, so we only read new updates

    @property
    def bot(self):
        if self._bot is None:
            from telegram import Bot  # lazy: SDK is an optional extra

            self._bot = Bot(self.token)
        return self._bot

    async def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        """Send a message, optionally with a row of inline buttons.

        Retries only on a *connection* failure, where nothing was sent. A read
        timeout means Telegram may already have the message (only the ack was
        slow), so retrying would post the same message two or three times.
        """
        reply_markup = None
        if buttons:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            reply_markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(label, callback_data=data) for label, data in buttons]]
            )
        from telegram.error import NetworkError, TimedOut

        last_exc = None
        for attempt in range(3):
            try:
                # A slightly longer read timeout helps a slow/high-latency link
                # (e.g. Telegram over a flaky connection) without stalling.
                await self.bot.send_message(
                    self.chat_id, text, reply_markup=reply_markup, read_timeout=15
                )
                return
            except TimedOut:
                # The request may already have reached Telegram (a read timeout
                # means only the reply was slow) — retrying would post the same
                # message twice or three times. Treat it as sent.
                return
            except NetworkError as exc:
                # Connection never opened (ConnectError/ConnectTimeout), so
                # nothing was sent — safe to retry.
                last_exc = exc
                await asyncio.sleep(2**attempt)  # 1s, 2s, then give up
        raise last_exc

    async def ask_text(self, prompt: str, timeout_seconds: int) -> str | None:
        """Send ``prompt`` and wait for the user to type a reply, else None.

        Returns ``STOP`` when the user types a stop word ("stop", "/cancel" …)
        instead of an answer, so the caller can abort instead of misreading it.
        """
        await self.send(prompt)

        async def predicate(update):
            msg = getattr(update, "message", None)
            if msg is not None and msg.chat_id == self.chat_id and msg.text:
                text = msg.text.strip()
                return STOP if is_stop(text) else text
            return None

        return await self._wait(predicate, timeout_seconds)

    async def ask_button(
        self, prompt: str, buttons: list[tuple[str, str]], timeout_seconds: int
    ) -> str | None:
        """Send ``prompt`` + buttons and wait for a tap, returning its callback data.

        Returns ``STOP`` when the user types a stop word instead of tapping, so a
        "stop" message aborts just like a Skip/Reject tap would.
        """
        await self.send(prompt, buttons=buttons)
        wanted = {data for _, data in buttons}

        async def predicate(update):
            msg = getattr(update, "message", None)
            if msg is not None and msg.chat_id == self.chat_id and msg.text and is_stop(msg.text):
                return STOP
            query = getattr(update, "callback_query", None)
            if (
                query is not None
                and query.message is not None
                and query.message.chat_id == self.chat_id
                and query.data in wanted
            ):
                await self._answer(query)
                return query.data
            return None

        return await self._wait(predicate, timeout_seconds)

    async def wait_for_ticker(self) -> str | None:
        """Block until the user sends a text message, returning its text.

        Used by the persistent ``serve`` loop as its idle wait: it long-polls
        Telegram (30s) until a message arrives, so nothing is consumed on a
        timer. Returns ``None`` only if polling is interrupted.
        """
        from telegram.error import NetworkError

        while True:
            try:
                updates = await self.bot.get_updates(
                    offset=self._offset,
                    timeout=30,
                    allowed_updates=["message"],
                )
            except NetworkError:  # transient: wait a moment and poll again
                await asyncio.sleep(2)
                continue
            for update in updates:
                self._offset = update.update_id + 1
                msg = getattr(update, "message", None)
                if msg is not None and msg.chat_id == self.chat_id and msg.text:
                    return msg.text.strip()

    async def _answer(self, query) -> None:
        """Acknowledge a callback so the button's spinner stops for the user."""
        # The user's answer matters, not the ack; ignore any failure to send it.
        with contextlib.suppress(Exception):
            await self.bot.answer_callback_query(query.id)

    async def watch_for_cancel(self, cancelled: asyncio.Event) -> None:
        """Poll for a "stop" while research runs; set ``cancelled`` when it lands.

        Research runs in a worker thread for a minute or two, and the normal
        prompt loop (``_wait``) isn't active then. This is the loop that notices
        a stop typed during that window, so the bot can ack it immediately and
        skip the order — instead of leaving the cancel unread until the research
        finishes and then dropping it.
        """
        from telegram.error import NetworkError

        while not cancelled.is_set():
            try:
                updates = await self.bot.get_updates(
                    offset=self._offset,
                    timeout=5,
                    allowed_updates=["message"],
                )
            except NetworkError:  # transient: keep watching; the task gets cancelled
                await asyncio.sleep(1)
                continue
            for update in updates:
                self._offset = update.update_id + 1
                msg = getattr(update, "message", None)
                if (
                    msg is not None
                    and msg.chat_id == self.chat_id
                    and msg.text
                    and is_stop(msg.text)
                ):
                    cancelled.set()
                    return

    async def _wait(self, predicate, timeout_seconds: int):
        """Poll for the first update ``predicate`` accepts, until the deadline."""
        from telegram.error import NetworkError

        # Drop stale updates (e.g. a ticker that already started this run) but
        # let a stop through — a cancel typed while research was running must
        # abort the prompt it lands on, not be silently discarded.
        if await self._drain_pending() == STOP:
            return STOP
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                updates = await self.bot.get_updates(
                    offset=self._offset,
                    timeout=1,
                    allowed_updates=["message", "callback_query"],
                )
            except NetworkError:  # transient: keep waiting; the deadline cancels
                await asyncio.sleep(1)
                continue
            for update in updates:
                self._offset = update.update_id + 1
                value = await predicate(update)
                if value is not None:
                    return value

    async def drain(self) -> None:
        """Confirm-and-drop every update currently pending, so we only read new ones.

        Used at startup so a restart never replays a stale ticker that arrived
        before the bot (re)started — without this, an unacknowledged "NVDA" from
        an earlier crash would be re-delivered and traded automatically.
        """
        from telegram.error import NetworkError

        while True:
            try:
                updates = await self.bot.get_updates(
                    offset=self._offset,
                    timeout=0,
                    allowed_updates=["message", "callback_query"],
                )
            except NetworkError:  # transient: can't reach Telegram, nothing to drain
                return
            if not updates:
                break
            self._offset = updates[-1].update_id + 1

    async def _drain_pending(self) -> str | None:
        """Confirm-and-drop pending updates, returning ``STOP`` if one is a stop.

        Like :meth:`drain`, but stop-aware: a "stop" typed while research ran (or
        between prompts) shouldn't be discarded — the next prompt must see it and
        abort. Non-stop messages are still dropped, so a stale ticker can't be
        misread as the answer to "How much?".
        """
        from telegram.error import NetworkError

        saw_stop = False
        while True:
            try:
                updates = await self.bot.get_updates(
                    offset=self._offset,
                    timeout=0,
                    allowed_updates=["message", "callback_query"],
                )
            except NetworkError:  # transient: nothing to drain
                break
            if not updates:
                break
            for update in updates:
                self._offset = update.update_id + 1
                msg = getattr(update, "message", None)
                if (
                    not saw_stop
                    and msg is not None
                    and msg.chat_id == self.chat_id
                    and msg.text
                    and is_stop(msg.text)
                ):
                    saw_stop = True
        return STOP if saw_stop else None
