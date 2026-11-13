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
import re
from dataclasses import dataclass

from execution_agent.symbols import to_moomoo
from tradingagents.agents.rating import is_review, run_rating
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


# A number anywhere in the reply, e.g. "500", "$500", "10 shares". The optional
# sign is captured so a negative amount is parsed and then rejected below.
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
# Unit markers in the reply. "shares" wins over "dollars" so "500 shares usd"
# never misreads; a bare number carries no unit and the caller asks for it.
_SHARE_RE = re.compile(r"\bshares?\b|\bshs\b|\bsh\b")
_DOLLAR_RE = re.compile(r"\$|\busd\b|\bdollars?\b|\bbucks?\b")


def parse_amount(text: str) -> tuple[float, str | None]:
    """Parse a user's "how much?" reply into ``(number, unit)``.

    ``unit`` is ``"shares"`` or ``"usd"`` when the reply says which, else
    ``None`` (a bare number — the caller then asks "dollars or shares?").
    Accepts "500", "$500", "500 usd", "10 shares", "10 sh". Raises
    ``ValueError`` when there is no positive number to read.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("amount must be a non-empty string")
    s = text.strip().lower()

    match = _NUMBER_RE.search(s)
    if match is None:
        raise ValueError(f"no number found in {text!r}")
    number = float(match.group())
    if number <= 0:
        raise ValueError(f"amount must be positive, got {text!r}")

    if _SHARE_RE.search(s):
        unit = "shares"
    elif _DOLLAR_RE.search(s):
        unit = "usd"
    else:
        unit = None
    return number, unit


def summary_from(final_state: dict) -> str:
    """The prose decision to show the human, preferring the PM's final call."""
    for key in ("final_trade_decision", "trader_investment_plan", "investment_plan"):
        text = final_state.get(key)
        if text:
            return str(text).strip()
    return ""


@dataclass(frozen=True)
class DecisionResult:
    """A run's outcome: direction and symbols, before any quantity is set.

    ``side`` is "BUY"/"SELL" or None (Hold / REVIEW). Quantity is chosen later,
    in the Telegram chat, so it is deliberately not part of this result.
    ``research`` carries the full final state of the run (all analyst reports,
    the bull/bear debate, the trader and risk debates, the plans and the final
    decision) so the serve bot can answer follow-up questions about it — the
    one-line ``summary`` alone would throw most of that away.
    """

    yahoo: str
    moomoo: str
    signal: str
    side: str | None
    summary: str
    cash: float | None
    position_quantity: float | None
    research: dict | None = None


def to_order(
    decision: DecisionResult, quantity: float, side: str | None = None
) -> ProposedOrder | None:
    """Assemble a ProposedOrder for a decision and a whole-unit quantity.

    ``side`` overrides ``decision.side`` (the human may flip the AI's call in
    chat); it defaults to the decision's own side when not given. None when the
    quantity is not actionable (zero/negative) or there is no side.
    """
    side = side or decision.side
    if side is None or quantity <= 0:
        return None
    return ProposedOrder(
        symbol_yahoo=decision.yahoo,
        symbol_moomoo=decision.moomoo,
        side=side,
        quantity=quantity,
        order_type="MARKET",
        rating=decision.signal,
        summary=decision.summary,
    )


def run_decision(
    graph,
    ticker: str,
    date: str,
    *,
    config,
    portfolio=None,
) -> DecisionResult:
    """Run the graph for ``ticker`` on ``date`` and return its decision context.

    ``graph`` is a ``TradingAgentsGraph`` instance (injected so unit tests can
    substitute a fake). Quantity is not computed here — it is chosen by the user
    in Telegram — so this stays free of any broker/price dependency.
    """
    yahoo = normalize_symbol(ticker)
    moomoo = to_moomoo(ticker)  # rejects crypto/forex/futures/indices before any LLM call

    final_state, signal = graph.propagate(yahoo, date, portfolio=portfolio)

    return _decision_from(yahoo, moomoo, signal, final_state, config, portfolio)


# State keys that signal a research phase finished, in the order they complete.
# The serve bot turns each into a one-line Telegram update so a run that takes
# minutes doesn't look frozen. The debate/risk states are handled separately
# below: they run several rounds, so they report each round rather than one
# premature "done" after the first argument.
MILESTONES: tuple[tuple[str, str], ...] = (
    ("market_report", "📊 Market analysis done"),
    ("sentiment_report", "💬 Sentiment analysis done"),
    ("news_report", "📰 News analysis done"),
    ("fundamentals_report", "🏦 Fundamentals analysis done"),
    ("trader_investment_plan", "🎯 Trader's plan done"),
    ("final_trade_decision", "✅ Final decision reached"),
)

