"""The /auto loop: conviction sizing, budget allocation, and the full run."""

from datetime import date

import pytest

from execution_agent import auto, persist
from execution_agent.config import ExecutionConfig
from execution_agent.decision import DecisionResult
from tradingagents.portfolio import PortfolioContext, Position


# --- pure helpers ---------------------------------------------------------


def test_sane_limit_returns_in_band_level():
    assert auto.sane_limit(189.5, 190.0, 0.20) == 189.5


def test_sane_limit_rejects_out_of_band():
    assert auto.sane_limit(300.0, 190.0, 0.20) is None  # >20% away -> MARKET


def test_sane_limit_none_when_missing():
    assert auto.sane_limit(None, 190.0, 0.20) is None
    assert auto.sane_limit(189.5, None, 0.20) is None
    assert auto.sane_limit(189.5, 0.0, 0.20) is None


def test_conviction_weight_buy_outweighs_overweight():
    assert auto.conviction_weight("Buy") == 2.0
    assert auto.conviction_weight("Overweight") == 1.0


def test_conviction_weight_zero_for_non_buys():
    assert auto.conviction_weight("Hold") == 0.0
    assert auto.conviction_weight("Sell") == 0.0
    assert auto.conviction_weight("REVIEW") == 0.0


def test_trim_quantity_full_exit_for_sell():
    assert auto.trim_quantity("Sell", 5, 0.5) == 5.0


def test_trim_quantity_trims_for_underweight():
    assert auto.trim_quantity("Underweight", 5, 0.5) == 2.0  # floor(2.5)


def test_trim_quantity_floors_to_zero():
    assert auto.trim_quantity("Underweight", 1, 0.5) == 0.0


def test_allocate_budget_weighted_split():
    buys = [("NVDA", 2.0), ("AAPL", 1.0)]
    assert auto.allocate_budget(buys, 600.0) == {"NVDA": 400.0, "AAPL": 200.0}


def test_allocate_budget_empty_and_no_budget():
    assert auto.allocate_budget([], 600.0) == {}
    assert auto.allocate_budget([("NVDA", 2.0)], None) == {}
    assert auto.allocate_budget([("NVDA", 0.0)], 600.0) == {}


def _decision(yahoo="NVDA", signal="Buy", side="BUY", summary="buy it", research=None):
    return DecisionResult(
        yahoo=yahoo,
        moomoo=f"US.{yahoo}" if "." not in yahoo else yahoo,
        signal=signal,
        side=side,
        summary=summary,
        cash=10000.0,
        position_quantity=None,
        research=research or {},
    )


def _book(cash=10000.0, positions=None):
    return PortfolioContext(cash=cash, currency="USD", positions=positions or [])


def _prices(entry=189.5, stop=172.0, target=200.0):
    return {"entry_price": entry, "stop_loss": stop, "price_target": target}


# --- decide_action --------------------------------------------------------


def test_decide_action_buy_with_notional():
    action = auto.decide_action(
        _decision(), _prices(), _book(), ExecutionConfig(), live=190.0, notional=1000.0
    )
    assert action == {
        "side": "BUY",
        "quantity": 5.0,  # floor(1000 / 190)
        "order_type": "LIMIT",
        "limit_price": 189.5,
        "time_in_force": "DAY",  # default; the loop overrides via the PM's horizon
        "reason": "ok",
    }


def test_decide_action_buy_no_budget():
    action = auto.decide_action(
        _decision(), _prices(), _book(), ExecutionConfig(), live=190.0, notional=None
    )
    assert action["reason"] == "no_budget"
    assert action["quantity"] == 0.0


def test_decide_action_buy_no_price_when_no_live():
    action = auto.decide_action(
        _decision(), _prices(), _book(), ExecutionConfig(), live=None, notional=1000.0
    )
    assert action["reason"] == "no_price"
    assert action["quantity"] == 0.0


def test_decide_action_sell_full_exit():
    book = _book(positions=[Position(ticker="NVDA", quantity=5, average_price=100.0)])
    action = auto.decide_action(
        _decision(signal="Sell", side="SELL"), _prices(), book, ExecutionConfig(), live=190.0
    )
    assert action["side"] == "SELL"
    assert action["quantity"] == 5.0
    assert action["reason"] == "ok"


