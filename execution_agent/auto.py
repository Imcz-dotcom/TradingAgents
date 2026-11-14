"""Fully-automatic paper-trading loop behind the ``/auto`` command.

``/auto`` is the opposite of the human-in-the-loop flow: it reads the account,
researches every ticker in the universe (the watchlist plus current holdings),
decides buy/sell on its own, and places the orders — **paper only**. It follows
the research's own price levels: buy at the trader's entry price, sell at the
portfolio manager's price target, as NORMAL (limit) orders, falling back to
MARKET when a level is absent or absurd.

Money is a *budget*, not a per-trade size. ``/auto 500`` means "I have $500 of
new money today": the loop splits that budget across the buy ideas by the
research's own conviction (``Buy`` outweighs ``Overweight``). ``/auto`` with no
amount means "just manage": it sells/trims/holds the existing positions but
never deploys new cash. Sells follow the rating too — a ``Sell`` is a full exit,
an ``Underweight`` trims to a fraction (``auto_underweight_trim``) rather than
always dumping the whole position.

Each run persists a date-keyed "plan" (``execution_agent/auto_plan.py``). The
next ``/auto`` refer-backs to it: same day, it reuses the stored verdict; a
stale date, it re-researches. Every decision and order is appended to the audit
trail (``execution_agent/state.py``).

The loop is synchronous (research and broker calls block), so the serve bot runs
it in a worker thread and passes ``on_step`` (progress) and ``stop_check``
(cancellation) to stay responsive and abort on a "stop".
"""

from __future__ import annotations

import math
from datetime import date

from execution_agent import auto_plan, persist, state
from execution_agent.broker import MoomooBroker
from execution_agent.config import ExecutionConfig
from execution_agent.decision import run_decision_streaming
from execution_agent.prices import parse_prices, parse_time_horizon, time_in_force_for
from tradingagents.agents.rating import is_review
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.portfolio import PortfolioContext, Position


def sane_limit(limit: float | None, live: float | None, band: float) -> float | None:
    """The limit to place, or None when the level is unusable (→ MARKET).

    None when there is no level, no live quote, or the level is farther than
    ``band`` (a fraction) from ``live``. An out-of-band level is treated as a
    hallucination and *dropped*, never clamped to the band.
    """
    if limit is None or live is None or live <= 0:
        return None
    if abs(limit - live) / live > band:
        return None
    return limit


def _live_price(broker, moomoo: str) -> float | None:
    """Best-effort last price; None when the quote can't be read."""
    try:
        return broker.last_price(moomoo)
    except Exception:
        return None


# A "Buy" is a stronger call than an "Overweight", so it earns a bigger slice of
# the /auto budget. Only these two ratings map to BUY (see DEFAULT_SIDE_MAPPING).
_BUY_WEIGHT = {"Buy": 2.0, "Overweight": 1.0}


def conviction_weight(signal: str) -> float:
    """The buy-conviction weight for a rating: Buy (2) > Overweight (1) > 0."""
    return _BUY_WEIGHT.get(signal, 0.0)


def trim_quantity(signal: str, position_quantity: float, trim: float) -> float:
    """Shares to sell for a rating: full exit for ``Sell``, ``trim`` of the
    position for ``Underweight`` (floored to whole units).
    """
    if signal == "Sell":
        return float(position_quantity)
    return float(math.floor(position_quantity * trim))


def allocate_budget(buys: list[tuple[str, float]], budget: float | None) -> dict[str, float]:
    """Split ``budget`` across buy candidates proportional to their weights.

    ``buys`` is a list of ``(ticker, weight)``; returns ``ticker -> notional``.
    Empty, zero-total, or no budget returns an empty map (nothing to deploy).
    """
    if budget is None or budget <= 0:
        return {}
    total = sum(w for _, w in buys)
    if total <= 0:
        return {}
    return {ticker: budget * w / total for ticker, w in buys if w > 0}


