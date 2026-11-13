"""Decision -> proposed order: rating-to-side, sizing, and the graph wrapper."""

import pytest

from execution_agent.config import ExecutionConfig
from execution_agent.decision import (
    build_order,
    parse_amount,
    quantity_for,
    run_decision,
    side_for,
)

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


# ---- build_order ----------------------------------------------------------


def _cfg(**kw):
    defaults = {"sizing_mode": "shares", "sizing_amount": 10}
    defaults.update(kw)
    return ExecutionConfig(**defaults)


def test_build_order_buy():
    order = build_order(
        signal="Buy",
        summary="Strong momentum.",
        yahoo="NVDA",
        moomoo="US.NVDA",
        config=_cfg(),
    )
    assert order is not None
    assert order.side == "BUY"
    assert order.quantity == 10.0
    assert order.order_type == "MARKET"
    assert order.symbol_moomoo == "US.NVDA"


def test_build_order_hold_is_none():
    assert build_order(signal="Hold", summary="", yahoo="NVDA", moomoo="US.NVDA", config=_cfg()) is None


def test_build_order_review_is_none():
    assert build_order(signal="REVIEW", summary="", yahoo="NVDA", moomoo="US.NVDA", config=_cfg()) is None


def test_build_order_zero_quantity_is_none():
    # shares=0.4 floors to 0 -> nothing to order
    cfg = _cfg(sizing_mode="shares", sizing_amount=0.4)
    assert build_order(signal="Buy", summary="", yahoo="NVDA", moomoo="US.NVDA", config=cfg) is None


# ---- run_decision (graph wrapper) ------------------------------------------


class FakeGraph:
    def __init__(self, signal, state=None):
        self.signal = signal
        self.state = state or {"final_trade_decision": "Buy: strong momentum."}
        self.calls = []

    def propagate(self, ticker, date, portfolio=None):
        self.calls.append((ticker, date, portfolio))
        return self.state, self.signal


def test_run_decision_buy_with_shares_sizing():
    graph = FakeGraph("Buy")
    cfg = _cfg(sizing_mode="shares", sizing_amount=10)

    state, order = run_decision(graph, "NVDA", "2026-11-12", config=cfg)

    assert graph.calls == [("NVDA", "2026-11-12", None)]
    assert order is not None
    assert order.side == "BUY"
    assert order.quantity == 10.0
    assert order.symbol_yahoo == "NVDA"
    assert order.symbol_moomoo == "US.NVDA"
    assert "Buy" in order.summary


def test_run_decision_hold_is_none():
    graph = FakeGraph("Hold")
    cfg = _cfg()
    _, order = run_decision(graph, "NVDA", "2026-11-12", config=cfg)
    assert order is None


def test_run_decision_review_is_none():
    graph = FakeGraph("REVIEW")
    cfg = _cfg()
    _, order = run_decision(graph, "NVDA", "2026-11-12", config=cfg)
    assert order is None


def test_run_decision_notional_uses_price_provider():
    graph = FakeGraph("Buy")
    cfg = _cfg(sizing_mode="notional", sizing_amount=1000)
    prices = {}

    def price(symbol):
        prices[symbol] = 45.0
        return 45.0

    _, order = run_decision(graph, "NVDA", "2026-11-12", config=cfg, price_provider=price)

    assert order is not None
    assert order.quantity == 22.0
    assert set(prices) == {"US.NVDA"}  # price queried with the Moomoo symbol


def test_run_decision_notional_requires_price_provider():
    graph = FakeGraph("Buy")
    cfg = _cfg(sizing_mode="notional", sizing_amount=1000)
    with pytest.raises(ValueError):
        run_decision(graph, "NVDA", "2026-11-12", config=cfg)


def test_run_decision_rejects_crypto_before_running():
    graph = FakeGraph("Buy")
    cfg = _cfg()
    with pytest.raises(ValueError):
        run_decision(graph, "BTC-USD", "2026-11-12", config=cfg)
    assert graph.calls == []  # never reached the LLM


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
