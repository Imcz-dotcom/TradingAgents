"""Fully-automatic paper-OR-approved-live trading loop behind ``/auto``.

``/auto`` is the opposite of the human-in-the-loop flow: it reads the account,
researches every ticker in the universe (the watchlist plus current holdings),
and decides buy/sell on its own. It follows the research's own price levels:
buy at the trader's entry price, sell at the portfolio manager's price target,
as NORMAL (limit) orders, falling back to MARKET when a level is absent or
absurd.

On paper (``SIMULATE``) every decided trade is placed immediately, exactly as
before — there is nothing to approve when no real money moves. On ``REAL``,
placing an order autonomously would mean the bot spends real money with no
human in the loop at all, which is a different risk than every other trading
path in this agent (even ``/execute`` always stops for a tap). So on REAL,
each trade /auto wants to place is instead sent to Telegram as an
Approve/Reject prompt (via the ``approve`` callback) and only placed if
approved; a timeout or Reject skips that trade, same as everywhere else in
this agent ("timeout = reject, never a silent yes"). Protective stop-losses and
cancelling a superseded resting order are not re-approved — they only reduce
risk on a position the human already approved into.

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
it in a worker thread and passes ``on_step`` (progress), ``stop_check``
(cancellation), and on REAL, ``approve`` (per-trade approval) to stay
responsive and abort on a "stop".
"""

from __future__ import annotations

import math
from datetime import date

from execution_agent import auto_plan, persist, state, watchlist
from execution_agent.broker import MoomooBroker
from execution_agent.config import ExecutionConfig, mode_label
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


# A Hold-pullback buy is deliberately a weaker claim than even an Overweight —
# it's not the LLM's own conviction, it's an execution-layer overlay the user
# opted into. If a genuine Buy/Overweight is competing for the same budget,
# that gets priority.
HOLD_PULLBACK_WEIGHT = 0.5


def is_hold_pullback_candidate(decision, prices: dict, book: PortfolioContext, live: float | None) -> bool:
    """Whether a Hold-rated ticker qualifies for the opt-in pullback buy.

    Requires: the rating is exactly Hold (not REVIEW — ``decision.side`` is
    None for both), the ticker isn't currently held (Hold on an existing
    position means "keep it", not "buy more" — the bracket already protects
    it), the trader gave an entry price, and that price is genuinely *below*
    the live price. A pullback level at or above the live price would fill
    like a market order immediately — the opposite of "wait for it to come
    down" — so that's treated as unusable, same as no level at all.
    """
    if decision.side is not None or is_review(decision.signal):
        return False
    position = book.position_in(decision.yahoo)
    if position is not None and position.quantity > 0:
        return False
    entry = prices.get("entry_price")
    return entry is not None and entry > 0 and live is not None and live > 0 and entry < live


def hold_pullback_action(prices: dict, live: float | None, notional: float | None, time_in_force: str) -> dict:
    """The action for an opted-in Hold-pullback buy: always a LIMIT at the
    trader's entry price, sized off that entry price (not the live price,
    since the order won't fill at live) — never a MARKET fallback, since
    buying right now at the live price defeats the entire point of waiting
    for a pullback.

    Pure — no broker, network, or I/O. Caller must already have confirmed
    :func:`is_hold_pullback_candidate`; this only guards budget/sizing.
    """
    entry = prices["entry_price"]
    if not notional or notional <= 0:
        return {"side": "BUY", "quantity": 0.0, "order_type": None, "limit_price": None, "reason": "no_budget"}
    qty = float(math.floor(notional / entry))
    if qty <= 0:  # budget too small to buy even one share at the pullback level
        return {"side": "BUY", "quantity": 0.0, "order_type": None, "limit_price": None, "reason": "no_price"}
    return {
        "side": "BUY",
        "quantity": qty,
        "order_type": "LIMIT",
        "limit_price": entry,
        "time_in_force": time_in_force,
        "reason": "ok",
    }


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


