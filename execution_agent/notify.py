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

import contextlib
import time

# Inline-button callback data, so the orchestrator and tests share one spelling.
APPROVE = "approve"
REJECT = "reject"
USD = "usd"
SHARES = "shares"


class TelegramNotifier:
    """A single-chat conversation driver over the Telegram Bot API."""

    def __init__(self, token: str, chat_id: str | int, bot=None):
        self.token = token
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
        """Send a message, optionally with a row of inline buttons."""
        reply_markup = None
        if buttons:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            reply_markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(label, callback_data=data) for label, data in buttons]]
            )
        await self.bot.send_message(self.chat_id, text, reply_markup=reply_markup)

    async def ask_text(self, prompt: str, timeout_seconds: int) -> str | None:
        """Send ``prompt`` and wait for the user to type a reply, else None."""
        await self.send(prompt)

        async def predicate(update):
            msg = getattr(update, "message", None)
            if msg is not None and msg.chat_id == self.chat_id and msg.text:
                return msg.text.strip()
            return None

        return await self._wait(predicate, timeout_seconds)

    async def ask_button(
        self, prompt: str, buttons: list[tuple[str, str]], timeout_seconds: int
    ) -> str | None:
        """Send ``prompt`` + buttons and wait for a tap, returning its callback data."""
        await self.send(prompt, buttons=buttons)
        wanted = {data for _, data in buttons}

        async def predicate(update):
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

    async def _answer(self, query) -> None:
        """Acknowledge a callback so the button's spinner stops for the user."""
        # The user's answer matters, not the ack; ignore any failure to send it.
        with contextlib.suppress(Exception):
            await self.bot.answer_callback_query(query.id)

    async def _wait(self, predicate, timeout_seconds: int):
        """Poll for the first update ``predicate`` accepts, until the deadline."""
        await self._drain()
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            updates = await self.bot.get_updates(
                offset=self._offset,
                timeout=1,
                allowed_updates=["message", "callback_query"],
            )
            for update in updates:
                self._offset = update.update_id + 1
                value = await predicate(update)
                if value is not None:
                    return value

    async def _drain(self) -> None:
        """Drop any updates queued before this conversation, so we only read new ones."""
        while True:
            updates = await self.bot.get_updates(
                offset=self._offset,
                timeout=0,
                allowed_updates=["message", "callback_query"],
            )
            if not updates:
                break
            self._offset = updates[-1].update_id + 1