def test_decide_action_underweight_trims():
    book = _book(positions=[Position(ticker="NVDA", quantity=5, average_price=100.0)])
    action = auto.decide_action(
        _decision(signal="Underweight", side="SELL"), _prices(), book, ExecutionConfig(), live=190.0
    )
    assert action["side"] == "SELL"
    assert action["quantity"] == 2.0  # floor(5 * 0.5)
    assert action["reason"] == "ok"


def test_decide_action_underweight_trims_to_zero_skips():
    book = _book(positions=[Position(ticker="NVDA", quantity=1, average_price=100.0)])
    action = auto.decide_action(
        _decision(signal="Underweight", side="SELL"), _prices(), book, ExecutionConfig(), live=190.0
    )
    assert action["reason"] == "no_position"
    assert action["quantity"] == 0.0


def test_decide_action_sell_no_position():
    action = auto.decide_action(
        _decision(signal="Sell", side="SELL"), _prices(), _book(), ExecutionConfig(), live=190.0
    )
    assert action["reason"] == "no_position"
    assert action["quantity"] == 0.0


def test_decide_action_hold_and_review():
    hold = auto.decide_action(_decision(signal="Hold", side=None), _prices(), _book(), ExecutionConfig(), live=190.0)
    assert hold["reason"] == "hold"
    review = auto.decide_action(_decision(signal="REVIEW", side=None), _prices(), _book(), ExecutionConfig(), live=190.0)
    assert review["reason"] == "review"


# --- run_auto with a mocked broker + research -----------------------------


class _FakeBroker:
    def __init__(self, cash=10000.0, currency="USD", positions=None, last=190.0):
        self.cash = cash
        self.currency = currency
        self.positions = positions or []
        self.last = last
        self.orders = []
        self.cancelled = []  # symbols passed to cancel_open_orders

    def get_account(self):
        return {"cash": self.cash, "currency": self.currency}

    def get_positions(self):
        return self.positions

    def last_price(self, symbol):
        return self.last

    def place_order(self, symbol, side, quantity, *, price=None, order_type="MARKET", time_in_force="DAY"):
        # Mirror the real broker: a price implies NORMAL, unless STOP is explicit.
        if order_type != "STOP" and price is not None:
            order_type = "NORMAL"
        self.orders.append(
            {"symbol": symbol, "side": side, "quantity": quantity, "price": price,
             "order_type": order_type, "time_in_force": time_in_force}
        )
        return {"order_id": len(self.orders), "order_status": "SUBMITTED"}

    def cancel_open_orders(self, symbol):
        self.cancelled.append(symbol)
        return 0


class _DummyGraph:
    def __init__(self, debug=False, config=None):
        pass


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """Wire run_auto to a tmp plan path and record research calls."""
    monkeypatch.setattr(auto.auto_plan, "auto_plan_path", lambda: str(tmp_path / "plan.json"))
    # Isolate the same-day reuse cache: an empty history means "nothing reused",
    # so these tests exercise fresh research (the reuse path has its own test).
    monkeypatch.setattr(persist, "history_path", lambda: str(tmp_path / "history.json"))
    calls: list[str] = []

    def _apply(broker, decisions, prices):
        monkeypatch.setattr(auto, "MoomooBroker", lambda **kw: broker)
        monkeypatch.setattr(auto, "TradingAgentsGraph", _DummyGraph)
        monkeypatch.setattr(auto, "parse_prices", lambda research: prices)

        def _fake_stream(graph, ticker, today, *, config, portfolio=None, on_step=None, stop_check=None):
            calls.append(ticker)
            return decisions.get(ticker)

        monkeypatch.setattr(auto, "run_decision_streaming", _fake_stream)

    return _apply, calls


def test_run_auto_refuses_real_mode():
    result = auto.run_auto(ExecutionConfig(), trading_env="REAL")
    assert "paper-only" in result


def test_run_auto_reuses_same_day_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(auto.auto_plan, "auto_plan_path", lambda: str(tmp_path / "plan.json"))
    auto.auto_plan.save_plan(
        {"date": date.today().isoformat(), "cash": 5000.0, "currency": "USD",
         "positions": [], "universe": [], "plan": []}
    )
    monkeypatch.setattr(
        auto, "run_decision_streaming", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not research"))
    )

    result = auto.run_auto(ExecutionConfig())

    assert "reusing today's plan" in result


