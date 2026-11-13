"""Entry point for the execution-agent CLI.

``czagent run TICKER --date YYYY-MM-DD`` runs the research
graph, asks how much to trade over Telegram, asks for final approval, then
places the order on Moomoo (paper by default). Every step is written to the
audit log (``~/.tradingagents/execution/audit.jsonl``).
"""

from __future__ import annotations

import asyncio

import typer

from execution_agent import state
from execution_agent.broker import MoomooBroker
from execution_agent.config import ExecutionConfig, mode_label
from execution_agent.decision import parse_amount, quantity_for, run_decision, to_order
from execution_agent.notify import (
    APPROVE,
    BUY,
    REJECT,
    SELL,
    SHARES,
    SKIP,
    STOP,
    USD,
    TelegramNotifier,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.portfolio import load_portfolio

app = typer.Typer(
    name="czagent",
    help=(
        "Execution agent: run TradingAgents, get Telegram approval, and place "
        "orders on Moomoo (paper by default)."
    ),
    no_args_is_help=True,
)


# Telegram rejects a single message over 4096 characters, so a summary longer
# than that can't go in one shot. Send the analyst's full reasoning and truncate
# only at Telegram's own ceiling — never at a short "preview" length that would
# hide most of the decision.
TELEGRAM_MAX_CHARS = 4096


def _fit_telegram(text: str, limit: int = TELEGRAM_MAX_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _print_decision(decision) -> None:
    side = decision.side or "no trade"
    typer.echo(f"Ticker: {decision.yahoo} ({decision.moomoo})")
    typer.echo(f"Rating: {decision.signal} -> {side}")
    typer.echo(decision.summary or "(no summary)")


@app.command()
def run(
    ticker: str = typer.Argument(..., help="Ticker, e.g. NVDA or 0700.HK"),
    date: str = typer.Option(..., "--date", "-d", help="Analysis date, YYYY-MM-DD"),
    portfolio: str = typer.Option(None, "--portfolio", help="Path to a portfolio JSON book"),
    decide_only: bool = typer.Option(
        False, "--decide-only", help="Run the decision and stop (no Telegram, no order)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Run the full approval flow but do not place the order"
    ),
) -> None:
    """Run TradingAgents, get Telegram approval, and place a Moomoo order."""
    config = ExecutionConfig.from_env()
    book = load_portfolio(portfolio) if portfolio else None
    asyncio.run(_run(ticker, date, config, book, decide_only, dry_run))


async def _run(ticker, date, config, portfolio, decide_only, dry_run) -> None:
    audit = state.audit_path()
    try:
        graph = TradingAgentsGraph(debug=False, config=DEFAULT_CONFIG.copy())
        decision = run_decision(graph, ticker, date, config=config, portfolio=portfolio)
        state.append_event(
            audit,
            {"event": "decision", "ticker": ticker, "signal": decision.signal, "side": decision.side},
        )

        if decide_only:
            _print_decision(decision)
            return

        if decision.side is None:
            typer.echo(f"No order: {decision.signal}")
            state.append_event(audit, {"event": "skip", "reason": decision.signal})
            return

        if not config.telegram_bot_token or not config.telegram_chat_id:
            typer.echo(
            "Missing EXECUTION_AGENT_TELEGRAM_BOT_TOKEN / EXECUTION_AGENT_TELEGRAM_CHAT_ID "
            "(set them in .env).",
            err=True,
        )
            raise typer.Exit(code=2)

        notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
        summary = await approve_and_place(notifier, config, decision, audit, dry_run)
        if summary:
            typer.echo(summary)
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 — log every failure, then exit
        state.append_event(audit, {"event": "error", "message": str(exc)})
        typer.echo(f"❌ {exc}", err=True)
        raise typer.Exit(code=1) from exc


async def approve_and_place(
    notifier, config, decision, audit, dry_run, trading_env: str | None = None
) -> str:
    """Confirm/flip direction, ask amount, approve, and place the order.

    Shared by the one-shot ``run`` command and the persistent ``serve`` bot. The
    AI's Buy/Sell is a *recommendation* — the human confirms it or flips it —
    then amount -> dollars/shares -> Approve/Reject. All interaction goes
    through ``notifier``; a broker call only happens after an explicit Approve.
    ``trading_env`` overrides ``config.trading_env`` (the serve bot lets the
    user switch paper/live at runtime). Returns a one-line human summary, or
    ``""`` when nothing was placed (cancelled, rejected, timed out, zero qty).
    """
    trading_env = trading_env or config.trading_env

    # 0. Direction: the AI recommends Buy/Sell; the human confirms or flips it.
    # The full summary is sent here, capped only so the whole message (header +
    # summary + footer) fits Telegram's 4096-char limit. The mode is shown up
    # front so a paper/live mix-up can't slip through.
    header = (
        f"{mode_label(trading_env)}\n"
        f"TradingAgents says {decision.side} {decision.yahoo} (rating: {decision.signal}).\n\n"
    )
    footer = "\n\nConfirm or change direction?"
    summary = _fit_telegram(decision.summary, TELEGRAM_MAX_CHARS - len(header) - len(footer))
    choice = await notifier.ask_button(
        header + summary + footer,
        [("🟢 Buy", BUY), ("🔴 Sell", SELL), ("⏭ Skip", SKIP)],
        config.approval_timeout_seconds,
    )
    if choice is None or choice in (SKIP, STOP):
        if choice is None:
            reason = "timeout"
        elif choice == SKIP:
            reason = "skipped"
        else:
            reason = "stopped"
        typer.echo(f"Not placed ({reason}) — no order.")
        state.append_event(audit, {"event": "skip", "reason": f"direction_{reason}"})
        return "🛑 Stopped — no order placed." if choice == STOP else ""
    side = "BUY" if choice == BUY else "SELL"
    state.append_event(audit, {"event": "direction", "side": side})

    # 1. How much?
    amount_text = await notifier.ask_text(
        "How much do you want to trade? (e.g. 500 or 10 shares)",
        config.approval_timeout_seconds,
    )
    if amount_text is None:
        typer.echo("No reply — cancelled.")
        state.append_event(audit, {"event": "skip", "reason": "timeout_on_amount"})
        return ""
    if amount_text == STOP:
        typer.echo("Stopped — no order.")
        state.append_event(audit, {"event": "skip", "reason": "stopped_on_amount"})
        return "🛑 Stopped — no order placed."

    try:
        number, unit = parse_amount(amount_text)
    except ValueError as exc:
        typer.echo(f"Couldn't read that amount: {exc}", err=True)
        state.append_event(audit, {"event": "skip", "reason": f"bad_amount: {exc}"})
        return ""

    # 2. Dollars or shares?
    if unit is None:
        unit = await notifier.ask_button(
            f"{number} — dollars or shares?",
            [("💵 Dollars", USD), ("🔢 Shares", SHARES)],
            config.approval_timeout_seconds,
        )
        if unit is None:
            typer.echo("No reply — cancelled.")
            state.append_event(audit, {"event": "skip", "reason": "timeout_on_unit"})
            return ""
        if unit == STOP:
            typer.echo("Stopped — no order.")
            state.append_event(audit, {"event": "skip", "reason": "stopped_on_unit"})
            return "🛑 Stopped — no order placed."

    state.append_event(audit, {"event": "amount", "number": number, "unit": unit})

    # 3. Compute quantity
    broker = MoomooBroker(
        host=config.moomoo_host, port=config.moomoo_port, trading_env=trading_env
    )
    sizing_mode = "shares" if unit == SHARES else "notional"
    last_price = broker.last_price(decision.moomoo) if sizing_mode == "notional" else None
    quantity = quantity_for(
        side,
        sizing_mode=sizing_mode,
        sizing_amount=number,
        cash=decision.cash,
        position_quantity=decision.position_quantity,
        last_price=last_price,
    )
    order = to_order(decision, quantity, side=side)
    if order is None:
        typer.echo("Nothing to order (quantity rounds to zero).")
        state.append_event(audit, {"event": "skip", "reason": "zero_quantity"})
        return ""

    state.append_event(
        audit,
        {"event": "proposal", "side": order.side, "quantity": order.quantity,
         "symbol_moomoo": order.symbol_moomoo},
    )

    # 4. Approve / Reject — the mode is restated here, the last human gate
    # before the order, so the user always knows paper vs real before it buys.
    choice = await notifier.ask_button(
        f"Place this order?\n\n{mode_label(trading_env)}\n\n"
        f"{order.side} {order.quantity} {order.symbol_moomoo} (rating: {order.rating})",
        [("✅ Approve", APPROVE), ("❌ Reject", REJECT)],
        config.approval_timeout_seconds,
    )
    if choice != APPROVE:
        if choice == REJECT:
            reason = "rejected"
        elif choice == STOP:
            reason = "stopped"
        else:
            reason = "timeout"
        typer.echo(f"Not approved ({reason}) — no order placed.")
        state.append_event(audit, {"event": "approval", "choice": reason})
        return "🛑 Stopped — no order placed." if choice == STOP else ""
    state.append_event(audit, {"event": "approval", "choice": "approved"})

    # 5. Execute
    if dry_run:
        summary = f"[dry-run] would place: {order.side} {order.quantity} {order.symbol_moomoo}"
        state.append_event(
            audit, {"event": "dry_run", "side": order.side, "quantity": order.quantity}
        )
        return summary

    result = broker.place_order(order.symbol_moomoo, order.side, order.quantity)
    state.append_event(audit, {"event": "order", **result})
    return f"✅ Order placed: {result['order_id']} ({result['order_status']})"


@app.command()
def serve() -> None:
    """Run a persistent Telegram bot — start trades from chat, leave it running."""
    config = ExecutionConfig.from_env()
    if not config.telegram_bot_token or not config.telegram_chat_id:
        typer.echo(
            "Missing EXECUTION_AGENT_TELEGRAM_BOT_TOKEN / EXECUTION_AGENT_TELEGRAM_CHAT_ID "
            "(set them in .env).",
            err=True,
        )
        raise typer.Exit(code=2)

    from execution_agent.serve import serve_loop  # lazy: avoids a main <-> serve cycle

    asyncio.run(serve_loop(config))


if __name__ == "__main__":
    app()