def position_qty_after(book: PortfolioContext, yahoo: str, action: dict, executed) -> float:
    """The held quantity still needing a bracket, after this run's own action.

    A MARKET order fills synchronously (paper), so a BUY changes the position
    immediately. A SELL (full exit or trim), whether it fills now (MARKET) or
    rests (LIMIT), already has its own order chasing ``price_target`` for that
    quantity — those shares must NOT also get wrapped in a bracket's take-profit,
    or the same shares would have two competing sell orders. So a successful
    SELL always subtracts its quantity, filled or not; only a BUY's effect is
    conditional on actually having filled (a pending LIMIT buy has nothing to
    protect yet — the bracket waits for a later run once it fills).
    """
    position = book.position_in(yahoo)
    starting = position.quantity if position else 0.0
    if action["side"] == "SELL" and action["reason"] == "ok":
        return max(0.0, starting - action["quantity"])
    if action["side"] == "BUY" and executed is not None and action["order_type"] == "MARKET":
        return starting + action["quantity"]
    return starting


def plan_bracket(new_qty: float, prices: dict) -> dict:
    """The stop/target orders to place for ``new_qty`` shares, or ``None`` for
    a side the research never gave a usable level for. Pure — no I/O.

    ``new_qty <= 0`` means nothing is held — no bracket to place at all.
    """
    if new_qty <= 0:
        return {"stop": None, "target": None}
    stop = (
        {"price": prices["stop_loss"], "quantity": new_qty}
        if prices.get("stop_loss") is not None else None
    )
    target = (
        {"price": prices["price_target"], "quantity": new_qty}
        if prices.get("price_target") is not None else None
    )
    return {"stop": stop, "target": target}


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
        if qty <= 0:
            # An Underweight trim floored to zero units (e.g. a fractional
            # dust position too small to meaningfully trim) — the position is
            # still genuinely held, so this must not report "no_position"
            # ("Not worth" — you don't own this), which the bracket-ensure
            # step (Pass 4) still runs for regardless, protecting it exactly
            # like any other held Hold-rated ticker.
            return {"side": "SELL", "quantity": 0.0, "order_type": None, "limit_price": None, "reason": "hold"}
        limit = sane_limit(prices["price_target"], live, config.limit_price_band)
        if limit is None and live is not None and live > 0:
            # No usable target (missing, or too far from live to trust) — a
            # raw MARKET order would accept *any* price, including a crashed
            # or illiquid print far below where the stock is actually
            # trading. Protect with a marketable limit near the live price
            # instead: it still fills almost immediately in normal
            # conditions, but won't fill into a bad tail print.
            limit = round(live * (1 - config.auto_market_sell_buffer), 2)
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


def verdict(entry: dict) -> str:
    """One of: ``Buy``, ``Sell``, ``Hold``, ``Not worth``, ``Buy @ X``,
    ``Sell @ X``, ``Buy @ X?``, ``Sell @ X?`` — or ``Error``. The reasoning
    behind any of these lives in the research itself; ask ``/chat`` for it,
    this is deliberately just the call.

    A bare ``Buy``/``Sell`` means the order already executed (a MARKET fill).
    ``Buy @ X``/``Sell @ X`` (no ``?``) means a real resting LIMIT order is
    sitting on the broker *right now* — Moomoo fires it automatically the
    instant price reaches ``X``, nothing further needed from you. ``Buy @
    X?``/``Sell @ X?`` (with the ``?``) means **nothing is set anywhere** —
    it's only the price that *would* trigger a buy/sell if you gave `/auto` a
    budget, a live price becomes available, or you approve it on REAL — the
    ``?`` is the signal that this one still needs something from you.
    """
    reason, side = entry["reason"], entry.get("side")
    if reason == "ok":
        label = "Buy" if side == "BUY" else "Sell"
        if entry.get("order_type") == "MARKET":
            return label
        price = entry.get("limit_price")
        return f"{label} @ {price:g}" if price is not None else label
    if reason == "hold":
        return "Hold"
    if reason == "review":
        return "Review"
    if reason == "no_position":
        return "Not worth"
    if reason in ("no_budget", "no_price"):
        price = entry.get("entry_price")
        return f"Buy @ {price:g}?" if price is not None else "Buy?"
    if reason == "not_approved":
        label = "Buy" if side == "BUY" else "Sell"
        price = entry.get("limit_price")
        return f"{label} @ {price:g}?" if price is not None else f"{label}?"
    return "Error"