def test_run_auto_researches_stale_plan(patched, tmp_path):
    apply, calls = patched
    apply(_FakeBroker(), {"NVDA": _decision()}, _prices())
    # a plan from a past date must not block research
    auto.auto_plan.save_plan(
        {"date": "2000-01-01", "cash": 1.0, "currency": "USD",
         "positions": [], "universe": [], "plan": []}
    )

    auto.run_auto(ExecutionConfig(watchlist="NVDA"))

    assert calls == ["NVDA"]


def test_run_auto_splits_budget_by_conviction(patched):
    broker = _FakeBroker(last=200.0)
    decisions = {
        "NVDA": _decision(yahoo="NVDA", signal="Buy", side="BUY"),
        "AAPL": _decision(yahoo="AAPL", signal="Overweight", side="BUY"),
    }
    apply, _ = patched
    apply(broker, decisions, _prices(entry=200.0))

    auto.run_auto(ExecutionConfig(watchlist="NVDA,AAPL"), budget=600.0)

    # NVDA (Buy, weight 2) -> $400 -> 2 shares; AAPL (Overweight, weight 1) ->
    # $200 -> 1 share.
    assert broker.orders == [
        {"symbol": "US.NVDA", "side": "BUY", "quantity": 2.0, "price": 200.0, "order_type": "NORMAL", "time_in_force": "GTC"},
        {"symbol": "US.AAPL", "side": "BUY", "quantity": 1.0, "price": 200.0, "order_type": "NORMAL", "time_in_force": "GTC"},
    ]
    assert auto.auto_plan.load_plan()["budget"] == 600.0


def test_run_auto_manage_mode_skips_buys_but_trims(patched):
    broker = _FakeBroker(positions=[{"symbol": "NVDA", "quantity": 5, "average_price": 100.0}])
    decisions = {
        "NEW": _decision(yahoo="NEW", signal="Buy", side="BUY"),
        "NVDA": _decision(yahoo="NVDA", signal="Underweight", side="SELL"),
    }
    apply, _ = patched
    apply(broker, decisions, _prices())

    auto.run_auto(ExecutionConfig(watchlist="NEW"))  # no budget -> manage only

    # The BUY is skipped (no budget); Underweight trims to floor(5 * 0.5) = 2.
    assert broker.orders == [
        {"symbol": "US.NVDA", "side": "SELL", "quantity": 2.0, "price": 200.0, "order_type": "NORMAL", "time_in_force": "GTC"},
    ]
    assert auto.auto_plan.load_plan()["budget"] is None


def test_run_auto_sell_is_full_exit(patched):
    broker = _FakeBroker(positions=[{"symbol": "NVDA", "quantity": 5, "average_price": 100.0}])
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Sell", side="SELL")}
    apply, _ = patched
    apply(broker, decisions, _prices())

    auto.run_auto(ExecutionConfig())  # universe = holdings only

    assert broker.orders == [
        {"symbol": "US.NVDA", "side": "SELL", "quantity": 5.0, "price": 200.0, "order_type": "NORMAL", "time_in_force": "GTC"},
    ]


def test_run_auto_skips_hold_and_review(patched):
    broker = _FakeBroker()
    decisions = {
        "HOLD": _decision(yahoo="HOLD", signal="Hold", side=None),
        "REV": _decision(yahoo="REV", signal="REVIEW", side=None),
    }
    apply, _ = patched
    apply(broker, decisions, _prices())

    result = auto.run_auto(ExecutionConfig(watchlist="HOLD,REV"))

    assert broker.orders == []
    assert "hold" in result
    assert "REVIEW" in result


def test_run_auto_persists_date_keyed_plan(patched):
    broker = _FakeBroker()
    apply, _ = patched
    apply(broker, {"NVDA": _decision()}, _prices())

    auto.run_auto(ExecutionConfig(watchlist="NVDA"), budget=1900.0)

    plan = auto.auto_plan.load_plan()
    assert plan["date"] == date.today().isoformat()
    assert plan["budget"] == 1900.0
    assert plan["universe"] == ["NVDA"]
    assert plan["plan"][0]["ticker"] == "NVDA"
    assert plan["plan"][0]["reason"] == "ok"
    assert plan["plan"][0]["executed"]["order_id"] == 1


