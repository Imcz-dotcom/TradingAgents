"""Persistent Telegram bot: drive the whole execution flow from chat.

Run ``czagent serve`` and leave the laptop running. From your
phone, send a ticker (``NVDA``, ``0700.HK``) or ``/run NVDA``; the bot runs the
research and then walks you through direction -> amount -> dollars/shares ->
Approve/Reject, exactly like the one-shot ``run`` command, but the ticker and
date come from the chat (the date defaults to today). One trade runs at a time;
after it finishes the bot returns to listening.

Chat controls:
  ``/mode``          — show the current mode (paper or real).
  ``/mode paper``    — switch to paper (simulate).
  ``/mode real``     — switch to real (confirmed first).
  ``stop``           — abort whatever the bot is waiting on.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import date

import typer

from execution_agent import state
from execution_agent.config import ExecutionConfig, mode_label
from execution_agent.decision import run_decision_streaming
from execution_agent.main import approve_and_place
from execution_agent.notify import APPROVE, REJECT, TelegramNotifier, is_stop
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

# A leading slash command; the ticker follows, e.g. "/run NVDA" -> "NVDA".
_COMMAND_RE = re.compile(r"^\s*/(?:run|analyze|start|trade|order)\b\s*", re.IGNORECASE)

# "/mode", "/mode paper", "/mode real" — query or switch the trading environment.
_MODE_RE = re.compile(r"^\s*/mode\b\s*(?P<arg>paper|real)?\s*$", re.IGNORECASE)


def parse_ticker_command(text: str) -> str:
    """Extract a bare ticker from a chat message, e.g. ``/run NVDA`` -> ``NVDA``.

    Returns "" when the message has no ticker to read (an empty message or a
    bare command with nothing after it).
    """
    cleaned = _COMMAND_RE.sub("", text.strip())
    return cleaned.split()[0] if cleaned else ""


def parse_mode_command(text: str) -> tuple[bool, str | None]:
    """Parse a "/mode" command -> ``(is_mode, arg)``.

    ``arg`` is ``"paper"`` / ``"real"`` when given, else ``None`` (a bare
    "/mode" just asks for the current mode).
    """
    m = _MODE_RE.match(text.strip()) if isinstance(text, str) else None
    if m is None:
        return False, None
    arg = m.group("arg")
    return True, (arg.lower() if arg else None)


async def serve_loop(config: ExecutionConfig) -> None:
    """Listen for tickers and run the flow for each, forever."""
    audit = state.audit_path()
    notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
    # Trading environment, switchable from chat with /mode paper|real. Starts
    # from config (paper by default); a restart always returns to paper, so a
    # live session can't silently persist across restarts.
    mode = config.trading_env
    # A transient network blip must not crash the bot on launch — the terminal
    # line still prints, and the bot goes on to listen regardless.
    with contextlib.suppress(Exception):
        await notifier.send(
            f"🤖 Execution agent running ({mode_label(mode)}). "
            "Send a ticker (e.g. `NVDA` or `/run NVDA`)."
        )
    typer.echo("✅ Bot is running — send a ticker from Telegram (e.g. `NVDA`). Ctrl-C to stop.")

    # Live progress: run the research in a worker thread so the event loop is
    # free to send a one-line Telegram update as each phase finishes. Without
    # this a run that takes minutes looks frozen.
    loop = asyncio.get_running_loop()

    async def _send_progress(label: str) -> None:
        # A dropped progress line must not kill the bot.
        with contextlib.suppress(Exception):
            await notifier.send(label)

    def _on_step(label: str) -> None:
        # Called from the research thread; hop back to the loop to send.
        loop.call_soon_threadsafe(asyncio.create_task, _send_progress(label))

    # Drop any messages that arrived before this startup (e.g. a stale "NVDA"
    # from a previous crash). Otherwise a restart replays it and trades on it
    # without the user sending anything now.
    with contextlib.suppress(Exception):
        await notifier.drain()

    while True:
        text = await notifier.wait_for_ticker()
        if text is None:
            continue
        if is_stop(text):
            await notifier.send("🛑 Stopped — nothing was running. Send a ticker like `NVDA` to start.")
            continue
        is_mode, mode_arg = parse_mode_command(text)
        if is_mode:
            if mode_arg is None:
                await notifier.send(f"Current mode: {mode_label(mode)}.")
            elif mode_arg == "paper":
                mode = "SIMULATE"
                await notifier.send("Switched to 📄 PAPER (simulate).")
            else:  # "real" — going live must be an explicit, confirmed act
                confirm = await notifier.ask_button(
                    "⚠️ Switch to 🔴 REAL trading? Orders will use real money.",
                    [("Yes, go live", APPROVE), ("Cancel", REJECT)],
                    config.approval_timeout_seconds,
                )
                if confirm == APPROVE:
                    mode = "REAL"
                    await notifier.send("Switched to 🔴 REAL (live). Be careful.")
                else:
                    await notifier.send(f"Kept {mode_label(mode)}.")
            continue
        ticker = parse_ticker_command(text)
        if not ticker:
            await notifier.send("Send a ticker like `NVDA` or `/run NVDA`.")
            continue

        typer.echo(f"📥 {ticker} — running research…")
        try:
            await notifier.send(f"📥 Got {ticker} — running research, this takes a minute…")
        except Exception as exc:  # noqa: BLE001 — a failed ack must not kill the bot
            typer.echo(f"⚠️ Couldn't reply on Telegram: {exc}", err=True)
        today = date.today().isoformat()
        try:
            graph = TradingAgentsGraph(debug=False, config=DEFAULT_CONFIG.copy())
            # Watch for "stop" while research runs (it can take a minute or two),
            # so a cancel is acked immediately instead of sitting unread until
            # the run finishes and then being dropped.
            cancelled = asyncio.Event()
            watch = asyncio.create_task(notifier.watch_for_cancel(cancelled))
            await asyncio.sleep(0)  # let the watcher start polling before research begins
            try:
                decision = await asyncio.to_thread(
                    run_decision_streaming, graph, ticker, today, config=config, on_step=_on_step
                )
            finally:
                watch.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watch

            if cancelled.is_set():
                typer.echo(f"🛑 {ticker}: stopped during research — no order.")
                await notifier.send("🛑 Stopped — no order placed.")
                state.append_event(audit, {"event": "skip", "reason": "stopped_during_research"})
                continue

            state.append_event(
                audit,
                {
                    "event": "decision",
                    "ticker": ticker,
                    "signal": decision.signal,
                    "side": decision.side,
                },
            )

            if decision.side is None:
                typer.echo(f"↩️ {decision.yahoo}: {decision.signal} — no order.")
                await notifier.send(f"No order for {decision.yahoo}: {decision.signal}")
                state.append_event(audit, {"event": "skip", "reason": decision.signal})
                continue

            summary = await approve_and_place(
                notifier, config, decision, audit, dry_run=False, trading_env=mode
            )
            await notifier.send(summary or "Cancelled — no order placed.")
            typer.echo(f"Done with {ticker}: {summary or 'no order'} — listening again.")
        except ValueError as exc:  # bad ticker (crypto/forex/unknown) — don't die
            typer.echo(f"❌ {exc}", err=True)
            await notifier.send(f"❌ {exc}")
            state.append_event(audit, {"event": "skip", "reason": f"bad_ticker: {exc}"})
        except Exception as exc:  # noqa: BLE001 — log and keep the bot alive
            typer.echo(f"❌ {exc}", err=True)
            state.append_event(audit, {"event": "error", "message": str(exc)})
            await notifier.send(f"❌ {exc}")
