"""Turn a TradingAgents decision into a concrete, human-approvable order.

The research graph returns a string rating (Buy / Overweight / Hold /
Underweight / Sell / REVIEW) plus prose reasoning. This module maps that rating
to a BUY/SELL side, computes a quantity from config, and returns a frozen
:class:`ProposedOrder` — never a broker call. Nothing here reaches Moomoo or
Telegram, so it is pure and unit-testable with the network blocked.

Direction comes from the reliable rating string; quantity is synthesized by
config. The LLM's prose (``final_trade_decision``) is kept only as a human-
readable summary for the approval message, never parsed for numbers.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from execution_agent.symbols import to_moomoo
from tradingagents.agents.rating import is_review
from tradingagents.dataflows.symbols import normalize_symbol

# Default rating -> side. Hold and REVIEW are intentionally absent: a Hold is a
# "do nothing" call, and REVIEW means the decision is unreadable — both produce
# no order. Override per-rating via ExecutionConfig.side_mapping (JSON).
DEFAULT_SIDE_MAPPING: dict[str, str] = {
    "Buy": "BUY",
    "Overweight": "BUY",
    "Sell": "SELL",
    "Underweight": "SELL",
}

_BUY = "BUY"
_SELL = "SELL"


@dataclass(frozen=True)
class ProposedOrder:
    """A concrete order ready for human approval (not yet placed)."""

    symbol_yahoo: str
    symbol_moomoo: str
    side: str  # "BUY" | "SELL"
    quantity: float
    order_type: str  # "MARKET"
    rating: str
    summary: str  # the PM's prose decision, for the Telegram message

    def is_buy(self) -> bool:
        return self.side == _BUY


def side_for(rating: str, side_mapping: dict[str, str] | None = None) -> str | None:
    """Map a rating to BUY/SELL, or None for Hold / REVIEW / unknown."""
    mapping = side_mapping if side_mapping is not None else DEFAULT_SIDE_MAPPING
    if is_review(rating):
        return None
    return mapping.get(rating)


def quantity_for(
    side: str,
    *,
    sizing_mode: str,
    sizing_amount: float,
    cash: float | None = None,
    position_quantity: float | None = None,
    last_price: float | None = None,
) -> float:
    """Quantity in whole units, floored. Raises when a required input is missing.

    - ``notional``: ``floor(amount / last_price)`` — needs ``last_price``.
    - ``shares``: ``amount``.
    - ``percent_of_cash``: ``amount%`` of ``cash``, then ``/ last_price``.

    A SELL is capped at ``position_quantity`` when a position is known (so it
    never oversells); ``position_quantity=None`` means "unknown, don't cap".
    """
    if sizing_mode == "shares":
        qty = float(sizing_amount)
    elif sizing_mode == "notional":
        if not last_price:
            raise ValueError("notional sizing needs a last price, but none was provided")
        qty = sizing_amount / last_price
    elif sizing_mode == "percent_of_cash":
        if cash is None:
            raise ValueError("percent_of_cash sizing needs cash, but none was provided")
        if not last_price:
            raise ValueError("percent_of_cash sizing needs a last price, but none was provided")
        qty = (cash * sizing_amount / 100.0) / last_price
    else:
        raise ValueError(f"unknown sizing_mode {sizing_mode!r}")

    if side == _SELL and position_quantity is not None:
        qty = min(qty, position_quantity)
    return float(math.floor(qty))


def summary_from(final_state: dict) -> str:
    """The prose decision to show the human, preferring the PM's final call."""
    for key in ("final_trade_decision", "trader_investment_plan", "investment_plan"):
        text = final_state.get(key)
        if text:
            return str(text).strip()
    return ""


def build_order(
    *,
    signal: str,
    summary: str,
    yahoo: str,
    moomoo: str,
    config,
    cash: float | None = None,
    position_quantity: float | None = None,
    last_price: float | None = None,
) -> ProposedOrder | None:
    """Map a decision's signal to a ProposedOrder, or None when not actionable.

    None for Hold / REVIEW (no side) and for a degenerate quantity (nothing to
    order — e.g. amount too small for one unit, or nothing held to sell). Raises
    ``ValueError`` when a required input (price / cash) is missing.
    """
    side = side_for(signal, config.parsed_side_mapping())
    if side is None:
        return None

    quantity = quantity_for(
        side,
        sizing_mode=config.sizing_mode,
        sizing_amount=config.sizing_amount,
        cash=cash,
        position_quantity=position_quantity,
        last_price=last_price,
    )
    if quantity <= 0:
        return None

    return ProposedOrder(
        symbol_yahoo=yahoo,
        symbol_moomoo=moomoo,
        side=side,
        quantity=quantity,
        order_type="MARKET",
        rating=signal,
        summary=summary,
    )


def run_decision(
    graph,
    ticker: str,
    date: str,
    *,
    config,
    portfolio=None,
    price_provider: Callable[[str], float] | None = None,
) -> tuple[dict, ProposedOrder | None]:
    """Run the graph for ``ticker`` on ``date`` and translate to a ProposedOrder.

    ``graph`` is a ``TradingAgentsGraph`` instance (injected so unit tests can
    substitute a fake). ``price_provider`` is ``callable(symbol_moomoo) -> float``,
    used only by notional / percent_of_cash sizing. Returns ``(final_state,
    order)``; ``order`` is None for Hold / REVIEW or a non-actionable quantity.
    """
    yahoo = normalize_symbol(ticker)
    moomoo = to_moomoo(ticker)  # rejects crypto/forex/futures/indices before any LLM call

    final_state, signal = graph.propagate(yahoo, date, portfolio=portfolio)

    cash = portfolio.cash if portfolio is not None else None
    position_quantity = None
    if portfolio is not None:
        position = portfolio.position_in(yahoo)
        if position is not None:
            position_quantity = position.quantity

    last_price = None
    if config.sizing_mode != "shares":
        if price_provider is None:
            raise ValueError("sizing needs a price_provider, but none was provided")
        last_price = price_provider(moomoo)

    order = build_order(
        signal=signal,
        summary=summary_from(final_state),
        yahoo=yahoo,
        moomoo=moomoo,
        config=config,
        cash=cash,
        position_quantity=position_quantity,
        last_price=last_price,
    )
    return final_state, order
