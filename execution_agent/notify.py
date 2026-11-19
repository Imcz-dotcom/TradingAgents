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
import html
import re
import threading
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

# Telegram rejects a single message over 4096 characters. ``send`` enforces the
# cap itself, so no caller can crash the bot with a long /status report or
# /auto summary; callers that want to budget the space (header + body + footer)
# still call ``fit_telegram`` with their own limit first.
TELEGRAM_MAX_CHARS = 4096


def fit_telegram(text: str, limit: int = TELEGRAM_MAX_CHARS) -> str:
    """``text`` trimmed to ``limit`` characters, ellipsised when it was longer."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# The bot's messages (and the LLM's summaries) are written in Markdown-ish text:
# **bold** section labels, `code` spans, and ```fenced``` blocks (used for the
# /auto verdict table, which needs real monospace alignment). Telegram renders
# none of these by default, so they used to show as literal punctuation on the
# phone. We convert just those forms to Telegram HTML (its most forgiving parse
# mode) and escape everything else, falling back to plain text if Telegram
# still objects. Fences are converted first so their own backticks don't get
# mis-matched by the single-backtick `code` pattern afterward.
_PRE_RE = re.compile(r"```(.*?)```", re.DOTALL)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_CODE_RE = re.compile(r"`([^`\n]+)`")


def to_telegram_html(text: str) -> str:
    """Escape ``text`` for Telegram HTML, rendering **bold**, `code`, and
    ```fenced``` blocks (the last as a real multi-line monospace ``<pre>``).

    Fenced content is stashed out and restored untouched at the end — a `code`
    span's own backticks appearing inside a fence (unlikely, but possible)
    must not be re-matched by the single-backtick pattern on the second pass.
    """
    escaped = html.escape(str(text), quote=False)
    stashed: list[str] = []

    def _stash(match: re.Match) -> str:
        stashed.append(match.group(1))
        return f"\x00PRE{len(stashed) - 1}\x00"

    escaped = _PRE_RE.sub(_stash, escaped)
    escaped = _BOLD_RE.sub(r"<b>\1</b>", escaped)
    escaped = _CODE_RE.sub(r"<code>\1</code>", escaped)
    for i, block in enumerate(stashed):
        escaped = escaped.replace(f"\x00PRE{i}\x00", f"<pre>{block}</pre>")
    return escaped


def is_stop(text: str) -> bool:
    """Whether a typed message is a stop request (e.g. "stop", "/cancel")."""
    if not isinstance(text, str):
        return False
    word = text.strip().lower()
    if word.startswith("/"):
        word = word[1:].lstrip()
    return word in STOP_WORDS


class PendingApproval:
    """One in-flight Approve/Reject prompt, resolved by whoever is polling.

    Used by :meth:`TelegramNotifier.watch_for_cancel` and
    :meth:`TelegramNotifier.ask_background_button` together so a long-running
    background operation (``/auto`` on REAL) can ask for a tap *while its own
    "stop" watcher is the only thing polling Telegram* — running a second,
    independent poll concurrently would race on ``self._offset`` and could
    silently drop updates (including the approval tap itself).
    """

    def __init__(self, buttons: list[tuple[str, str]], result: asyncio.Future):
        self.buttons = buttons
        self.result = result


class ApprovalBox:
    """Holds the currently pending :class:`PendingApproval`, or ``None``.

    A plain mutable box (not a queue) because only one approval is ever asked
    at a time — ``run_auto`` processes tickers one at a time, waiting for each
    answer before moving on.
    """

    def __init__(self):
        self.current: PendingApproval | None = None


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

    async def send(self, text: str, buttons=None) -> None:
        """Send a message, optionally with inline buttons.

        ``buttons`` is a list of ``(label, callback_data)`` pairs (one row), or
        a list of such lists (one row each). The text is always fitted to
        Telegram's 4096-character cap here, so a long report can never raise
        out of a handler. The HTML-formatted variant is used when it still fits
        the cap (escaping can grow the text); otherwise — or when Telegram
        rejects the markup — the plain text is sent instead.

        Retries only on a *connection* failure, where nothing was sent. A read
        timeout means Telegram may already have the message (only the ack was
        slow), so retrying would post the same message two or three times.
        """
        reply_markup = None
        if buttons:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            rows = buttons if isinstance(buttons[0], list) else [buttons]
            reply_markup = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(label, callback_data=data) for label, data in row]
                    for row in rows
                ]
            )
        from telegram.error import BadRequest, NetworkError, TimedOut

        text = fit_telegram(text)
        html_text = to_telegram_html(text)
        use_html = len(html_text) <= TELEGRAM_MAX_CHARS

        last_exc = None
        for attempt in range(3):
            try:
                # A slightly longer read timeout helps a slow/high-latency link
                # (e.g. Telegram over a flaky connection) without stalling.
                try:
                    if use_html:
                        await self.bot.send_message(
                            self.chat_id,
                            html_text,
                            reply_markup=reply_markup,
                            read_timeout=15,
                            parse_mode="HTML",
                        )
                    else:
                        await self.bot.send_message(
                            self.chat_id, text, reply_markup=reply_markup, read_timeout=15
                        )
                except BadRequest:
                    if not use_html:
                        raise  # plain text refused — nothing simpler to fall back to
                    # Telegram rejected the HTML (odd markup in an LLM summary):
                    # the message matters more than the formatting — send plain.
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

        A tapped inline button whose callback data looks like a command
        (``/execute NVDA``) is returned exactly as if the user had typed it, so
        the serve loop's buttons dispatch through the same parser as text.
        """
        from telegram.error import NetworkError

        while True:
            try:
                updates = await self.bot.get_updates(
                    offset=self._offset,
                    timeout=30,
                    allowed_updates=["message", "callback_query"],
                )
            except NetworkError:  # transient: wait a moment and poll again
                await asyncio.sleep(2)
                continue
            for update in updates:
                self._offset = update.update_id + 1
                msg = getattr(update, "message", None)
                if msg is not None and msg.chat_id == self.chat_id and msg.text:
                    return msg.text.strip()
                query = getattr(update, "callback_query", None)
                if (
                    query is not None
                    and query.message is not None
                    and query.message.chat_id == self.chat_id
                    and isinstance(query.data, str)
                    and query.data.startswith("/")
                ):
                    await self._answer(query)
                    return query.data

    async def _answer(self, query) -> None:
        """Acknowledge a callback so the button's spinner stops for the user."""
        # The user's answer matters, not the ack; ignore any failure to send it.
        with contextlib.suppress(Exception):
            await self.bot.answer_callback_query(query.id)

    async def watch_for_cancel(
        self, cancelled: threading.Event, approvals: ApprovalBox | None = None
    ) -> None:
        """Poll for a "stop" while research (or ``/auto``) runs; set ``cancelled``.

        ``cancelled`` is a ``threading.Event`` so the worker thread can also
        read it (via ``is_set``) and abort at its next boundary. This is the
        loop that notices a stop typed mid-run, so the bot can ack it
        immediately and abandon the run — instead of leaving the cancel unread
        until the run finishes and then dropping it.

        When ``approvals`` is given, this same loop also resolves whichever
        :class:`PendingApproval` is currently in it — see
        :meth:`ask_background_button`. A background operation needs exactly one
        update poller; running ``ask_button``'s own poll concurrently with this
        one would race on ``self._offset``.
        """
        from telegram.error import NetworkError

        while not cancelled.is_set():
            try:
                updates = await self.bot.get_updates(
                    offset=self._offset,
                    timeout=5,
                    allowed_updates=["message", "callback_query"],
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
                    pending = approvals.current if approvals else None
                    if pending is not None and not pending.result.done():
                        pending.result.set_result(STOP)
                    return
                pending = approvals.current if approvals else None
                if pending is None:
                    continue
                query = getattr(update, "callback_query", None)
                if (
                    query is not None
                    and query.message is not None
                    and query.message.chat_id == self.chat_id
                    and query.data in {data for _, data in pending.buttons}
                    and not pending.result.done()
                ):
                    await self._answer(query)
                    pending.result.set_result(query.data)

    async def ask_background_button(
        self,
        approvals: ApprovalBox,
        prompt: str,
        buttons: list[tuple[str, str]],
        timeout_seconds: int,
    ) -> str | None:
        """Ask for a tap while a separate ``watch_for_cancel(_, approvals)`` task
        is the one actually polling Telegram, returning the matching button's
        callback data, ``STOP``, or ``None`` on timeout.

        This does **not** poll itself (unlike :meth:`ask_button`) — it registers
        a :class:`PendingApproval` in ``approvals`` and waits on its future,
        which the watcher task resolves when a matching tap or a stop word
        arrives. Used by ``/auto`` on REAL, where the watcher is already
        running for the whole scan and a second independent poll would race it.
        """
        pending = PendingApproval(buttons=buttons, result=asyncio.get_event_loop().create_future())
        approvals.current = pending
        try:
            await self.send(prompt, buttons=buttons)
            try:
                return await asyncio.wait_for(pending.result, timeout_seconds)
            except asyncio.TimeoutError:
                return None
        finally:
            if approvals.current is pending:
                approvals.current = None

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
