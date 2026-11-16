"""Persistent Telegram bot: research, then execute on your own schedule.

Run ``czagent serve`` and leave the laptop running, then drive everything from
the phone. The command set is documented once, in :data:`HELP_TEXT` below (the
``/help`` reply) — research a ticker, ``/execute`` it, ``/chat`` about it,
``/auto`` the whole account, plus ``/status``, ``/orders``, ``/cancel``,
``/watchlist`` and ``/mode``.

Research and execution are deliberately separate: research takes minutes, and
you may not want to answer "how much?" the instant it finishes. Research is
fire-and-forget; you execute (or ask about it) whenever you're ready.

Two background behaviours run off the idle loop (checked between messages):
a daily scheduled ``/auto`` (``EXECUTION_AGENT_AUTO_SCHEDULE``) that reports
quietly — only orders, errors and the final plan — and a fill watcher that
messages when a resting order fills.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime

import typer

from execution_agent import auto_plan, chat, cleanup, persist, state, watchlist
from execution_agent.auto import run_auto
from execution_agent.broker import OPEN_STATUSES, MoomooBroker
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

# "/execute [ticker]" — run the approval flow on a ticker's stored research.
# No ticker means the latest research of the day.
_EXECUTE_RE = re.compile(r"^\s*/execute\b\s*(?P<ticker>[A-Za-z0-9.]+)?", re.IGNORECASE)

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

# "/help" (or "/commands") — list what the bot can do.
_HELP_RE = re.compile(r"^\s*/(?:help|commands)\b", re.IGNORECASE)

# "/status" (or "/portfolio", "/report") — the account report.
_STATUS_RE = re.compile(r"^\s*/(?:status|portfolio|report)\b", re.IGNORECASE)

# "/orders" — list open (resting) orders.
_ORDERS_RE = re.compile(r"^\s*/orders\b", re.IGNORECASE)

# "/cancel 123" / "/cancelorder 123" — cancel one order by id. A *bare*
# "/cancel" never reaches this: it is a stop word (is_stop) handled first.
_CANCEL_ORDER_RE = re.compile(r"^\s*/cancel(?:order)?\b\s+(?P<oid>\d+)\s*$", re.IGNORECASE)

# "/watchlist", "/watchlist add AMD", "/watchlist remove AMD".
_WATCHLIST_RE = re.compile(
    r"^\s*/watchlist\b\s*(?:(?P<action>add|remove|rm|del|delete)\b\s*)?(?P<ticker>[A-Za-z0-9.\-]+)?\s*$",
    re.IGNORECASE,
)

# How often the idle wait wakes to run the background checks, how often the
# fill watcher polls the broker, and the floor on how many chat turns the
# per-day history file keeps (the prompt replays config.chat_history_turns).
_IDLE_WAKE_SECONDS = 60
_FILL_POLL_SECONDS = 120
_PERSISTED_CHAT_TURNS = 50

HELP_TEXT = (
    "🤖 **What I can do**\n\n"
    "`NVDA` or `/research NVDA` — research a ticker (add `fresh` to force a re-run)\n"
    "`/execute [ticker]` — trade a researched ticker (I'll ask amount + approval)\n"
    "`/chat [ticker] <question>` — ask me about today's research\n"
    "`/auto` — manage the account: sell/trim/hold per the analysis (paper only)\n"
    "`/auto 500` — same, plus deploy $500 across the buy ideas by conviction\n"
    "`/status` — cash, positions with P/L, open orders, today's research\n"
    "`/orders` — open orders, with tap-to-cancel buttons\n"
    "`/cancel <id>` — cancel one resting order\n"
    "`/watchlist [add|remove TICKER]` — view or edit the /auto watchlist\n"
    "`/mode [paper|real]` — show or switch trading mode\n"
    "`stop` — abort whatever I'm doing"
)


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


def parse_execute_command(text: str) -> tuple[bool, str | None]:
    """Parse an "/execute [ticker]" message -> ``(is_execute, ticker)``.

    ``ticker`` is the ticker to execute (``None`` = the latest research of the
    day), or ``None`` when ``text`` isn't an /execute command.
    """
    if not isinstance(text, str):
        return False, None
    m = _EXECUTE_RE.match(text.strip())
    if m is None:
        return False, None
    return True, m.group("ticker") or None


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


def is_help_command(text: str) -> bool:
    """Whether ``text`` asks for the command list."""
    return isinstance(text, str) and bool(_HELP_RE.match(text.strip()))


def is_status_command(text: str) -> bool:
    """Whether ``text`` asks for the account report."""
    return isinstance(text, str) and bool(_STATUS_RE.match(text.strip()))


def is_orders_command(text: str) -> bool:
    """Whether ``text`` asks for the open-orders list."""
    return isinstance(text, str) and bool(_ORDERS_RE.match(text.strip()))


def parse_cancel_order(text: str) -> int | None:
    """The order id of a "/cancel <id>" request, or None when it isn't one."""
    m = _CANCEL_ORDER_RE.match(text.strip()) if isinstance(text, str) else None
    return int(m.group("oid")) if m else None