def test_run_auto_places_stop_loss_after_market_buy(patched):
    """A MARKET buy fills immediately, so a protective stop is placed right after."""
    broker = _FakeBroker()
    decisions = {"NEW": _decision(yahoo="NEW", signal="Buy", side="BUY")}
    apply, _ = patched
    # entry 300 vs live 190 -> out of band -> MARKET buy; stop 172 -> STOP sell.
    apply(broker, decisions, {"entry_price": 300.0, "stop_loss": 172.0, "price_target": 200.0})

    auto.run_auto(ExecutionConfig(watchlist="NEW"), budget=1900.0)

    assert broker.orders == [
        {"symbol": "US.NEW", "side": "BUY", "quantity": 10.0, "price": None, "order_type": "MARKET", "time_in_force": "DAY"},
        {"symbol": "US.NEW", "side": "SELL", "quantity": 10.0, "price": 172.0, "order_type": "STOP", "time_in_force": "GTC"},
    ]


def test_run_auto_no_stop_loss_for_pending_limit_buy(patched):
    """A limit buy hasn't filled, so the stop level is recorded, not placed."""
    broker = _FakeBroker()
    decisions = {"NEW": _decision(yahoo="NEW", signal="Buy", side="BUY")}
    apply, _ = patched
    apply(broker, decisions, _prices())  # entry 189.5 in-band -> LIMIT buy

    auto.run_auto(ExecutionConfig(watchlist="NEW"), budget=1900.0)

    # Only the LIMIT buy; no STOP sell (nothing to protect yet).
    assert len(broker.orders) == 1
    assert broker.orders[0]["order_type"] == "NORMAL"
    plan = auto.auto_plan.load_plan()
    assert plan["plan"][0]["stop_loss"] == 172.0  # recorded for a later run
    assert plan["plan"][0]["stop_order"] is None


def test_run_auto_cancels_stale_orders_before_placing(patched):
    broker = _FakeBroker(last=200.0)
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Buy", side="BUY")}
    apply, _ = patched
    apply(broker, decisions, _prices(entry=200.0))

    auto.run_auto(ExecutionConfig(watchlist="NVDA"), budget=400.0)

    # A fresh verdict wipes prior-day resting orders on the ticker first.
    assert broker.cancelled == ["US.NVDA"]
    assert len(broker.orders) == 1


def test_run_auto_reuses_same_day_research(patched, monkeypatch, tmp_path):
    broker = _FakeBroker(last=200.0)
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Buy", side="BUY")}
    apply, calls = patched
    apply(broker, decisions, _prices(entry=200.0))

    # Pre-seed today's interactive research on NVDA into the shared history.
    monkeypatch.setattr(persist, "history_path", lambda: str(tmp_path / "hist.json"))
    persist.save_history({
        "date": date.today().isoformat(),
        "researches": {"NVDA": {
            "executed": False,
            "decision": {
                "yahoo": "NVDA", "moomoo": "US.NVDA", "signal": "Buy", "side": "BUY",
                "summary": "buy it", "cash": None, "position_quantity": None,
                "research": {},
            },
        }},
        "chat_history": [],
    })

    auto.run_auto(ExecutionConfig(watchlist="NVDA"), budget=400.0)

    # The morning's verdict is reused — the graph is never re-run — but the buy
    # is still sized and placed off today's live quote.
    assert calls == []
    assert broker.orders == [
        {"symbol": "US.NVDA", "side": "BUY", "quantity": 2.0, "price": 200.0, "order_type": "NORMAL", "time_in_force": "GTC"},
    ]


def test_run_auto_long_horizon_places_gtc_limit(patched):
    broker = _FakeBroker(last=200.0)
    d = _decision(research={"investment_plan": "**Time Horizon**: 3-6 months"})
    apply, _ = patched
    apply(broker, {"NVDA": d}, _prices(entry=200.0))

    auto.run_auto(ExecutionConfig(watchlist="NVDA"), budget=400.0)

    assert broker.orders[0]["order_type"] == "NORMAL"
    assert broker.orders[0]["time_in_force"] == "GTC"


def test_run_auto_short_horizon_places_day_limit(patched):
    broker = _FakeBroker(last=200.0)
    d = _decision(research={"investment_plan": "**Time Horizon**: short-term"})
    apply, _ = patched
    apply(broker, {"NVDA": d}, _prices(entry=200.0))

    auto.run_auto(ExecutionConfig(watchlist="NVDA"), budget=400.0)

    assert broker.orders[0]["order_type"] == "NORMAL"
    assert broker.orders[0]["time_in_force"] == "DAY"