def levels_hint(entry: dict) -> str:
    """The price levels relevant alongside a verdict — what "hold till" or
    "re-entry at" actually means, as real numbers.

    A ticker carrying a bracket (any position actually held — freshly bought,
    or a Hold-rated holding backfilled with one) shows the stop/target it's
    literally being protected and exited by — that's the direct answer to
    "hold till what price". A ticker with no position (``Not worth``) falls
    back to the research's own target as a re-entry reference. A ``Hold`` on
    a ticker you don't hold at all has no exit level to show, so it falls
    back one step further to the trader's entry price — a watch level, not
    an order. ``-`` only when none of this exists, which is normal: not every
    research run produces a usable target or entry (see /chat for why).
    """
    bracket = entry.get("bracket") or {}
    parts = []
    if bracket.get("stop_price") is not None:
        parts.append(f"stop {bracket['stop_price']:g}")
    if bracket.get("target_price") is not None:
        parts.append(f"target {bracket['target_price']:g}")
    if parts:
        return " / ".join(parts)
    if entry.get("price_target") is not None:
        return f"target {entry['price_target']:g}"
    if entry["reason"] == "hold" and entry.get("entry_price") is not None:
        return f"watch {entry['entry_price']:g}"
    return "-"


def verdict_table(plan: list[dict]) -> str:
    """The whole day's calls as one compact, aligned monospace table."""
    rows = [(e["ticker"], verdict(e), levels_hint(e)) for e in plan]
    if not rows:
        return ""
    ticker_w = max(len("Ticker"), *(len(t) for t, _, _ in rows))
    verdict_w = max(len("Verdict"), *(len(v) for _, v, _ in rows))
    lines = [
        f"{'Ticker'.ljust(ticker_w)}  {'Verdict'.ljust(verdict_w)}  Levels",
        f"{'-' * ticker_w}  {'-' * verdict_w}  {'-' * len('Levels')}",
    ]
    lines.extend(f"{t.ljust(ticker_w)}  {v.ljust(verdict_w)}  {lv}" for t, v, lv in rows)
    return "```\n" + "\n".join(lines) + "\n```"


def _summarize(day: dict) -> str:
    """The human summary of a stored plan: budget + cash + positions + a
    terse Buy/Sell/Hold/Not-worth table. Ask ``/chat`` for the reasoning."""
    mode = mode_label(day.get("trading_env") or "SIMULATE")
    lines = [f"📊 Auto scan — {mode} — {day.get('date')}"]
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
        lines.append(verdict_table(plan))
        if any("?" in verdict(e) for e in plan):
            lines.append("`?` = not set yet — needs a budget, a live price, or your approval.")
        lines.append("`/chat <ticker> ...` for the reasoning behind any call.")
    return "\n".join(lines)