def parse_watchlist_command(text: str) -> tuple[bool, str | None, str | None]:
    """Parse a "/watchlist [add|remove TICKER]" message.

    Returns ``(is_watchlist, action, ticker)`` where ``action`` is ``"add"`` /
    ``"remove"`` / ``None`` (just show). ``rm``/``del``/``delete`` are remove
    aliases. A ticker with no action defaults to "add" (people type
    ``/watchlist AMD`` meaning *add AMD*).
    """
    m = _WATCHLIST_RE.match(text.strip()) if isinstance(text, str) else None
    if m is None:
        return False, None, None
    action, ticker = m.group("action"), m.group("ticker")
    if action:
        action = "remove" if action.lower() in ("remove", "rm", "del", "delete") else "add"
    elif ticker:
        action = "add"
    return True, action, (ticker.upper() if ticker else None)


def _fill_messages(prev: dict[int, str], orders: list[dict]) -> tuple[list[str], dict[int, str]]:
    """Diff an order snapshot against the previous one, yielding fill messages.

    ``prev`` maps order_id -> last seen status; ``orders`` is today's full list
    from the broker. An order previously seen resting that is now FILLED_ALL
    earns a "🎉 Filled" message. Returns ``(messages, new_status_map)``. Pure,
    so the serve loop's fill watcher unit-tests without a broker.
    """
    messages: list[str] = []
    current: dict[int, str] = {}
    for order in orders:
        oid, status = order["order_id"], order["order_status"]
        current[oid] = status
        if status == "FILLED_ALL" and prev.get(oid) in OPEN_STATUSES:
            price = f" @ {order['price']:g}" if order.get("price") else ""
            messages.append(
                f"🎉 Order filled: {order['side']} {order['quantity']:g} {order['symbol']}{price}"
            )
    return messages, current


def _schedule_due(
    schedule: tuple[int, int] | None,
    now: datetime,
    last_run_date: str | None,
    plan_date: str | None,
) -> bool:
    """Whether the daily scheduled /auto should fire now.

    Fires at most once per day, only at/after the scheduled time, and never
    when /auto already ran today (``plan_date`` is today's stored plan date) —
    a manual morning run satisfies the schedule, saving a redundant scan.
    """
    if schedule is None:
        return False
    today = now.date().isoformat()
    if last_run_date == today or plan_date == today:
        return False
    return (now.hour, now.minute) >= schedule


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


def _money(value, currency: str | None) -> str:
    """A number as money text, e.g. ``12,345.67 USD``."""
    suffix = f" {currency}" if currency else ""
    return f"{value:,.2f}{suffix}"


