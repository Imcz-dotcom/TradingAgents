"""Persistent Telegram bot: research, then execute on your own schedule.

Run ``czagent serve`` and leave the laptop running. From your phone you can:

  ``NVDA`` / ``/research NVDA`` — run the research, store the full result, and
                                  send the summary + AI's Buy/Sell. No order.
  ``/execute``               — run the approval flow on the latest research of
                                  the day (direction -> amount -> dollars/shares
                                  -> Approve/Reject), exactly like ``run``.
  ``/chat <question>``       — ask a question about today's research (the bot
                                  answers from the full reports/debates, not the
                                  one-line summary). Multi-turn, remembers the
                                  whole day's conversation, and can target any
                                  ticker researched today: ``/chat NVDA ...``.
  ``/mode`` / ``/mode paper`` / ``/mode real`` — show or switch paper/live.
  ``/auto``                  — fully-automatic paper scan: read the account,
                               research the watchlist + holdings, and manage them
                               itself (paper only, no approval).
  ``/auto <amount>``         — same, with a dollar budget to deploy: split across
                               the buy ideas by the research's conviction.
  ``stop``                   — abort whatever the bot is waiting on.

Research and execution are deliberately separate: research takes minutes, and
you may not want to answer "how much?" the instant it finishes. Research is
fire-and-forget; you execute (or ask about it) whenever you're ready.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import threading
from dataclasses import dataclass, replace
from datetime import date

import typer

from execution_agent import chat, cleanup, persist, state
from execution_agent.auto import run_auto
from execution_agent.config import ExecutionConfig, mode_label
from execution_agent.decision import DecisionResult, parse_amount, run_decision_streaming
from execution_agent.main import TELEGRAM_MAX_CHARS, _fit_telegram, approve_and_place
from execution_agent.notify import APPROVE, REJECT, SHARES, TelegramNotifier, is_stop
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

# A leading slash command naming a ticker to research; the ticker follows, e.g.
# "/research NVDA" -> "NVDA". "/run" and friends are kept as aliases.
_COMMAND_RE = re.compile(
    r"^\s*/(?:research|run|analyze|start|trade|order)\b\s*", re.IGNORECASE
)

# "/mode", "/mode paper", "/mode real" — query or switch the trading environment.
_MODE_RE = re.compile(r"^\s*/mode\b\s*(?P<arg>paper|real)?\s*$", re.IGNORECASE)

# "/execute" — run the approval flow on the latest stored research.
_EXECUTE_RE = re.compile(r"^\s*/execute\b", re.IGNORECASE)

# "/auto" — run the fully-automatic paper-trading loop (no human approval).
_AUTO_RE = re.compile(r"^\s*/auto\b", re.IGNORECASE)

# "/chat <question>" — ask about today's research; an optional leading ticker
# ("/chat NVDA ...") targets that run instead of the latest one.
_CHAT_RE = re.compile(r"^\s*/chat\b\s*(?P<question>.*)$", re.IGNORECASE)

# A "fresh"/"force" hint after a ticker forces a re-run even though the same
# ticker was already researched today (the default is to reuse the stored run).
_FRESH_RE = re.compile(r"\b(?:fresh|force|refresh|re-?run)\b", re.IGNORECASE)

# A leading ticker token in a /chat message, e.g. "/chat NVDA what's the risk?"
_TICKER_LEAD = re.compile(r"^\s*([A-Za-z0-9.]+)\s*(.*)$", re.DOTALL)


@dataclass
class _Research:
    """One completed research run on a ticker, held for /execute and /chat."""

    ticker: str
    decision: DecisionResult  # carries .research = the full final state
    executed: bool = False


def parse_ticker_command(text: str) -> str:
    """Extract a bare ticker to research, e.g. ``/research NVDA`` -> ``NVDA``.

    Returns "" when the message has no ticker to read (an empty message or a
    bare command with nothing after it).
    """
    if not isinstance(text, str):
        return ""
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


def is_execute_command(text: str) -> bool:
    """Whether ``text`` is an "/execute" request (run the approval flow)."""
    return isinstance(text, str) and bool(_EXECUTE_RE.match(text.strip()))


def is_auto_command(text: str) -> bool:
    """Whether ``text`` is an "/auto" request (autonomous paper scan)."""
    return isinstance(text, str) and bool(_AUTO_RE.match(text.strip()))


def parse_auto_command(text: str) -> tuple[bool, float | None, str | None]:
    """Parse an "/auto" message into ``(is_auto, budget, error)``.

    ``budget`` is the dollars to deploy (``None`` = manage-only), and ``error``
    is a human message when the trailing amount is unreadable or not dollars
    (``None`` otherwise). Not an /auto command → ``(False, None, None)``.
    """
    if not isinstance(text, str):
        return False, None, None
    m = _AUTO_RE.match(text.strip())
    if m is None:
        return False, None, None
    rest = text.strip()[m.end():].strip()
    if not rest:
        return True, None, None  # manage-only
    try:
        number, unit = parse_amount(rest)
    except ValueError:
        return True, None, "Couldn't read that budget — use a dollar amount like `/auto 500`."
    if unit == SHARES:
        return True, None, "The /auto budget is in dollars, not shares — try `/auto 500`."
    return True, number, None


def parse_chat_command(text: str) -> tuple[bool, str]:
    """Parse a "/chat <question>" message -> ``(is_chat, question)``.

    ``question`` is the text after "/chat" ("" when there is none), so a bare
    "/chat" is a request for the current research with nothing to answer yet.
    """
    if not isinstance(text, str):
        return False, ""
    m = _CHAT_RE.match(text.strip())
    if m is None:
        return False, ""
    return True, m.group("question").strip()


def _research_summary(decision: DecisionResult, trading_env: str) -> str:
    """The message sent when research finishes: mode, call, and the full summary."""
    header = (
        f"{mode_label(trading_env)}\n"
        f"TradingAgents says {decision.side} {decision.yahoo} (rating: {decision.signal}).\n\n"
    )
    footer = "\n\n/execute to trade · /chat to ask a question."
    summary = _fit_telegram(decision.summary, TELEGRAM_MAX_CHARS - len(header) - len(footer))
    return header + summary + footer


def resolve_chat_target(question: str, researches: dict) -> tuple[str | None, str]:
    """Pick which research a ``/chat`` question is about.

    - A leading ticker token (``/chat NVDA ...``) targets that ticker.
    - Otherwise, any researched ticker named anywhere in the question wins.
    - Otherwise, the latest research (last inserted) is assumed.

    Returns ``(ticker, remaining_question)``; ``ticker`` is None when there is
    no research yet.
    """
    if not researches:
        return None, question
    q = (question or "").strip()
    m = _TICKER_LEAD.match(q)
    if m:
        lead = m.group(1)
        for t in researches:
            if t.upper() == lead.upper():
                return t, m.group(2).strip()
    for t in researches:
        if re.search(rf"\b{re.escape(t)}\b", q, re.IGNORECASE):
            return t, q
    return next(reversed(researches)), q


def _research_to_entry(res: _Research) -> dict:
    """Serialize one research run to a plain JSON-safe dict."""
    d = res.decision
    return {
        "executed": res.executed,
        "decision": {
            "yahoo": d.yahoo,
            "moomoo": d.moomoo,
            "signal": d.signal,
            "side": d.side,
            "summary": d.summary,
            "cash": d.cash,
            "position_quantity": d.position_quantity,
            "research": persist.research_subset(d.research),
        },
    }


def _entry_to_research(ticker: str, entry: dict) -> _Research | None:
    """Rebuild a ``_Research`` from a persisted entry, or None if malformed."""
    decision = persist.decision_from_entry(entry.get("decision"))
    if decision is None:
        return None
    return _Research(ticker=ticker, decision=decision, executed=entry.get("executed", False))


def _day_to_history(researches: dict, chat_history: list) -> dict:
    """Serialize the whole day — every research run and chat turn — to a dict."""
    return {
        "date": date.today().isoformat(),
        "researches": {t: _research_to_entry(r) for t, r in researches.items()},
        "chat_history": chat_history,
    }


def _history_to_day(day: dict) -> tuple[dict, list]:
    """Rebuild ``(researches, chat_history)`` from a persisted day; best-effort.

    Malformed entries are dropped rather than failing the whole restore, so a
    single bad record never loses the rest of the day.
    """
    if not isinstance(day, dict):
        return {}, []
    raw = day.get("researches") or {}
    researches: dict = {}
    if isinstance(raw, dict):
        for ticker, entry in raw.items():
            if not isinstance(ticker, str) or not isinstance(entry, dict):
                continue
            res = _entry_to_research(ticker, entry)
            if res is not None:
                researches[ticker] = res
    chat_history: list = []
    for entry in day.get("chat_history") or []:
        if isinstance(entry, dict) and entry.get("q"):
            chat_history.append(
                {"ticker": entry.get("ticker"), "q": entry["q"], "a": entry.get("a", "")}
            )
    return researches, chat_history


async def serve_loop(config: ExecutionConfig) -> None:
    """Listen for commands and drive research / execute / chat, forever."""
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
            "Send a ticker (e.g. `NVDA`) to research, then `/execute` to trade or `/chat` to ask."
        )
    typer.echo("✅ Bot is running — send a ticker to research, `/execute` to trade, `/chat` to ask. Ctrl-C to stop.")

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
    # from a previous crash). Otherwise a restart replays it and researches it
    # without the user sending anything now.
    with contextlib.suppress(Exception):
        await notifier.drain()

    # Housekeeping: drop research logs older than a week so the results dir
    # doesn't grow without bound. Best-effort — a failed cleanup must not stop
    # the bot.
    with contextlib.suppress(Exception):
        removed = cleanup.purge_old_logs(
            DEFAULT_CONFIG["results_dir"], max_age_days=config.log_retention_days
        )
        if removed:
            typer.echo(f"🧹 Removed {removed} stale log file(s) older than a week.")

    # The day's state: every ticker researched today (insertion-ordered, so the
    # last one is "latest"), plus the whole day's /chat conversation.
    researches: dict[str, _Research] = {}
    chat_history: list[dict] = []

    def save_day() -> None:
        """Persist the day to disk for same-day restarts."""
        with contextlib.suppress(Exception):
            persist.save_history(_day_to_history(researches, chat_history))

    # Restore today's history from an earlier session, so /chat and /execute
    # survive a restart within the same day. A new day starts fresh (the stored
    # date no longer matches today) — that is the "per-day" memory.
    loaded = persist.load_history()
    if isinstance(loaded, dict) and loaded.get("date") == date.today().isoformat():
        researches, chat_history = _history_to_day(loaded)
        if researches:
            typer.echo(f"📚 Restored today's research on {', '.join(researches)}.")

    async def handle_research(ticker: str, fresh: bool = False) -> None:
        today = date.today().isoformat()
        # Same ticker today: reuse the research already held instead of paying for
        # another full pipeline run. The data is keyed by date, so a re-run later
        # the same day would read the same inputs; the only way it changes is a
        # fresh run (forced with "/research NVDA fresh").
        if not fresh and ticker in researches:
            d = researches[ticker].decision
            call = f"{d.side} {d.yahoo}" if d.side else f"no order on {d.yahoo}"
            typer.echo(f"📚 {ticker}: reuse today's research — not re-running.")
            await notifier.send(
                f"📚 Already researched {ticker} today — {call} (rating {d.signal}). "
                "Not re-running. `/execute` to trade or `/chat` to ask. "
                f"Send `/research {ticker} fresh` to force a new run."
            )
            return
        typer.echo(f"📥 {ticker} — running research…")
        try:
            await notifier.send(f"📥 Got {ticker} — running research, this takes a minute…")
        except Exception as exc:  # noqa: BLE001 — a failed ack must not kill the bot
            typer.echo(f"⚠️ Couldn't reply on Telegram: {exc}", err=True)
        try:
            graph = TradingAgentsGraph(debug=False, config=DEFAULT_CONFIG.copy())
            # A "stop" during research is caught on two fronts: the watcher polls
            # Telegram and sets ``stopped`` the moment a stop lands, and the
            # research itself checks ``stopped`` between phases so it aborts at
            # the next phase boundary instead of running the full pipeline.
            stopped = threading.Event()
            watch = asyncio.create_task(notifier.watch_for_cancel(stopped))
            await asyncio.sleep(0)  # let the watcher start polling before research begins
            runner = asyncio.create_task(
                asyncio.to_thread(
                    run_decision_streaming,
                    graph,
                    ticker,
                    today,
                    config=config,
                    on_step=_on_step,
                    stop_check=stopped.is_set,
                )
            )
            try:
                await asyncio.wait({runner, watch}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                watch.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watch

            if stopped.is_set():
                # Stop arrived (or the run is still mid-phase): ack now and
                # abandon the run instead of waiting for it to finish.
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runner
                typer.echo(f"🛑 {ticker}: stopped during research — no order.")
                await notifier.send("🛑 Stopped — no order placed.")
                state.append_event(audit, {"event": "skip", "reason": "stopped_during_research"})
                return

            decision = runner.result()
            state.append_event(
                audit,
                {"event": "decision", "ticker": ticker, "signal": decision.signal, "side": decision.side},
            )
            # Re-researching a ticker moves it to the end, so "latest" always
            # means the run you just did.
            if ticker in researches:
                del researches[ticker]
            # Keep only the JSON-safe subset in RAM. The full final state (with
            # the raw agent ``messages``) is already archived to disk by
            # ``record_decision`` inside run_decision_streaming; /chat and
            # /execute only ever read the subset, so the messages are dead weight.
            researches[ticker] = _Research(
                ticker=ticker,
                decision=replace(decision, research=persist.research_subset(decision.research)),
            )
            save_day()

            if decision.side is None:
                await notifier.send(
                    f"No order for {decision.yahoo}: {decision.signal}. "
                    "Still saved — `/chat` to ask about it."
                )
                return
            await notifier.send(_research_summary(decision, mode))
            typer.echo(f"📋 {ticker}: researched ({decision.signal}) — `/execute` to trade or `/chat` to ask.")
        except ValueError as exc:  # bad ticker (crypto/forex/unknown) — don't die
            typer.echo(f"❌ {exc}", err=True)
            await notifier.send(f"❌ {exc}")
            state.append_event(audit, {"event": "skip", "reason": f"bad_ticker: {exc}"})
        except Exception as exc:  # noqa: BLE001 — log and keep the bot alive
            typer.echo(f"❌ {exc}", err=True)
            state.append_event(audit, {"event": "error", "message": str(exc)})
            await notifier.send(f"❌ {exc}")

    async def handle_execute() -> None:
        if not researches:
            await notifier.send("No research yet — send a ticker or `/research NVDA` first.")
            return
        ticker = next(reversed(researches))  # the latest research of the day
        res = researches[ticker]
        if res.decision.side is None:
            await notifier.send(
                f"No trade for {res.decision.yahoo}: {res.decision.signal}. `/chat` to ask about it."
            )
            return
        if res.executed:
            await notifier.send(
                f"{ticker} was already executed — send `/research {ticker}` for a fresh decision."
            )
            return
        summary = await approve_and_place(
            notifier, config, res.decision, audit, dry_run=False, trading_env=mode
        )
        if summary.startswith("✅ Order placed"):
            res.executed = True
            save_day()
        await notifier.send(summary or "Cancelled — no order placed.")
        typer.echo(f"Done with {ticker}: {summary or 'no order'} — listening again.")

    async def handle_chat(question: str) -> None:
        if not researches:
            await notifier.send("No research yet — send a ticker or `/research NVDA` first.")
            return
        ticker, question = resolve_chat_target(question, researches)
        if not question:
            await notifier.send(
                "Ask a question about the research, e.g. `/chat what's the biggest risk?` "
                "— or name a ticker: `/chat NVDA what's the risk?`"
            )
            return
        res = researches[ticker]
        typer.echo(f"💬 {ticker}: answering — {question}")
        try:
            answer = await asyncio.to_thread(
                chat.answer, res.decision.research, ticker, chat_history, question
            )
        except Exception as exc:  # noqa: BLE001 — a chat failure must not kill the bot
            await notifier.send(f"❌ Chat failed: {exc}")
            return
        chat_history.append({"ticker": ticker, "q": question, "a": answer})
        save_day()
        await notifier.send(_fit_telegram(answer))

    async def handle_auto(budget: float | None) -> None:
        typer.echo("🤖 /auto — running the autonomous paper scan…")
        try:
            if budget:
                await notifier.send(
                    f"🤖 /auto — deploying ${budget:g} today: I'll research the "
                    "watchlist + holdings and split it across the buys by conviction."
                )
            else:
                await notifier.send(
                    "🤖 /auto — managing the account: I'll research the watchlist + "
                    "holdings and sell/trim/hold per the analysis (no new buys "
                    "without a budget)."
                )
        except Exception as exc:  # noqa: BLE001 — a failed ack must not kill the bot
            typer.echo(f"⚠️ Couldn't reply on Telegram: {exc}", err=True)

        # Same stop/race pattern as research: run the loop in a worker thread so
        # the event loop stays free, and a "stop" aborts it at the next boundary.
        stopped = threading.Event()
        watch = asyncio.create_task(notifier.watch_for_cancel(stopped))
        await asyncio.sleep(0)  # let the watcher start polling before /auto begins
        runner = asyncio.create_task(
            asyncio.to_thread(
                run_auto,
                config,
                budget=budget,
                trading_env=mode,
                audit=audit,
                on_step=_on_step,
                stop_check=stopped.is_set,
            )
        )
        try:
            await asyncio.wait({runner, watch}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch

        if runner.done():
            summary = runner.result()  # may itself be a "stopped" summary
        else:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner
            summary = "🛑 Stopped — /auto aborted."
        await notifier.send(_fit_telegram(summary))
        typer.echo("✅ /auto complete — listening again.")

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
        if is_execute_command(text):
            await handle_execute()
            continue
        if is_auto_command(text):
            _, budget, err = parse_auto_command(text)
            if err:
                await notifier.send(err)
            else:
                await handle_auto(budget)
            continue
        is_chat, question = parse_chat_command(text)
        if is_chat:
            await handle_chat(question)
            continue
        ticker = parse_ticker_command(text)
        if not ticker:
            await notifier.send("Send a ticker like `NVDA`, or use `/execute` or `/chat <question>`.")
            continue
        await handle_research(ticker, fresh=bool(_FRESH_RE.search(text)))
