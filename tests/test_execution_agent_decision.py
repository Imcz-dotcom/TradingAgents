"""Decision -> proposed order: rating-to-side, sizing, and the graph wrapper."""

import pytest

from execution_agent.config import ExecutionConfig
from execution_agent.decision import (
    DecisionResult,
    parse_amount,
    quantity_for,
    run_decision,
    run_decision_streaming,
    side_for,
    to_order,
)
from tradingagents.portfolio import PortfolioContext, Position

# ---- rating -> side -------------------------------------------------------


@pytest.mark.parametrize(
    ("rating", "expected"),
    [
        ("Buy", "BUY"),
        ("Overweight", "BUY"),
        ("Sell", "SELL"),
        ("Underweight", "SELL"),
        ("Hold", None),
        ("REVIEW", None),
        ("garbage", None),
    ],
)
def test_side_for(rating, expected):
    assert side_for(rating) == expected


def test_side_for_custom_mapping():
    assert side_for("Buy", {"Buy": "SELL"}) == "SELL"


# ---- sizing ---------------------------------------------------------------


def test_quantity_shares():
    assert quantity_for("BUY", sizing_mode="shares", sizing_amount=10) == 10.0


def test_quantity_notional_floors():
    # floor(1000 / 45) = 22
    assert quantity_for("BUY", sizing_mode="notional", sizing_amount=1000, last_price=45) == 22.0


def test_quantity_percent_of_cash():
    # 10% of 10000 = 1000; floor(1000 / 45) = 22
    assert (
        quantity_for(
            "BUY", sizing_mode="percent_of_cash", sizing_amount=10, cash=10000, last_price=45
        )
        == 22.0
    )


def test_quantity_notional_needs_price():
    with pytest.raises(ValueError):
        quantity_for("BUY", sizing_mode="notional", sizing_amount=1000)


def test_quantity_percent_needs_cash():
    with pytest.raises(ValueError):
        quantity_for("BUY", sizing_mode="percent_of_cash", sizing_amount=10, last_price=45)


def test_quantity_sell_capped_by_position():
    # notional would be 22, but we only hold 5
    assert (
        quantity_for(
            "SELL", sizing_mode="notional", sizing_amount=1000, last_price=45, position_quantity=5
        )
        == 5.0
    )


def test_quantity_sell_uncapped_when_position_unknown():
    assert (
        quantity_for("SELL", sizing_mode="notional", sizing_amount=1000, last_price=45) == 22.0
    )


# ---- parse_amount (chat "how much?" replies) --------------------------------


@pytest.mark.parametrize(
    ("text", "number", "unit"),
    [
        ("500", 500.0, None),            # bare number -> ambiguous, caller asks
        ("$500", 500.0, "usd"),
        ("500 usd", 500.0, "usd"),
        ("500 USD", 500.0, "usd"),
        ("500.50 usd", 500.5, "usd"),
        ("500 dollars", 500.0, "usd"),
        ("10 shares", 10.0, "shares"),
        ("10 share", 10.0, "shares"),
        ("10 sh", 10.0, "shares"),
        ("10 shs", 10.0, "shares"),
        ("buy 20 shares please", 20.0, "shares"),
    ],
)
def test_parse_amount(text, number, unit):
    assert parse_amount(text) == (number, unit)


@pytest.mark.parametrize("text", ["", "   ", "no number", "zero", "0", "-5"])
def test_parse_amount_rejects(text):
    with pytest.raises(ValueError):
        parse_amount(text)


# ---- to_order --------------------------------------------------------------


def _decision(side="BUY"):
    return DecisionResult(
        yahoo="NVDA",
        moomoo="US.NVDA",
        signal="Buy",
        side=side,
        summary="Strong momentum.",
        cash=None,
        position_quantity=None,
    )


def test_to_order_builds_market_order():
    order = to_order(_decision(), 10.0)
    assert order is not None
    assert order.side == "BUY"
    assert order.quantity == 10.0
    assert order.order_type == "MARKET"
    assert order.symbol_yahoo == "NVDA"
    assert order.symbol_moomoo == "US.NVDA"


def test_to_order_zero_quantity_is_none():
    assert to_order(_decision(), 0.0) is None


def test_to_order_no_side_is_none():
    assert to_order(_decision(side=None), 10.0) is None


# ---- run_decision (graph wrapper) ------------------------------------------


class FakeGraph:
    def __init__(self, signal, state=None):
        self.signal = signal
        self.state = state or {"final_trade_decision": "Buy: strong momentum."}
        self.calls = []

    def propagate(self, ticker, date, portfolio=None):
        self.calls.append((ticker, date, portfolio))
        return self.state, self.signal


def test_run_decision_buy():
    graph = FakeGraph("Buy")
    decision = run_decision(graph, "NVDA", "2026-11-12", config=ExecutionConfig())

    assert graph.calls == [("NVDA", "2026-11-12", None)]
    assert decision.side == "BUY"
    assert decision.yahoo == "NVDA"
    assert decision.moomoo == "US.NVDA"
    assert "Buy" in decision.summary
    assert decision.cash is None
    assert decision.position_quantity is None


def test_run_decision_hold_has_no_side():
    decision = run_decision(FakeGraph("Hold"), "NVDA", "2026-11-12", config=ExecutionConfig())
    assert decision.side is None