def decide_action(
    decision,
    prices: dict,
    book: PortfolioContext,
    config: ExecutionConfig,
    live: float | None,
    *,
    notional: float | None = None,
    time_in_force: str = "DAY",
) -> dict:
    """The action for one decision: side, quantity, order type, limit, reason.

    Pure — no broker, network, or I/O — so it unit-tests directly. ``decision``
    is a ``DecisionResult``; ``book`` is the account's ``PortfolioContext``.
    ``notional`` is the dollar slice this BUY was allocated (``None`` means no
    budget — a BUY is skipped, never re-sized by a fixed default).
    ``time_in_force`` ("GTC"/"DAY") is applied to LIMIT orders only — a MARKET
    order fills immediately, so it always rests as DAY.
    """
    side = decision.side
    if side is None:
        reason = "review" if is_review(decision.signal) else "hold"
        return {"side": None, "quantity": 0.0, "order_type": None, "limit_price": None, "reason": reason}

    position = book.position_in(decision.yahoo)
    if side == "SELL":
        if position is None or position.quantity <= 0:
            return {"side": "SELL", "quantity": 0.0, "order_type": None, "limit_price": None, "reason": "no_position"}
        qty = trim_quantity(decision.signal, position.quantity, config.auto_underweight_trim)
        if qty <= 0:  # an Underweight trim that floors to zero units
            return {"side": "SELL", "quantity": 0.0, "order_type": None, "limit_price": None, "reason": "no_position"}
        limit = sane_limit(prices["price_target"], live, config.limit_price_band)
        order_type = "LIMIT" if limit is not None else "MARKET"
        return {
            "side": "SELL",
            "quantity": qty,
            "order_type": order_type,
            "limit_price": limit,
            "time_in_force": time_in_force if order_type == "LIMIT" else "DAY",
            "reason": "ok",
        }

    # BUY — only with a budget slice; otherwise manage-mode leaves cash alone.
    if not notional or notional <= 0:
        return {"side": "BUY", "quantity": 0.0, "order_type": None, "limit_price": None, "reason": "no_budget"}
    if not live or live <= 0:
        return {"side": "BUY", "quantity": 0.0, "order_type": None, "limit_price": None, "reason": "no_price"}
    qty = float(math.floor(notional / live))
    if qty <= 0:  # budget too small to buy even one share
        return {"side": "BUY", "quantity": 0.0, "order_type": None, "limit_price": None, "reason": "no_price"}
    limit = sane_limit(prices["entry_price"], live, config.limit_price_band)
    order_type = "LIMIT" if limit is not None else "MARKET"
    return {
        "side": "BUY",
        "quantity": qty,
        "order_type": order_type,
        "limit_price": limit,
        "time_in_force": time_in_force if order_type == "LIMIT" else "DAY",
        "reason": "ok",
    }


def _entry_line(entry: dict) -> str:
    """One plan entry as a single Telegram line."""
    ticker = entry["ticker"]
    reason = entry["reason"]
    if reason == "ok":
        qty = f"{entry['quantity']:g}"
        if entry["order_type"] == "LIMIT" and entry["limit_price"] is not None:
            tif = entry.get("time_in_force") or "DAY"
            kind = f"LIMIT {tif} @ {entry['limit_price']:g}"
        else:
            kind = "MARKET"
        order = entry["executed"] or {}
        oid = order.get("order_id", "?")
        alloc = ""
        if entry.get("side") == "BUY" and entry.get("notional"):
            alloc = f" ~${entry['notional']:,.0f}"
        return f"✅ {ticker}: {entry['side']} {qty} ({kind}){alloc} — order {oid}"
    if reason == "hold":
        return f"➖ {ticker}: hold — no action"
    if reason == "review":
        return f"⚠️ {ticker}: REVIEW — skipped"
    if reason == "no_position":
        return f"➖ {ticker}: sell called but no position — skipped"
    if reason == "no_budget":
        return f"➖ {ticker}: buy called but no budget — skipped"
    if reason == "no_price":
        return f"⚠️ {ticker}: no usable price/size — skipped"
    return f"⚠️ {ticker}: {reason}"


def _summarize(day: dict) -> str:
    """The human summary of a stored plan (budget + cash + positions + lines)."""
    lines = [f"📊 Auto paper scan — {day.get('date')}"]
    currency = day.get("currency")
    budget = day.get("budget")
    if budget:
        lines.append(f"💰 Budget: {budget:,.2f}{' ' + currency if currency else ''} to deploy")
    else:
        lines.append("💰 Budget: none — manage only (sell/trim/hold, no new buys)")
    cash = day.get("cash")
    if cash is not None:
        lines.append(f"💵 Cash: {cash:,.2f}{' ' + currency if currency else ''}")
    positions = day.get("positions") or []
    if positions:
        held = ", ".join(f"{p['ticker']} ×{p['quantity']:g}" for p in positions)
        lines.append(f"📈 Positions: {held}")
    else:
        lines.append("📈 Positions: none")
    plan = day.get("plan") or []
    if plan:
        lines.append("")
        lines.extend(_entry_line(e) for e in plan)
    return "\n".join(lines)