def format_status(
    mode: str,
    account: dict,
    positions: list[dict],
    open_orders: list[dict],
    researched: list[str],
    watch: list[str],
) -> str:
    """The /status report: account worth, positions with P/L, orders, research."""
    currency = account.get("currency")
    lines = [f"📊 **Account report** — {mode_label(mode)}", ""]
    lines.append(f"💵 Cash: {_money(account.get('cash') or 0.0, currency)}")
    if account.get("total_assets") is not None:
        lines.append(f"💼 Total assets: {_money(account['total_assets'], currency)}")
    if account.get("market_value") is not None:
        lines.append(f"📦 Invested: {_money(account['market_value'], currency)}")

    lines.append("")
    if positions:
        lines.append("📈 **Positions**")
        for p in positions:
            line = f"• {p['symbol']} ×{p['quantity']:g} @ {p['average_price']:g}"
            if p.get("current_price") is not None:
                line += f" → {p['current_price']:g}"
            if p.get("pl_value") is not None:
                arrow = "🟢" if p["pl_value"] >= 0 else "🔴"
                ratio = f" ({p['pl_ratio']:+.1f}%)" if p.get("pl_ratio") is not None else ""
                line += f" {arrow} {p['pl_value']:+,.2f}{ratio}"
            lines.append(line)
    else:
        lines.append("📈 Positions: none")

    if open_orders:
        lines.append("")
        lines.append(f"⏳ Open orders: {len(open_orders)} — `/orders` to see or cancel them")
    if researched:
        lines.append("")
        lines.append(
            f"🔎 Researched today: {', '.join(researched)} — `/chat` to ask about them"
        )
    if watch:
        lines.append(f"👀 Watchlist: {', '.join(watch)}")
    return "\n".join(lines)


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
        """Persist the day to disk for same-day restarts.

        The stored chat history is capped: /chat only replays the last few turns
        anyway (``chat_history_turns``), so an unbounded day of Q&A would grow
        the file — and every save rewrites it whole — for no benefit. The cap
        never drops below what the prompt replays, so a raised
        ``chat_history_turns`` is honored across restarts too.
        """
        keep = max(_PERSISTED_CHAT_TURNS, config.chat_history_turns)
        with contextlib.suppress(Exception):
            persist.save_history(_day_to_history(researches, chat_history[-keep:]))

    def _broker() -> MoomooBroker:
        """A broker for the *current* mode (the user can switch at runtime)."""
        return MoomooBroker(
            host=config.moomoo_host, port=config.moomoo_port, trading_env=mode
        )

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
                    "Still saved — `/chat` to ask about it.",
                    buttons=[("💬 Ask about it", f"/chat {ticker}")],
                )
                return
            # Tap-able next steps: the button data is a command, which the idle
            # wait returns exactly as if it had been typed.
            await notifier.send(
                _research_summary(decision, mode),
                buttons=[
                    (f"▶️ Execute {ticker}", f"/execute {ticker}"),
                    ("💬 Ask about it", f"/chat {ticker}"),
                ],
            )
            typer.echo(f"📋 {ticker}: researched ({decision.signal}) — `/execute` to trade or `/chat` to ask.")
        except ValueError as exc:  # bad ticker (crypto/forex/unknown) — don't die
            typer.echo(f"❌ {exc}", err=True)
            await notifier.send(f"❌ {exc}")
            state.append_event(audit, {"event": "skip", "reason": f"bad_ticker: {exc}"})
        except Exception as exc:  # noqa: BLE001 — log and keep the bot alive
            typer.echo(f"❌ {exc}", err=True)
            state.append_event(audit, {"event": "error", "message": str(exc)})
            await notifier.send(f"❌ {exc}")

    async def handle_execute(ticker: str | None = None) -> None:
        if not researches:
            await notifier.send("No research yet — send a ticker or `/research NVDA` first.")
            return
        if ticker is None:
            ticker = next(reversed(researches))  # the latest research of the day
        else:
            # `/execute WDC` targets a specific run; it must exist first.
            match = next((t for t in researches if t.upper() == ticker.upper()), None)
            if match is None:
                await notifier.send(
                    f"No research on {ticker.upper()} yet — research it first "
                    f"(send `{ticker}` or `/research {ticker}`), then `/execute {ticker}`."
                )
                return
            ticker = match
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
        typer.echo(f"💬 {ticker}: answering — {question}")
        try:
            all_research = {t: r.decision.research for t, r in researches.items()}
            answer = await asyncio.to_thread(
                chat.answer,
                all_research,
                ticker,
                chat_history,
                question,
                max_turns=config.chat_history_turns,
            )
        except Exception as exc:  # noqa: BLE001 — a chat failure must not kill the bot
            await notifier.send(f"❌ Chat failed: {exc}")
            return
        chat_history.append({"ticker": ticker, "q": question, "a": answer})
        save_day()
        await notifier.send(_fit_telegram(answer))

    async def handle_auto(budget: float | None, quiet: bool = False) -> None:
        typer.echo("🤖 /auto — running the autonomous paper scan…")
        # The scheduled (quiet) run announces nothing — the summary is enough.
        if not quiet:
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
                quiet=quiet,
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
            # /auto saved its research to the shared history; pull it into live
            # memory so /chat can answer about those tickers without a restart.
            loaded = persist.load_history()
            if isinstance(loaded, dict) and loaded.get("date") == date.today().isoformat():
                refreshed, _ = _history_to_day(loaded)
                researches.update(refreshed)
        else:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner
            summary = "🛑 Stopped — /auto aborted."
        await notifier.send(_fit_telegram(summary))
        typer.echo("✅ /auto complete — listening again.")

    async def handle_status() -> None:
        try:
            broker = _broker()
            account = await asyncio.to_thread(broker.get_account)
            positions = await asyncio.to_thread(broker.get_positions)
            open_orders = await asyncio.to_thread(broker.list_orders, None, open_only=True)
        except Exception as exc:  # noqa: BLE001 — OpenD down is a message, not a crash
            await notifier.send(f"❌ Couldn't read the account (is OpenD running?): {exc}")
            return
        await notifier.send(
            format_status(
                mode, account, positions, open_orders,
                list(researches), watchlist.load(config),
            )
        )

    async def handle_orders() -> None:
        try:
            open_orders = await asyncio.to_thread(_broker().list_orders, None, open_only=True)
        except Exception as exc:  # noqa: BLE001
            await notifier.send(f"❌ Couldn't read the orders (is OpenD running?): {exc}")
            return
        if not open_orders:
            await notifier.send("⏳ No open orders — nothing resting on the book.")
            return
        lines = ["⏳ **Open orders**", ""]
        for o in open_orders:
            price = f" @ {o['price']:g}" if o.get("price") else " (market)"
            lines.append(
                f"• #{o['order_id']}: {o['side']} {o['quantity']:g} {o['symbol']}{price}"
                f" — {o['order_status']}"
            )
        lines.append("")
        lines.append("Tap a button or send `/cancel <id>` to cancel one.")
        # One tap-to-cancel button per order, each on its own keyboard row so
        # the labels stay readable no matter how many orders are resting.
        buttons = [
            [(f"❌ Cancel #{o['order_id']} {o['symbol']}", f"/cancel {o['order_id']}")]
            for o in open_orders
        ]
        await notifier.send("\n".join(lines), buttons=buttons)

    async def handle_cancel_order(order_id: int) -> None:
        try:
            await asyncio.to_thread(_broker().cancel_order, order_id)
        except Exception as exc:  # noqa: BLE001 — already filled/cancelled, bad id…
            await notifier.send(f"❌ Couldn't cancel order {order_id}: {exc}")
            return
        state.append_event(audit, {"event": "cancel", "order_id": order_id})
        await notifier.send(f"🧹 Cancelled order {order_id}.")

    async def handle_watchlist(action: str | None, ticker: str | None) -> None:
        if action == "add" and ticker:
            try:
                tickers, added = await asyncio.to_thread(watchlist.add, config, ticker)
            except ValueError as exc:
                await notifier.send(f"❌ Can't watch {ticker}: {exc}")
                return
            verb = "Added" if added else "Already watching"
            await notifier.send(f"👀 {verb} {ticker}. Watchlist: {', '.join(tickers)}")
            return
        if action == "remove" and ticker:
            tickers, removed = await asyncio.to_thread(watchlist.remove, config, ticker)
            if removed:
                shown = ", ".join(tickers) if tickers else "(empty)"
                await notifier.send(f"👀 Removed {ticker}. Watchlist: {shown}")
            else:
                await notifier.send(f"👀 {ticker} wasn't on the watchlist.")
            return
        tickers = watchlist.load(config)
        if tickers:
            await notifier.send(
                f"👀 Watchlist: {', '.join(tickers)}\n"
                "`/watchlist add AMD` or `/watchlist remove AMD` to edit it."
            )
        else:
            await notifier.send(
                "👀 The watchlist is empty — `/watchlist add NVDA` to start one."
            )

    # ---- background checks, run between messages ----------------------------

    schedule = config.parsed_auto_schedule()
    last_scheduled: str | None = None
    # Fill watcher: a status snapshot per order id, diffed on each poll. The
    # first poll is deferred so startup (and the tests) never block on OpenD.
    known_orders: dict[int, str] = {}
    last_fill_check = time.monotonic()

    async def run_background_checks() -> None:
        nonlocal last_scheduled, known_orders, last_fill_check
        # Daily scheduled /auto — quiet: only orders/errors + the final summary.
        if schedule is not None:
            plan = auto_plan.load_plan()
            plan_date = plan.get("date") if isinstance(plan, dict) else None
            if _schedule_due(schedule, datetime.now(), last_scheduled, plan_date):
                last_scheduled = datetime.now().date().isoformat()
                typer.echo("⏰ Scheduled /auto starting…")
                await handle_auto(config.auto_schedule_budget or None, quiet=True)
        # Fill watcher — polls the (local) broker every couple of minutes and
        # messages only on a fill: "notify me only when something is serious".
        if time.monotonic() - last_fill_check >= _FILL_POLL_SECONDS:
            last_fill_check = time.monotonic()
            try:
                orders = await asyncio.to_thread(_broker().list_orders)
            except Exception:  # noqa: BLE001 — OpenD down: skip quietly, retry later
                return
            messages, known_orders = _fill_messages(known_orders, orders)
            for message in messages:
                state.append_event(audit, {"event": "fill", "message": message})
                with contextlib.suppress(Exception):
                    await notifier.send(message)

    while True:
        # The idle wait wakes periodically so the background checks (schedule,
        # fills) run even when no one is messaging the bot.
        text = None
        try:
            text = await asyncio.wait_for(
                notifier.wait_for_ticker(), timeout=_IDLE_WAKE_SECONDS
            )
        except asyncio.TimeoutError:
            pass
        await run_background_checks()
        if text is None:
            continue
        if is_stop(text):
            await notifier.send("🛑 Stopped — nothing was running. Send a ticker like `NVDA` to start.")
            continue
        if is_help_command(text):
            await notifier.send(HELP_TEXT)
            continue
        if is_status_command(text):
            await handle_status()
            continue
        if is_orders_command(text):
            await handle_orders()
            continue
        cancel_id = parse_cancel_order(text)
        if cancel_id is not None:
            await handle_cancel_order(cancel_id)
            continue
        is_wl, wl_action, wl_ticker = parse_watchlist_command(text)
        if is_wl:
            await handle_watchlist(wl_action, wl_ticker)
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
        is_execute, exec_ticker = parse_execute_command(text)
        if is_execute:
            await handle_execute(exec_ticker)
            continue
        is_auto, budget, err = parse_auto_command(text)
        if is_auto:
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