def test_run_decision_review_has_no_side():
    decision = run_decision(FakeGraph("REVIEW"), "NVDA", "2026-11-12", config=ExecutionConfig())
    assert decision.side is None


def test_run_decision_portfolio_supplies_cash_and_position():
    graph = FakeGraph("Sell")
    portfolio = PortfolioContext(
        cash=10000.0,
        positions=[Position(ticker="NVDA", quantity=5, average_price=40.0)],
    )

    decision = run_decision(
        graph, "NVDA", "2026-11-12", config=ExecutionConfig(), portfolio=portfolio
    )

    assert decision.side == "SELL"
    assert decision.cash == 10000.0
    assert decision.position_quantity == 5.0


def test_run_decision_rejects_crypto_before_running():
    graph = FakeGraph("Buy")
    with pytest.raises(ValueError):
        run_decision(graph, "BTC-USD", "2026-11-12", config=ExecutionConfig())
    assert graph.calls == []  # never reached the LLM


# ---- run_decision_streaming (live progress) --------------------------------


class FakeStreamingGraph:
    """A graph that yields fixed ``(messages, chunk)`` steps from stream_run."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def create_run_state(self, ticker, date, asset_type="stock", portfolio=None):
        return {}

    def checkpoint_input(self, init_state):
        return init_state

    class _Prop:
        def get_graph_args(self):
            return {}

    propagator = _Prop()

    def stream_run(self, graph_input, **args):
        yield from self._chunks

    def record_decision(self, ticker, date, final_state):
        pass

    def clear_checkpoint_on_success(self, ticker, date, asset_type="stock", portfolio=None):
        pass


def test_run_decision_streaming_emits_each_phase_once():
    graph = FakeStreamingGraph([
        ([], {"market_report": "Market is up."}),
        ([], {"sentiment_report": "Bullish."}),
        ([], {"final_trade_decision": "Rating: Buy", "final_rating": "Buy"}),
    ])
    labels = []

    decision = run_decision_streaming(
        graph, "NVDA", "2026-11-12", config=ExecutionConfig(), on_step=labels.append
    )

    assert labels == [
        "📊 Market analysis done",
        "💬 Sentiment analysis done",
        "✅ Final decision reached",
    ]
    assert decision.side == "BUY"
    assert decision.yahoo == "NVDA"
    assert decision.moomoo == "US.NVDA"


def test_run_decision_streaming_reports_debate_and_risk_rounds():
    graph = FakeStreamingGraph([
        ([], {"investment_debate_state": {"count": 1}}),
        ([], {"investment_debate_state": {"count": 3}}),
        ([], {"investment_debate_state": {"count": 5}}),
        ([], {"trader_investment_plan": "Plan: hold through volatility."}),
        ([], {"risk_debate_state": {"count": 3}}),
        ([], {"risk_debate_state": {"count": 6}}),
        ([], {"risk_debate_state": {"count": 9}}),
        ([], {"final_trade_decision": "Rating: Buy", "final_rating": "Buy"}),
    ])
    graph.config = {"max_debate_rounds": 3, "max_risk_discuss_rounds": 3}
    labels = []

    decision = run_decision_streaming(
        graph, "NVDA", "2026-11-12", config=ExecutionConfig(), on_step=labels.append
    )

    assert labels == [
        "⚖️ Bull/bear debate — round 1/3",
        "⚖️ Bull/bear debate — round 2/3",
        "⚖️ Bull/bear debate — round 3/3",
        "🎯 Trader's plan done",
        "🛡️ Risk review — round 1/3",
        "🛡️ Risk review — round 2/3",
        "🛡️ Risk review — round 3/3",
        "✅ Final decision reached",
    ]
    assert decision.side == "BUY"


def test_run_decision_streaming_skips_empty_debate_state():
    graph = FakeStreamingGraph([
        ([], {"investment_debate_state": {"bull_history": "", "bear_history": ""}}),
        ([], {"final_trade_decision": "Rating: Hold", "final_rating": "Hold"}),
    ])
    labels = []

    decision = run_decision_streaming(
        graph, "NVDA", "2026-11-12", config=ExecutionConfig(), on_step=labels.append
    )

    assert labels == ["✅ Final decision reached"]
    assert decision.side is None  # Hold has no side


def test_run_decision_streaming_stops_immediately_when_checked():
    graph = FakeStreamingGraph([
        ([], {"market_report": "Market is up."}),
        ([], {"final_trade_decision": "Rating: Buy", "final_rating": "Buy"}),
    ])

    decision = run_decision_streaming(
        graph, "NVDA", "2026-11-12", config=ExecutionConfig(), stop_check=lambda: True
    )

    assert decision is None  # aborted before the first chunk was processed


def test_run_decision_streaming_stops_at_next_phase_boundary():
    graph = FakeStreamingGraph([
        ([], {"market_report": "Market is up."}),
        ([], {"sentiment_report": "Bullish."}),
        ([], {"final_trade_decision": "Rating: Buy", "final_rating": "Buy"}),
    ])
    calls = {"n": 0}

    def stop_after_first():
        calls["n"] += 1
        return calls["n"] > 1  # let the first chunk through, then stop

    decision = run_decision_streaming(
        graph, "NVDA", "2026-11-12", config=ExecutionConfig(), stop_check=stop_after_first
    )

    assert decision is None  # stopped at the second phase boundary