def run_auto(
    config: ExecutionConfig,
    *,
    budget: float | None = None,
    trading_env: str | None = None,
    audit: str | None = None,
    on_step=None,
    stop_check=None,
) -> str:
    """Run the whole auto loop once and return a one-message summary.

    ``budget`` is the dollars of *new* money to deploy today (``None`` means
    manage-only: sell/trim/hold, no new buys). ``trading_env`` overrides
    ``config.trading_env`` (the serve bot lets the user switch paper/live at
    runtime); /auto is paper-only and refuses anything else. ``on_step(label)``
    receives progress; ``stop_check() -> bool`` aborts the loop at the next
    boundary when True. ``audit`` is the audit-log path (skipped when None).
    """
    trading_env = trading_env or config.trading_env
    if trading_env != "SIMULATE":
        return "🔒 /auto is paper-only — it refuses to run in 🔴 REAL mode. Use `/mode paper` first."

    today = date.today().isoformat()

    # Refer-back: a same-day plan is reused, not re-researched (the user's
    # "date-aware" memory). A stale date means the stored verdict is outdated.
    stored = auto_plan.load_plan()
    if isinstance(stored, dict) and stored.get("date") == today:
        return "📚 Already ran /auto today — reusing today's plan (nothing re-researched):\n\n" + _summarize(stored)

    broker = MoomooBroker(
        host=config.moomoo_host, port=config.moomoo_port, trading_env=trading_env
    )
    try:
        acct = broker.get_account()
        positions = broker.get_positions()
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the bot
        if audit:
            state.append_event(audit, {"event": "error", "message": f"auto account read: {exc}"})
        return f"❌ /auto couldn't read the account (is OpenD logged in?): {exc}"

    book = PortfolioContext(
        cash=acct.get("cash"),
        currency=acct.get("currency"),
        positions=[
            Position(ticker=p["symbol"], quantity=p["quantity"], average_price=p["average_price"])
            for p in positions
        ],
    )

    # Universe = watchlist ∪ current holdings, deduped, order preserved.
    universe: list[str] = []
    seen: set[str] = set()
    for ticker in config.watchlist_tickers() + [p.ticker for p in book.positions]:
        key = ticker.upper()
        if key not in seen:
            seen.add(key)
            universe.append(ticker)

    plan_entries: list[dict] = []
    graph = TradingAgentsGraph(debug=False, config=DEFAULT_CONFIG.copy())

    def _stop_and_save() -> str:
        day = _make_day(today, acct, book, universe, plan_entries, budget)
        auto_plan.save_plan(day)
        return "🛑 Stopped during /auto — partial plan saved, no further orders.\n\n" + _summarize(day)

    # Pass 1 — research every ticker first, so the budget can be split by
    # conviction across *all* the buy ideas before any order goes out.
    candidates: list[dict] = []
    for ticker in universe:
        if stop_check is not None and stop_check():
            return _stop_and_save()

        # Reuse a same-day result from the interactive /research flow (it
        # persists every run to the shared history) instead of re-paying for a
        # full pipeline run. The verdict is reused; the live quote is still
        # fetched below so sizing reflects the current price.
        decision = persist.load_today_decision(normalize_symbol(ticker))
        if decision is None:
            if on_step is not None:
                on_step(f"🔎 Auto: researching {ticker}…")
            try:
                decision = run_decision_streaming(
                    graph,
                    ticker,
                    today,
                    config=config,
                    portfolio=book,
                    on_step=on_step,
                    stop_check=stop_check,
                )
            except ValueError as exc:  # bad ticker (crypto/forex/unknown)
                plan_entries.append({"ticker": ticker, "side": None, "reason": "error", "signal": str(exc)})
                if audit:
                    state.append_event(audit, {"event": "auto_error", "ticker": ticker, "message": str(exc)})
                continue
            except Exception as exc:  # noqa: BLE001 — one bad run must not kill the loop
                plan_entries.append({"ticker": ticker, "side": None, "reason": "error", "signal": str(exc)})
                if audit:
                    state.append_event(audit, {"event": "auto_error", "ticker": ticker, "message": str(exc)})
                continue
            if decision is None:  # cancelled mid-run by a stop
                return _stop_and_save()
        elif on_step is not None:
            on_step(f"📚 Auto: reusing today's research on {decision.yahoo}")

        prices = parse_prices(decision.research)
        live = _live_price(broker, decision.moomoo) if decision.side is not None else None
        horizon = parse_time_horizon(decision.research)
        candidates.append({"decision": decision, "prices": prices, "live": live, "horizon": horizon})

    # Pass 2 — split the budget across buy candidates by conviction, then decide.
    buys = [
        (c["decision"].yahoo, conviction_weight(c["decision"].signal))
        for c in candidates
        if c["decision"].side == "BUY"
    ]
    notional = allocate_budget(buys, budget)

    for c in candidates:
        if stop_check is not None and stop_check():
            return _stop_and_save()

        decision = c["decision"]
        prices = c["prices"]

        # A fresh verdict supersedes any resting orders from a prior day: cancel
        # them first so a stale GTC limit can't fire against today's call.
        try:
            cancelled = broker.cancel_open_orders(decision.moomoo)
            if cancelled:
                if on_step is not None:
                    on_step(f"🧹 Auto: cancelled {cancelled} stale order(s) on {decision.yahoo}")
                if audit:
                    state.append_event(audit, {"event": "auto_cancel", "ticker": decision.yahoo, "count": cancelled})
        except Exception as exc:  # noqa: BLE001 — cancel is best-effort; place today's order regardless
            if audit:
                state.append_event(audit, {"event": "auto_error", "ticker": decision.yahoo, "message": f"cancel stale: {exc}"})

        slice_ = notional.get(decision.yahoo) if decision.side == "BUY" else None
        action = decide_action(
            decision, prices, book, config, c["live"],
            notional=slice_, time_in_force=time_in_force_for(c["horizon"]),
        )

        if audit:
            state.append_event(
                audit,
                {
                    "event": "auto_decision",
                    "ticker": decision.yahoo,
                    "signal": decision.signal,
                    "side": action["side"],
                    "reason": action["reason"],
                },
            )

        # Pass 3 — place the order (limit/market) and a protective stop-loss.
        executed = None
        stop_order = None
        if action["reason"] == "ok" and action["quantity"] > 0:
            try:
                result = broker.place_order(
                    decision.moomoo, action["side"], action["quantity"],
                    price=action["limit_price"], time_in_force=action["time_in_force"],
                )
                executed = {"order_id": result["order_id"], "order_status": result["order_status"]}
                if on_step is not None:
                    on_step(f"✅ Auto: {action['side']} {decision.yahoo} — order {executed['order_id']}")
                if audit:
                    state.append_event(
                        audit,
                        {"event": "order", "ticker": decision.yahoo, "side": action["side"], **result},
                    )
                # A protective stop-loss is only meaningful once the position
                # exists. A MARKET buy fills immediately (paper), so place the
                # stop right after it; a pending LIMIT buy has nothing to protect
                # yet, so the stop level is recorded in the plan for a later run.
                if (
                    action["side"] == "BUY"
                    and action["order_type"] == "MARKET"
                    and prices["stop_loss"] is not None
                ):
                    try:
                        stop_result = broker.place_order(
                            decision.moomoo, "SELL", action["quantity"],
                            order_type="STOP", price=prices["stop_loss"], time_in_force="GTC",
                        )
                        stop_order = {"order_id": stop_result["order_id"], "order_status": stop_result["order_status"]}
                        if on_step is not None:
                            on_step(f"🛡️ Auto: stop-loss on {decision.yahoo} @ {prices['stop_loss']:g} — order {stop_order['order_id']}")
                        if audit:
                            state.append_event(
                                audit,
                                {"event": "order", "ticker": decision.yahoo, "side": "SELL",
                                 "order_type": "STOP", **stop_result},
                            )
                    except Exception as exc:  # noqa: BLE001 — stop rejected; record, don't fail the entry
                        stop_order = None
                        if audit:
                            state.append_event(
                                audit, {"event": "auto_error", "ticker": decision.yahoo, "message": f"stop-loss: {exc}"}
                            )
            except Exception as exc:  # noqa: BLE001 — a rejected order is recorded, not fatal
                executed = None
                action["reason"] = "error"
                if audit:
                    state.append_event(audit, {"event": "auto_error", "ticker": decision.yahoo, "message": str(exc)})

        plan_entries.append(
            {
                "ticker": decision.yahoo,
                "side": action["side"],
                "signal": decision.signal,
                "quantity": action["quantity"],
                "order_type": action["order_type"],
                "limit_price": action["limit_price"],
                "notional": slice_ if decision.side == "BUY" else None,
                "weight": conviction_weight(decision.signal) if decision.side == "BUY" else None,
                "entry_price": prices["entry_price"],
                "stop_loss": prices["stop_loss"],
                "price_target": prices["price_target"],
                "time_horizon": c["horizon"],
                "time_in_force": action.get("time_in_force"),
                "summary": decision.summary,
                "executed": executed,
                "stop_order": stop_order,
                "reason": action["reason"],
            }
        )

    day = _make_day(today, acct, book, universe, plan_entries, budget)
    auto_plan.save_plan(day)
    return _summarize(day)


def _make_day(today: str, acct: dict, book: PortfolioContext, universe: list[str], plan_entries: list[dict], budget: float | None) -> dict:
    """Assemble the persisted day document."""
    return {
        "date": today,
        "budget": budget,
        "cash": acct.get("cash"),
        "currency": acct.get("currency"),
        "positions": [
            {"ticker": p.ticker, "quantity": p.quantity, "average_price": p.average_price}
            for p in book.positions
        ],
        "universe": universe,
        "plan": plan_entries,
    }