# Speakers per round in each debate: the investment debate alternates bull and
# bear (2), the risk review cycles aggressive / conservative / neutral (3).
_DEBATE_SPEAKERS = 2
_RISK_SPEAKERS = 3


def _phase_complete(key: str, state: dict) -> bool:
    """Whether ``state`` marks phase ``key`` as finished (non-empty)."""
    value = state.get(key)
    if not value:
        return False
    if isinstance(value, dict):
        # Debate/risk states are dicts; done once any field has real content.
        return any(isinstance(v, str) and v.strip() for v in value.values())
    return isinstance(value, str) and bool(value.strip())


def _debate_round(state: dict, key: str, speakers_per_round: int) -> int | None:
    """The 1-based round a debate state is on, or None before it starts.

    ``count`` is the number of speaker turns finished so far; one round spans
    ``speakers_per_round`` turns. Returns None when the debate hasn't begun, so
    callers stay silent (and never emit a "round 0").
    """
    value = state.get(key)
    if not isinstance(value, dict):
        return None
    count = value.get("count")
    if not isinstance(count, int) or count <= 0:
        return None
    return (count + speakers_per_round - 1) // speakers_per_round


def _decision_from(yahoo, moomoo, signal, final_state, config, portfolio) -> DecisionResult:
    cash = portfolio.cash if portfolio is not None else None
    position_quantity = None
    if portfolio is not None:
        position = portfolio.position_in(yahoo)
        if position is not None:
            position_quantity = position.quantity

    return DecisionResult(
        yahoo=yahoo,
        moomoo=moomoo,
        signal=signal,
        side=side_for(signal, config.parsed_side_mapping()),
        summary=summary_from(final_state),
        cash=cash,
        position_quantity=position_quantity,
        research=final_state,
    )


def run_decision_streaming(
    graph,
    ticker: str,
    date: str,
    *,
    config,
    portfolio=None,
    on_step=None,
) -> DecisionResult:
    """Like :func:`run_decision`, but stream the graph and call ``on_step(label)``
    as each phase finishes. ``on_step`` is synchronous and called from the same
    thread that runs the graph — the caller bridges it to Telegram if needed.
    """
    yahoo = normalize_symbol(ticker)
    moomoo = to_moomoo(ticker)  # rejects crypto/forex/futures/indices before any LLM call

    init_state = graph.create_run_state(yahoo, date, "stock", portfolio)
    graph_input = graph.checkpoint_input(init_state)
    args = graph.propagator.get_graph_args()

    final_state: dict = {}
    seen: set[str] = set()
    cfg = getattr(graph, "config", {}) or {}
    max_debate = int(cfg.get("max_debate_rounds") or 1)
    max_risk = int(cfg.get("max_risk_discuss_rounds") or 1)
    last_debate_round = 0
    last_risk_round = 0
    for _, chunk in graph.stream_run(graph_input, **args):
        if chunk is None:
            continue
        final_state.update(chunk)
        for key, label in MILESTONES:
            if key not in seen and _phase_complete(key, chunk):
                seen.add(key)
                if on_step is not None:
                    on_step(label)
        # The debates run several rounds; report each one so the minutes between
        # "market" and "trader" don't look frozen.
        round_ = _debate_round(chunk, "investment_debate_state", _DEBATE_SPEAKERS)
        if round_ is not None and round_ > last_debate_round:
            last_debate_round = round_
            if on_step is not None:
                on_step(f"⚖️ Bull/bear debate — round {round_}/{max_debate}")
        round_ = _debate_round(chunk, "risk_debate_state", _RISK_SPEAKERS)
        if round_ is not None and round_ > last_risk_round:
            last_risk_round = round_
            if on_step is not None:
                on_step(f"🛡️ Risk review — round {round_}/{max_risk}")

    graph.record_decision(yahoo, date, final_state)
    graph.clear_checkpoint_on_success(yahoo, date, "stock", portfolio)

    signal = run_rating(final_state)
    return _decision_from(yahoo, moomoo, signal, final_state, config, portfolio)