def run_auto(
    config: ExecutionConfig,
    *,
    budget: float | None = None,
    trading_env: str | None = None,
    audit: str | None = None,
    on_step=None,
    stop_check=None,
    quiet: bool = False,
    approve=None,
) -> str:
    """Run the whole auto loop once and return a one-message summary.

    ``budget`` is the dollars of *new* money to deploy today (``None`` means
    manage-only: sell/trim/hold, no new buys). ``trading_env`` overrides
    ``config.trading_env`` (the serve bot lets the user switch paper/live at
    runtime). ``on_step(label)`` receives progress; ``stop_check() -> bool``
    aborts the loop at the next boundary when True. ``audit`` is the audit-log
    path (skipped when None).

    ``approve`` matters only on ``trading_env == "REAL"``: a sync callable
    ``approve(ticker, side, quantity, order_type, limit_price) -> bool`` asked
    before *every* trade /auto wants to place, mirroring "timeout = reject"
    everywhere else in this agent. On REAL with no ``approve`` given, every
    trade is treated as unapproved (fails safe — never silently trades real
    money unsupervised); on SIMULATE, ``approve`` is never called and every
    decided trade places immediately, exactly as before.

    Progress is deliberately terse: one line per ticker, never the pipeline's
    per-phase chatter (a 6-ticker scan would otherwise send ~50 messages).
    ``quiet=True`` (the scheduled daily run) suppresses even the per-ticker
    lines — only *serious* events reach the phone: orders placed, stop-losses,
    cancellations, errors, REAL approval prompts, and the final summary.
    """
    trading_env = trading_env or config.trading_env

    def _progress(label: str) -> None:
        """Routine progress — dropped entirely in quiet (scheduled) mode."""
        if not quiet and on_step is not None:
            on_step(label)

    def _serious(label: str) -> None:
        """Orders, stops, errors — always worth a message, even when quiet."""
        if on_step is not None:
            on_step(label)

    today = date.today().isoformat()

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

    # Universe = live watchlist ∪ current holdings, deduped, order preserved.
    # The watchlist is read fresh from its store (editable via /watchlist), so
    # an addition takes effect on the very next run, no restart needed.
    full_universe: list[str] = []
    seen: set[str] = set()
    for ticker in watchlist.load(config) + [p.ticker for p in book.positions]:
        key = ticker.upper()
        if key not in seen:
            seen.add(key)
            full_universe.append(ticker)

    # Refer-back: same-day research is reused, never re-paid for — but every
    # held ticker still goes through Pass 2/3 (decide + bracket-ensure) on
    # every run, cheaply, via the same-day cache inside the loop below. Only a
    # ticker that *errored* today is held back entirely — retrying a failing
    # pipeline on every same-day /auto would burn tokens for the same failure;
    # a fresh day retries it naturally.
    stored = auto_plan.load_plan()
    carried_plan: list[dict] = []
    if isinstance(stored, dict) and stored.get("date") == today:
        failed_today = {
            str(e.get("ticker") or "").upper()
            for e in (stored.get("plan") or [])
            if e.get("reason") == "error"
        }
        to_research = [t for t in full_universe if t.upper() not in failed_today]
        carried_plan = [
            e for e in (stored.get("plan") or [])
            if str(e.get("ticker") or "").upper() in failed_today
        ]
        if not to_research:
            return "📚 Already ran /auto today — every ticker already failed once (watchlist unchanged):\n\n" + _summarize(stored)
    else:
        to_research = full_universe

    plan_entries: list[dict] = carried_plan
    graph = TradingAgentsGraph(debug=False, config=DEFAULT_CONFIG.copy())

    def _stop_and_save() -> str:
        persist.save_auto_researches([c["decision"] for c in candidates])
        day = _make_day(today, acct, book, full_universe, plan_entries, budget, trading_env)
        auto_plan.save_plan(day)
        return "🛑 Stopped during /auto — partial plan saved, no further orders.\n\n" + _summarize(day)

    # Pass 1 — research every ticker first, so the budget can be split by
    # conviction across *all* the buy ideas before any order goes out.
    candidates: list[dict] = []
    for index, ticker in enumerate(to_research, start=1):
        if stop_check is not None and stop_check():
            return _stop_and_save()

        # Reuse a same-day result from the interactive /research flow (it
        # persists every run to the shared history) instead of re-paying for a
        # full pipeline run. The verdict is reused; the live quote is still
        # fetched below so sizing reflects the current price.
        decision = persist.load_today_decision(normalize_symbol(ticker))
        if decision is None:
            _progress(f"🔎 Auto: researching {ticker}… ({index}/{len(to_research)})")
            try:
                # on_step deliberately not forwarded: the pipeline's per-phase
                # chatter times every universe ticker floods the chat.
                decision = run_decision_streaming(
                    graph,
                    ticker,
                    today,
                    config=config,
                    portfolio=book,
                    on_step=None,
                    stop_check=stop_check,
                )
            except ValueError as exc:  # bad ticker (crypto/forex/unknown)
                plan_entries.append({"ticker": ticker, "side": None, "reason": "error", "signal": str(exc)})
                _serious(f"❌ Auto: {ticker} skipped — {exc}")
                if audit:
                    state.append_event(audit, {"event": "auto_error", "ticker": ticker, "message": str(exc)})
                continue
            except Exception as exc:  # noqa: BLE001 — one bad run must not kill the loop
                plan_entries.append({"ticker": ticker, "side": None, "reason": "error", "signal": str(exc)})
                _serious(f"❌ Auto: {ticker} research failed — {exc}")
                if audit:
                    state.append_event(audit, {"event": "auto_error", "ticker": ticker, "message": str(exc)})
                continue
            if decision is None:  # cancelled mid-run by a stop
                return _stop_and_save()
        else:
            _progress(f"📚 Auto: reusing today's research on {decision.yahoo}")

        prices = parse_prices(decision.research)
        # A live quote is normally only needed for an actionable side — but a
        # Hold with an entry price also needs one, to check the pullback is
        # genuinely below the live price, when the user opted into that.
        wants_live = decision.side is not None or (
            config.auto_buy_hold_pullback
            and decision.signal == "Hold"
            and prices.get("entry_price") is not None
        )
        live = _live_price(broker, decision.moomoo) if wants_live else None
        horizon = parse_time_horizon(decision.research)
        candidates.append({"decision": decision, "prices": prices, "live": live, "horizon": horizon})

    # Share the day's research with /chat (and future /auto reuse) as soon as it
    # exists, so an error later in the loop can't lose it.
    persist.save_auto_researches([c["decision"] for c in candidates])

    # Pass 2 — split the budget across buy candidates by conviction, then decide.
    buys = [
        (c["decision"].yahoo, conviction_weight(c["decision"].signal))
        for c in candidates
        if c["decision"].side == "BUY"
    ]
    pullback_tickers: set[str] = set()
    if config.auto_buy_hold_pullback:
        for c in candidates:
            if is_hold_pullback_candidate(c["decision"], c["prices"], book, c["live"]):
                buys.append((c["decision"].yahoo, HOLD_PULLBACK_WEIGHT))
                pullback_tickers.add(c["decision"].yahoo)
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
                _serious(f"🧹 Auto: cancelled {cancelled} stale order(s) on {decision.yahoo}")
                if audit:
                    state.append_event(audit, {"event": "auto_cancel", "ticker": decision.yahoo, "count": cancelled})
        except Exception as exc:  # noqa: BLE001 — cancel is best-effort; place today's order regardless
            if audit:
                state.append_event(audit, {"event": "auto_error", "ticker": decision.yahoo, "message": f"cancel stale: {exc}"})

        is_pullback = decision.yahoo in pullback_tickers
        slice_ = notional.get(decision.yahoo) if (decision.side == "BUY" or is_pullback) else None
        if is_pullback:
            action = hold_pullback_action(
                prices, c["live"], slice_, time_in_force_for(c["horizon"]),
            )
        else:
            action = decide_action(
                decision, prices, book, config, c["live"],
                notional=slice_, time_in_force=time_in_force_for(c["horizon"]),
            )

        # REAL money: every trade needs an explicit tap before it's placed —
        # /auto's whole point is to decide *without* approval, which is only
        # safe to let loose on paper. No approve callable given on REAL fails
        # safe (treated as rejected), never as a silent approval.
        if trading_env == "REAL" and action["reason"] == "ok" and action["quantity"] > 0:
            approved = bool(
                approve
                and approve(
                    decision.yahoo, action["side"], action["quantity"],
                    action["order_type"], action["limit_price"],
                )
            )
            if not approved:
                action = {**action, "reason": "not_approved"}

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

        # Pass 3 — place the order (limit/market).
        executed = None
        if action["reason"] == "ok" and action["quantity"] > 0:
            try:
                result = broker.place_order(
                    decision.moomoo, action["side"], action["quantity"],
                    price=action["limit_price"], time_in_force=action["time_in_force"],
                )
                executed = {"order_id": result["order_id"], "order_status": result["order_status"]}
                _serious(f"✅ Auto: {action['side']} {decision.yahoo} — order {executed['order_id']}")
                if audit:
                    state.append_event(
                        audit,
                        {"event": "order", "ticker": decision.yahoo, "side": action["side"], **result},
                    )
            except Exception as exc:  # noqa: BLE001 — a rejected order is recorded, not fatal
                executed = None
                action["reason"] = "error"
                if audit:
                    state.append_event(audit, {"event": "auto_error", "ticker": decision.yahoo, "message": str(exc)})

        # Pass 4 — the protective bracket (stop-loss + take-profit). Runs for
        # EVERY candidate, not just ones bought this run, so an existing
        # holding (bought via /execute, or from before this existed) gets
        # covered too — this is deliberate, not a side effect. Never gated by
        # approval: it only manages risk on a position already held, never
        # takes on new exposure, same reasoning as the old single-case stop.
        new_qty = position_qty_after(book, decision.yahoo, action, executed)
        bracket = {"stop_order_id": None, "target_order_id": None, "quantity": new_qty,
                   "stop_price": None, "target_price": None}
        try:
            for existing in broker.list_orders(decision.moomoo, open_only=True):
                if existing["order_type"] == "STOP" and existing["side"] == "SELL":
                    broker.cancel_order(existing["order_id"])
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup; still try to place fresh orders
            if audit:
                state.append_event(audit, {"event": "auto_error", "ticker": decision.yahoo, "message": f"bracket cleanup: {exc}"})

        plan = plan_bracket(new_qty, prices)
        if plan["stop"] is not None:
            try:
                stop_result = broker.place_order(
                    decision.moomoo, "SELL", plan["stop"]["quantity"],
                    order_type="STOP", price=plan["stop"]["price"], time_in_force="GTC",
                )
                bracket["stop_order_id"] = stop_result["order_id"]
                bracket["stop_price"] = plan["stop"]["price"]
                _serious(f"🛡️ Auto: stop-loss on {decision.yahoo} @ {plan['stop']['price']:g} — order {stop_result['order_id']}")
                if audit:
                    state.append_event(
                        audit,
                        {"event": "order", "ticker": decision.yahoo, "side": "SELL",
                         "order_type": "STOP", **stop_result},
                    )
            except Exception as exc:  # noqa: BLE001 — stop rejected; record, don't fail the entry
                if audit:
                    state.append_event(audit, {"event": "auto_error", "ticker": decision.yahoo, "message": f"stop-loss: {exc}"})
        if plan["target"] is not None:
            try:
                target_result = broker.place_order(
                    decision.moomoo, "SELL", plan["target"]["quantity"],
                    price=plan["target"]["price"], time_in_force="GTC",
                )
                bracket["target_order_id"] = target_result["order_id"]
                bracket["target_price"] = plan["target"]["price"]
                _serious(f"🎯 Auto: take-profit on {decision.yahoo} @ {plan['target']['price']:g} — order {target_result['order_id']}")
                if audit:
                    state.append_event(
                        audit,
                        {"event": "order", "ticker": decision.yahoo, "side": "SELL",
                         "order_type": "NORMAL", **target_result},
                    )
            except Exception as exc:  # noqa: BLE001 — target rejected; record, don't fail the entry
                if audit:
                    state.append_event(audit, {"event": "auto_error", "ticker": decision.yahoo, "message": f"take-profit: {exc}"})

        plan_entries.append(
            {
                "ticker": decision.yahoo,
                "side": action["side"],
                "signal": decision.signal,
                "quantity": action["quantity"],
                "order_type": action["order_type"],
                "limit_price": action["limit_price"],
                "notional": slice_ if (decision.side == "BUY" or is_pullback) else None,
                "weight": (
                    HOLD_PULLBACK_WEIGHT if is_pullback
                    else conviction_weight(decision.signal) if decision.side == "BUY"
                    else None
                ),
                "pullback": is_pullback,
                "entry_price": prices["entry_price"],
                "stop_loss": prices["stop_loss"],
                "price_target": prices["price_target"],
                "time_horizon": c["horizon"],
                "time_in_force": action.get("time_in_force"),
                "summary": decision.summary,
                "executed": executed,
                "bracket": bracket,
                "reason": action["reason"],
            }
        )

    day = _make_day(today, acct, book, full_universe, plan_entries, budget, trading_env)
    auto_plan.save_plan(day)
    return _summarize(day)


def _make_day(
    today: str, acct: dict, book: PortfolioContext, universe: list[str],
    plan_entries: list[dict], budget: float | None, trading_env: str = "SIMULATE",
) -> dict:
    """Assemble the persisted day document."""
    return {
        "date": today,
        "trading_env": trading_env,
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
