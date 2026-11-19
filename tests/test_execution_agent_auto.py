"""The /auto loop: conviction sizing, budget allocation, and the full run."""

from datetime import date

import pytest

from execution_agent import auto, persist, watchlist
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


# --- Hold-pullback (opt-in): is_hold_pullback_candidate / hold_pullback_action --


def _hold_decision(yahoo="NVDA"):
    return DecisionResult(
        yahoo=yahoo, moomoo=f"US.{yahoo}", signal="Hold", side=None,
        summary="", cash=None, position_quantity=None, research={},
    )


def test_is_hold_pullback_candidate_true_when_entry_below_live():
    decision = _hold_decision()
    book = PortfolioContext(cash=10000.0, currency="USD", positions=[])

    assert auto.is_hold_pullback_candidate(decision, {"entry_price": 150.0}, book, 190.0) is True


def test_is_hold_pullback_candidate_false_when_entry_at_or_above_live():
    """A pullback level at/above the live price would fill like a market
    order instantly — the opposite of "wait for it to come down"."""
    decision = _hold_decision()
    book = PortfolioContext(cash=10000.0, currency="USD", positions=[])

    assert auto.is_hold_pullback_candidate(decision, {"entry_price": 200.0}, book, 190.0) is False


def test_is_hold_pullback_candidate_false_when_already_held():
    """Hold on something you already own means "keep it", not "buy more" —
    the bracket already protects an existing holding."""
    decision = _hold_decision()
    book = PortfolioContext(
        cash=10000.0, currency="USD",
        positions=[Position(ticker="NVDA", quantity=1.0, average_price=100.0)],
    )

    assert auto.is_hold_pullback_candidate(decision, {"entry_price": 150.0}, book, 190.0) is False


def test_is_hold_pullback_candidate_false_without_entry_price():
    decision = _hold_decision()
    book = PortfolioContext(cash=10000.0, currency="USD", positions=[])

    assert auto.is_hold_pullback_candidate(decision, {"entry_price": None}, book, 190.0) is False


def test_is_hold_pullback_candidate_false_for_review():
    """REVIEW also has side=None — must not be mistaken for Hold."""
    decision = DecisionResult(
        yahoo="NVDA", moomoo="US.NVDA", signal="REVIEW", side=None,
        summary="", cash=None, position_quantity=None, research={},
    )
    book = PortfolioContext(cash=10000.0, currency="USD", positions=[])

    assert auto.is_hold_pullback_candidate(decision, {"entry_price": 150.0}, book, 190.0) is False


def test_is_hold_pullback_candidate_false_for_a_real_buy_rating():
    decision = DecisionResult(
        yahoo="NVDA", moomoo="US.NVDA", signal="Buy", side="BUY",
        summary="", cash=None, position_quantity=None, research={},
    )
    book = PortfolioContext(cash=10000.0, currency="USD", positions=[])

    assert auto.is_hold_pullback_candidate(decision, {"entry_price": 150.0}, book, 190.0) is False


def test_is_hold_pullback_candidate_false_without_a_live_quote():
    decision = _hold_decision()
    book = PortfolioContext(cash=10000.0, currency="USD", positions=[])

    assert auto.is_hold_pullback_candidate(decision, {"entry_price": 150.0}, book, None) is False


def test_hold_pullback_action_no_budget():
    action = auto.hold_pullback_action({"entry_price": 150.0}, 190.0, None, "GTC")
    assert action == {
        "side": "BUY", "quantity": 0.0, "order_type": None, "limit_price": None, "reason": "no_budget",
    }


def test_hold_pullback_action_sizes_off_entry_price_not_live():
    """A $750 budget at a $150 pullback buys 5 shares, not floor(750/live)."""
    action = auto.hold_pullback_action({"entry_price": 150.0}, 190.0, 750.0, "GTC")
    assert action == {
        "side": "BUY", "quantity": 5.0, "order_type": "LIMIT", "limit_price": 150.0,
        "time_in_force": "GTC", "reason": "ok",
    }


def test_hold_pullback_action_too_small_a_budget_is_no_price():
    action = auto.hold_pullback_action({"entry_price": 150.0}, 190.0, 100.0, "GTC")
    assert action["reason"] == "no_price"
    assert action["quantity"] == 0.0


def test_hold_pullback_action_never_falls_back_to_market():
    """Unlike decide_action, this never produces a MARKET order — the whole
    point is waiting for the pullback, not buying at today's price."""
    action = auto.hold_pullback_action({"entry_price": 150.0}, 190.0, 750.0, "GTC")
    assert action["order_type"] == "LIMIT"


# --- position_qty_after / plan_bracket (pure helpers) ---------------------


def _bracket_book(positions=None):
    return PortfolioContext(cash=10000.0, currency="USD", positions=positions or [])


def _held(ticker="NVDA", qty=5.0):
    return _bracket_book([Position(ticker=ticker, quantity=qty, average_price=100.0)])


def _action(side, quantity=0.0, order_type=None, reason="ok"):
    return {"side": side, "quantity": quantity, "order_type": order_type, "reason": reason}


def test_position_qty_after_hold_on_existing_holding_is_unchanged():
    """The backfill case: no action this run, but a position is already held."""
    assert auto.position_qty_after(_held(qty=5.0), "NVDA", _action(None, reason="hold"), None) == 5.0


def test_position_qty_after_market_buy_adds_to_position():
    executed = {"order_id": 1}
    action = _action("BUY", quantity=10.0, order_type="MARKET")
    assert auto.position_qty_after(_book(), "NVDA", action, executed) == 10.0


def test_position_qty_after_pending_limit_buy_unchanged():
    """A resting LIMIT buy hasn't filled — nothing to protect yet."""
    executed = {"order_id": 1}
    action = _action("BUY", quantity=10.0, order_type="LIMIT")
    assert auto.position_qty_after(_book(), "NVDA", action, executed) == 0.0


def test_position_qty_after_sell_subtracts_even_when_pending():
    """A SELL's own order already chases price_target for that qty — whether
    it fills now or rests, those shares must not also get a bracket target."""
    executed = {"order_id": 1}
    action = _action("SELL", quantity=5.0, order_type="LIMIT")
    assert auto.position_qty_after(_held(qty=5.0), "NVDA", action, executed) == 0.0


def test_position_qty_after_trim_leaves_remainder():
    executed = {"order_id": 1}
    action = _action("SELL", quantity=2.0, order_type="NORMAL")
    assert auto.position_qty_after(_held(qty=5.0), "NVDA", action, executed) == 3.0


def test_position_qty_after_rejected_sell_leaves_full_position():
    """not_approved/no_position/error are never "ok" — nothing was exited."""
    action = _action("SELL", quantity=5.0, order_type="LIMIT", reason="not_approved")
    assert auto.position_qty_after(_held(qty=5.0), "NVDA", action, None) == 5.0


def test_plan_bracket_zero_qty_is_empty():
    assert auto.plan_bracket(0.0, {"stop_loss": 170.0, "price_target": 200.0}) == {
        "stop": None, "target": None,
    }


def test_plan_bracket_both_sides():
    result = auto.plan_bracket(5.0, {"stop_loss": 170.0, "price_target": 200.0})
    assert result == {
        "stop": {"price": 170.0, "quantity": 5.0},
        "target": {"price": 200.0, "quantity": 5.0},
    }


def test_plan_bracket_skips_side_without_a_usable_level():
    result = auto.plan_bracket(5.0, {"stop_loss": None, "price_target": 200.0})
    assert result["stop"] is None
    assert result["target"] == {"price": 200.0, "quantity": 5.0}


def test_allocate_budget_weighted_split():
    buys = [("NVDA", 2.0), ("AAPL", 1.0)]
    assert auto.allocate_budget(buys, 600.0) == {"NVDA": 400.0, "AAPL": 200.0}


def test_allocate_budget_empty_and_no_budget():
    assert auto.allocate_budget([], 600.0) == {}
    assert auto.allocate_budget([("NVDA", 2.0)], None) == {}
    assert auto.allocate_budget([("NVDA", 0.0)], 600.0) == {}


# --- verdict / verdict_table: the terse Buy/Sell/Hold/Not-worth call --------


def _plan_entry(**overrides):
    base = {
        "ticker": "NVDA", "side": "BUY", "signal": "Buy", "quantity": 0.0,
        "order_type": None, "limit_price": None, "notional": None, "weight": None,
        "entry_price": None, "stop_loss": None, "price_target": None,
        "time_horizon": None, "time_in_force": None, "summary": "", "executed": None,
        "bracket": None, "reason": "ok",
    }
    base.update(overrides)
    return base


def test_verdict_market_buy_is_bare_buy():
    entry = _plan_entry(reason="ok", side="BUY", order_type="MARKET")
    assert auto.verdict(entry) == "Buy"


def test_verdict_market_sell_is_bare_sell():
    entry = _plan_entry(reason="ok", side="SELL", order_type="MARKET")
    assert auto.verdict(entry) == "Sell"


def test_verdict_limit_buy_shows_trigger_price():
    entry = _plan_entry(reason="ok", side="BUY", order_type="LIMIT", limit_price=190.0)
    assert auto.verdict(entry) == "Buy @ 190"


def test_verdict_limit_sell_shows_trigger_price():
    entry = _plan_entry(reason="ok", side="SELL", order_type="LIMIT", limit_price=220.0)
    assert auto.verdict(entry) == "Sell @ 220"


def test_verdict_hold():
    assert auto.verdict(_plan_entry(reason="hold", side=None)) == "Hold"


def test_verdict_review():
    assert auto.verdict(_plan_entry(reason="review", side=None)) == "Review"


def test_verdict_no_position_is_not_worth():
    entry = _plan_entry(reason="no_position", side="SELL", signal="Sell")
    assert auto.verdict(entry) == "Not worth"


def test_verdict_no_budget_shows_would_be_entry_unset():
    """The trailing "?" is the signal that nothing is actually resting."""
    entry = _plan_entry(reason="no_budget", side="BUY", entry_price=190.0)
    assert auto.verdict(entry) == "Buy @ 190?"


def test_verdict_no_budget_without_a_level_is_bare_buy_unset():
    entry = _plan_entry(reason="no_budget", side="BUY", entry_price=None)
    assert auto.verdict(entry) == "Buy?"


def test_verdict_no_price_shows_would_be_entry_unset():
    entry = _plan_entry(reason="no_price", side="BUY", entry_price=190.0)
    assert auto.verdict(entry) == "Buy @ 190?"


def test_verdict_not_approved_shows_the_rejected_price_unset():
    entry = _plan_entry(reason="not_approved", side="SELL", limit_price=220.0)
    assert auto.verdict(entry) == "Sell @ 220?"


def test_verdict_real_resting_limit_has_no_question_mark():
    """A LIMIT order from a successful placement IS live on the broker — no ?."""
    entry = _plan_entry(reason="ok", side="BUY", order_type="LIMIT", limit_price=190.0)
    assert auto.verdict(entry) == "Buy @ 190"
    assert "?" not in auto.verdict(entry)


def test_verdict_unknown_reason_is_error():
    assert auto.verdict(_plan_entry(reason="something_else")) == "Error"


def test_verdict_table_is_aligned_and_fenced():
    plan = [
        _plan_entry(ticker="NVDA", reason="ok", side="SELL", order_type="MARKET"),
        _plan_entry(ticker="AAPLLONG", reason="hold", side=None),
    ]

    table = auto.verdict_table(plan)

    assert table.startswith("```\n") and table.endswith("\n```")
    assert "NVDA    " in table  # padded to AAPLLONG's width
    assert "Sell" in table and "Hold" in table


def test_verdict_table_empty_plan_is_empty_string():
    assert auto.verdict_table([]) == ""


# --- levels_hint: "hold till what price" / "sell at what price" -----------


def test_levels_hint_shows_bracket_when_present():
    """The direct answer to "hold till what price" — the actual resting
    stop/target this ticker is protected by right now."""
    entry = _plan_entry(
        reason="hold", side=None,
        bracket={"stop_order_id": 1, "target_order_id": 2, "stop_price": 150.0, "target_price": 180.0},
    )
    assert auto.levels_hint(entry) == "stop 150 / target 180"


def test_levels_hint_bracket_with_only_one_side():
    entry = _plan_entry(
        reason="hold", side=None,
        bracket={"stop_order_id": 1, "target_order_id": None, "stop_price": 150.0, "target_price": None},
    )
    assert auto.levels_hint(entry) == "stop 150"


def test_levels_hint_falls_back_to_research_target_without_a_bracket():
    """Not worth — nothing held, no bracket, but still show the re-entry
    reference when the research actually gave one."""
    entry = _plan_entry(reason="no_position", side="SELL", signal="Sell", price_target=349.0)
    assert auto.levels_hint(entry) == "target 349"


def test_levels_hint_is_dash_when_nothing_is_known():
    """Normal when the research simply never produced a usable target."""
    entry = _plan_entry(reason="no_position", side="SELL", signal="Underweight")
    assert auto.levels_hint(entry) == "-"


def test_levels_hint_hold_not_held_falls_back_to_entry_watch_level():
    """Hold on a ticker you don't own: no exit level to show, so fall back to
    the trader's entry price as a watch level — not an order, just a level."""
    entry = _plan_entry(reason="hold", side=None, entry_price=349.73)
    assert auto.levels_hint(entry) == "watch 349.73"


def test_levels_hint_not_worth_never_shows_an_entry_watch_level():
    """A Sell/Underweight call (Not worth) must not show a buy-side entry
    level — that would contradict the bearish verdict."""
    entry = _plan_entry(reason="no_position", side="SELL", signal="Sell", entry_price=310.5)
    assert auto.levels_hint(entry) == "-"


def test_levels_hint_bracket_takes_priority_over_research_target():
    """A held, protected position shows what it's ACTUALLY protected by, not
    the raw research number (which may differ slightly from the placed order)."""
    entry = _plan_entry(
        reason="ok", side="BUY", order_type="MARKET", price_target=355.0,
        bracket={"stop_order_id": 1, "target_order_id": 2, "stop_price": 280.0, "target_price": 350.0},
    )
    assert auto.levels_hint(entry) == "stop 280 / target 350"


def test_verdict_table_includes_levels_column():
    plan = [
        _plan_entry(
            ticker="NVDA", reason="hold", side=None,
            bracket={"stop_order_id": 1, "target_order_id": 2, "stop_price": 150.0, "target_price": 180.0},
        ),
        _plan_entry(ticker="WDC", reason="no_position", side="SELL", signal="Underweight"),
    ]

    table = auto.verdict_table(plan)

    assert "Levels" in table
    assert "stop 150 / target 180" in table
    assert "WDC" in table and "  -" in table  # no levels known for WDC


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
    """The trim floors to zero (e.g. a 1-share or fractional-dust position),
    but the ticker is still genuinely held — must report "hold", not
    "no_position" ("Not worth"), which would wrongly claim it isn't held."""
    book = _book(positions=[Position(ticker="NVDA", quantity=1, average_price=100.0)])
    action = auto.decide_action(
        _decision(signal="Underweight", side="SELL"), _prices(), book, ExecutionConfig(), live=190.0
    )
    assert action["reason"] == "hold"
    assert action["quantity"] == 0.0


def test_decide_action_fractional_dust_position_trims_to_zero_reports_hold():
    """Real-world case: a fractional leftover (e.g. 0.3135 shares from a
    corporate action) rated Underweight. Half of a third of a share floors
    to zero — must still read "Hold", not "Not worth", since the position is
    genuinely held."""
    book = _book(positions=[Position(ticker="WDC", quantity=0.3135, average_price=755.0)])
    action = auto.decide_action(
        _decision(yahoo="WDC", signal="Underweight", side="SELL"),
        _prices(), book, ExecutionConfig(), live=287.15,
    )
    assert action["reason"] == "hold"
    assert auto.verdict(action) == "Hold"


def test_decide_action_sell_no_position():
    action = auto.decide_action(
        _decision(signal="Sell", side="SELL"), _prices(), _book(), ExecutionConfig(), live=190.0
    )
    assert action["reason"] == "no_position"
    assert action["quantity"] == 0.0


# --- Sell with no usable target: protective marketable limit, not raw MARKET --


def test_decide_action_sell_without_target_uses_marketable_limit_not_market():
    """The actual bug report: no price_target must never mean 'accept any
    price' — it's a LIMIT near live, not a raw, unprotected MARKET order."""
    book = _book(positions=[Position(ticker="NVDA", quantity=5, average_price=100.0)])
    action = auto.decide_action(
        _decision(signal="Sell", side="SELL"),
        {"entry_price": 170.0, "stop_loss": 158.0, "price_target": None},
        book, ExecutionConfig(), live=140.0,
    )
    assert action["order_type"] == "LIMIT"
    assert action["limit_price"] == pytest.approx(140.0 * 0.98)  # default 2% buffer
    assert action["reason"] == "ok"


def test_decide_action_sell_target_too_far_from_live_also_uses_marketable_limit():
    """An out-of-band target (sane_limit rejects it) gets the same protection
    as a missing one — both mean "nothing usable to anchor on"."""
    book = _book(positions=[Position(ticker="NVDA", quantity=5, average_price=100.0)])
    action = auto.decide_action(
        _decision(signal="Sell", side="SELL"),
        {"entry_price": 170.0, "stop_loss": 158.0, "price_target": 400.0},  # way out of band
        book, ExecutionConfig(), live=140.0,
    )
    assert action["order_type"] == "LIMIT"
    assert action["limit_price"] == pytest.approx(140.0 * 0.98)


def test_decide_action_sell_without_target_and_without_live_falls_back_to_market():
    """No live quote at all means there's nothing to anchor a limit to —
    this is the only remaining case that still uses a raw MARKET order."""
    book = _book(positions=[Position(ticker="NVDA", quantity=5, average_price=100.0)])
    action = auto.decide_action(
        _decision(signal="Sell", side="SELL"),
        {"entry_price": 170.0, "stop_loss": 158.0, "price_target": None},
        book, ExecutionConfig(), live=None,
    )
    assert action["order_type"] == "MARKET"
    assert action["limit_price"] is None


def test_decide_action_sell_buffer_is_configurable():
    book = _book(positions=[Position(ticker="NVDA", quantity=5, average_price=100.0)])
    action = auto.decide_action(
        _decision(signal="Sell", side="SELL"),
        {"entry_price": 170.0, "stop_loss": 158.0, "price_target": None},
        book, ExecutionConfig(auto_market_sell_buffer=0.05), live=100.0,
    )
    assert action["limit_price"] == pytest.approx(95.0)


def test_decide_action_sell_with_usable_target_is_unaffected():
    """The existing, correct behavior — a real in-band target is used as-is,
    not touched by the new fallback."""
    book = _book(positions=[Position(ticker="NVDA", quantity=5, average_price=100.0)])
    action = auto.decide_action(
        _decision(signal="Sell", side="SELL"), _prices(), book, ExecutionConfig(), live=190.0
    )
    assert action["limit_price"] == 200.0  # _prices()'s price_target, untouched


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
        self.orders = []  # exact shape callers assert on — never gains new keys
        self._statuses = {}  # 1-based order_id -> "SUBMITTED" | "CANCELLED"
        self.cancelled = []  # symbols passed to cancel_open_orders
        self.cancelled_ids = []  # order ids passed to cancel_order

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
        order_id = len(self.orders)
        self._statuses[order_id] = "SUBMITTED"
        return {"order_id": order_id, "order_status": "SUBMITTED"}

    def cancel_open_orders(self, symbol):
        self.cancelled.append(symbol)
        return 0

    def list_orders(self, symbol=None, *, open_only=False):
        out = []
        for order_id, o in enumerate(self.orders, start=1):
            status = self._statuses.get(order_id, "SUBMITTED")
            if open_only and status != "SUBMITTED":
                continue
            if symbol is not None and o["symbol"] != symbol:
                continue
            out.append(
                {"order_id": order_id, "symbol": o["symbol"], "side": o["side"],
                 "quantity": o["quantity"], "price": o["price"],
                 "order_type": o["order_type"], "order_status": status}
            )
        return out

    def cancel_order(self, order_id):
        self.cancelled_ids.append(order_id)
        self._statuses[order_id] = "CANCELLED"


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
    # Isolate the live watchlist file: absent -> the config seed is used.
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: str(tmp_path / "wl.json"))
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


def test_run_auto_real_mode_without_approve_fails_safe(patched):
    """REAL with no approve callable must never place a trade unsupervised."""
    broker = _FakeBroker(last=200.0)
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Buy", side="BUY")}
    apply, _ = patched
    apply(broker, decisions, _prices(entry=200.0))

    result = auto.run_auto(ExecutionConfig(watchlist="NVDA"), trading_env="REAL", budget=400.0)

    assert broker.orders == []  # nothing placed without an approval mechanism
    assert "Buy @ 200?" in result  # unset — the trigger price, not yet approved


def test_run_auto_real_mode_approved_trade_places(patched):
    broker = _FakeBroker(last=200.0)
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Buy", side="BUY")}
    apply, _ = patched
    apply(broker, decisions, _prices(entry=200.0))
    asked = []

    def approve(ticker, side, quantity, order_type, limit_price):
        asked.append((ticker, side, quantity, order_type, limit_price))
        return True

    auto.run_auto(
        ExecutionConfig(watchlist="NVDA"), trading_env="REAL", budget=400.0, approve=approve
    )

    assert asked == [("NVDA", "BUY", 2.0, "LIMIT", 200.0)]
    assert len(broker.orders) == 1


def test_run_auto_real_mode_rejected_trade_skips(patched):
    broker = _FakeBroker(last=200.0)
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Buy", side="BUY")}
    apply, _ = patched
    apply(broker, decisions, _prices(entry=200.0))

    result = auto.run_auto(
        ExecutionConfig(watchlist="NVDA"), trading_env="REAL", budget=400.0,
        approve=lambda *a: False,
    )

    assert broker.orders == []
    assert "Buy @ 200?" in result


def test_run_auto_simulate_mode_never_calls_approve(patched):
    """SIMULATE places immediately — approve is only consulted on REAL."""
    broker = _FakeBroker(last=200.0)
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Buy", side="BUY")}
    apply, _ = patched
    apply(broker, decisions, _prices(entry=200.0))

    def never_called(*a):
        raise AssertionError("approve must not be called on SIMULATE")

    auto.run_auto(
        ExecutionConfig(watchlist="NVDA"), trading_env="SIMULATE", budget=400.0,
        approve=never_called,
    )

    assert len(broker.orders) == 1  # placed without ever consulting approve


def test_run_auto_real_mode_sell_also_needs_approval(patched):
    broker = _FakeBroker(positions=[{"symbol": "NVDA", "quantity": 5, "average_price": 100.0}])
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Sell", side="SELL")}
    apply, _ = patched
    apply(broker, decisions, _prices())

    auto.run_auto(ExecutionConfig(), trading_env="REAL", approve=lambda *a: False)

    # The sell itself never placed an order — but the shares are still fully
    # held (nothing exited them), so they get a fresh protective bracket.
    assert broker.orders == [
        {"symbol": "US.NVDA", "side": "SELL", "quantity": 5.0, "price": 172.0, "order_type": "STOP", "time_in_force": "GTC"},
        {"symbol": "US.NVDA", "side": "SELL", "quantity": 5.0, "price": 200.0, "order_type": "NORMAL", "time_in_force": "GTC"},
    ]


def test_summarize_shows_real_mode_label(patched):
    broker = _FakeBroker(last=200.0)
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Buy", side="BUY")}
    apply, _ = patched
    apply(broker, decisions, _prices(entry=200.0))

    result = auto.run_auto(
        ExecutionConfig(watchlist="NVDA"), trading_env="REAL", budget=400.0,
        approve=lambda *a: True,
    )

    assert "REAL" in result


def test_summarize_explains_question_mark_only_when_present():
    with_unset = {"plan": [_plan_entry(reason="no_budget", entry_price=190.0)]}
    assert "not set yet" in auto._summarize(with_unset)

    all_set = {"plan": [_plan_entry(reason="ok", side="BUY", order_type="MARKET")]}
    assert "not set yet" not in auto._summarize(all_set)


def test_run_auto_reuses_same_day_research_but_reruns_pass23(patched):
    broker = _FakeBroker()
    apply, calls = patched
    apply(broker, {}, _prices())

    # NVDA is already in the shared store *and* in today's plan — nothing new,
    # so a same-day re-run must skip research entirely.
    auto.auto_plan.save_plan(
        {"date": date.today().isoformat(), "cash": 5000.0, "currency": "USD",
         "positions": [], "universe": ["NVDA"],
         "plan": [{"ticker": "NVDA", "side": None, "reason": "hold", "signal": "Hold"}]}
    )
    persist.save_history({
        "date": date.today().isoformat(),
        "researches": {"NVDA": {"executed": False, "decision": {
            "yahoo": "NVDA", "moomoo": "US.NVDA", "signal": "Hold", "side": None,
            "summary": "", "cash": None, "position_quantity": None, "research": {},
        }}},
        "chat_history": [],
    })

    result = auto.run_auto(ExecutionConfig(watchlist="NVDA"))

    # Research isn't re-paid for (calls stays empty — the LLM pipeline never
    # runs again), but Pass 2/3 still re-processes NVDA every call — that's
    # what keeps the protective bracket fresh on a ticker nothing else changed.
    assert calls == []
    assert "NVDA    Hold" in result


def test_run_auto_researches_new_watchlist_ticker_on_same_day(patched):
    broker = _FakeBroker(last=200.0)
    decisions = {"AAPL": _decision(yahoo="AAPL", signal="Buy", side="BUY")}
    apply, calls = patched
    apply(broker, decisions, _prices(entry=200.0))

    # Today's plan already covers NVDA, and the shared store holds NVDA too —
    # so a same-day re-run must leave NVDA alone and only research the new AAPL.
    auto.auto_plan.save_plan(
        {"date": date.today().isoformat(), "cash": 5000.0, "currency": "USD",
         "positions": [], "universe": ["NVDA"],
         "plan": [{"ticker": "NVDA", "side": None, "reason": "hold", "signal": "Hold"}]}
    )
    persist.save_history({
        "date": date.today().isoformat(),
        "researches": {"NVDA": {"executed": False, "decision": {
            "yahoo": "NVDA", "moomoo": "US.NVDA", "signal": "Hold", "side": None,
            "summary": "", "cash": None, "position_quantity": None, "research": {},
        }}},
        "chat_history": [],
    })

    auto.run_auto(ExecutionConfig(watchlist="NVDA,AAPL"), budget=400.0)

    assert calls == ["AAPL"]
    plan = auto.auto_plan.load_plan()
    assert plan["universe"] == ["NVDA", "AAPL"]  # full universe, order preserved
    assert [e["ticker"] for e in plan["plan"]] == ["NVDA", "AAPL"]  # carried + new


def test_run_auto_re_researches_plan_ticker_missing_from_store(patched):
    broker = _FakeBroker(last=200.0)
    decisions = {"AAPL": _decision(yahoo="AAPL", signal="Sell", side="SELL")}
    apply, calls = patched
    apply(broker, decisions, _prices(entry=200.0))

    # The plan covers both tickers, but only NVDA reached the shared store (a
    # pre-sharing run). AAPL must be re-researched to fill the /chat gap.
    auto.auto_plan.save_plan(
        {"date": date.today().isoformat(), "cash": 5000.0, "currency": "USD",
         "positions": [], "universe": ["NVDA", "AAPL"],
         "plan": [
             {"ticker": "NVDA", "side": None, "reason": "hold", "signal": "Hold"},
             {"ticker": "AAPL", "side": "SELL", "reason": "no_position", "signal": "Sell"},
         ]}
    )
    persist.save_history({
        "date": date.today().isoformat(),
        "researches": {"NVDA": {"executed": False, "decision": {
            "yahoo": "NVDA", "moomoo": "US.NVDA", "signal": "Hold", "side": None,
            "summary": "", "cash": None, "position_quantity": None, "research": {},
        }}},
        "chat_history": [],
    })

    auto.run_auto(ExecutionConfig(watchlist="NVDA,AAPL"))

    assert calls == ["AAPL"]
    plan = auto.auto_plan.load_plan()
    assert [e["ticker"] for e in plan["plan"]] == ["NVDA", "AAPL"]


def test_run_auto_skips_ticker_that_errored_today(patched):
    """A ticker that failed earlier today isn't re-researched on a same-day run."""
    broker = _FakeBroker(last=200.0)
    apply, calls = patched
    apply(broker, {}, _prices())

    auto.auto_plan.save_plan(
        {"date": date.today().isoformat(), "cash": 5000.0, "currency": "USD",
         "positions": [], "universe": ["APPL"],
         "plan": [{"ticker": "APPL", "side": None, "reason": "error", "signal": "unknown ticker"}]}
    )

    result = auto.run_auto(ExecutionConfig(watchlist="APPL"))

    assert calls == []  # the failing ticker is not retried today
    assert "every ticker already failed once" in result


def test_run_auto_uses_live_watchlist_file(patched, tmp_path):
    """/watchlist edits (the stored file) win over the .env seed."""
    broker = _FakeBroker(last=200.0)
    decisions = {"AMD": _decision(yahoo="AMD", signal="Buy", side="BUY")}
    apply, calls = patched
    apply(broker, decisions, _prices(entry=200.0))
    watchlist.save(["AMD"])  # the phone-edited list replaces the seed

    auto.run_auto(ExecutionConfig(watchlist="NVDA"), budget=400.0)

    assert calls == ["AMD"]  # file list, not the NVDA seed


def test_run_auto_progress_is_per_ticker_not_per_phase(patched):
    broker = _FakeBroker(last=200.0)
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Buy", side="BUY")}
    apply, _ = patched
    apply(broker, decisions, _prices(entry=200.0))
    steps: list[str] = []

    auto.run_auto(ExecutionConfig(watchlist="NVDA"), budget=400.0, on_step=steps.append)

    assert any("researching NVDA" in s for s in steps)
    assert any(s.startswith("✅ Auto: BUY NVDA") for s in steps)


def test_run_auto_quiet_sends_only_serious(patched):
    """quiet=True (the scheduled run): orders yes, routine progress no."""
    broker = _FakeBroker(last=200.0)
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Buy", side="BUY")}
    apply, _ = patched
    apply(broker, decisions, _prices(entry=200.0))
    steps: list[str] = []

    auto.run_auto(
        ExecutionConfig(watchlist="NVDA"), budget=400.0, on_step=steps.append, quiet=True
    )

    assert not any("researching" in s for s in steps)  # routine chatter dropped
    assert any(s.startswith("✅ Auto: BUY NVDA") for s in steps)  # orders still land


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

    result = auto.run_auto(ExecutionConfig(watchlist="NEW"))  # no budget -> manage only

    # The BUY is skipped (no budget); Underweight trims to floor(5 * 0.5) = 2,
    # and the remaining 3 shares (not part of the trim) get a fresh bracket.
    assert broker.orders == [
        {"symbol": "US.NVDA", "side": "SELL", "quantity": 2.0, "price": 200.0, "order_type": "NORMAL", "time_in_force": "GTC"},
        {"symbol": "US.NVDA", "side": "SELL", "quantity": 3.0, "price": 172.0, "order_type": "STOP", "time_in_force": "GTC"},
        {"symbol": "US.NVDA", "side": "SELL", "quantity": 3.0, "price": 200.0, "order_type": "NORMAL", "time_in_force": "GTC"},
    ]
    assert auto.auto_plan.load_plan()["budget"] is None
    # The skipped BUY still reports the price that would have triggered it.
    assert "NEW     Buy @ 189.5?" in result  # ? — no budget, nothing resting yet


def test_run_auto_sell_is_full_exit(patched):
    broker = _FakeBroker(positions=[{"symbol": "NVDA", "quantity": 5, "average_price": 100.0}])
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Sell", side="SELL")}
    apply, _ = patched
    apply(broker, decisions, _prices())

    auto.run_auto(ExecutionConfig())  # universe = holdings only

    assert broker.orders == [
        {"symbol": "US.NVDA", "side": "SELL", "quantity": 5.0, "price": 200.0, "order_type": "NORMAL", "time_in_force": "GTC"},
    ]


def test_run_auto_sell_without_target_protects_with_marketable_limit(patched):
    """End-to-end reproduction of the user's actual NVDA scenario: no
    price_target, price has dropped hard — must not dump at an unprotected
    MARKET price."""
    broker = _FakeBroker(positions=[{"symbol": "NVDA", "quantity": 5, "average_price": 100.0}], last=140.0)
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Sell", side="SELL")}
    apply, _ = patched
    apply(broker, decisions, {"entry_price": 170.0, "stop_loss": 158.0, "price_target": None})

    auto.run_auto(ExecutionConfig())  # universe = holdings only

    assert broker.orders == [
        {"symbol": "US.NVDA", "side": "SELL", "quantity": 5.0, "price": pytest.approx(137.2), "order_type": "NORMAL", "time_in_force": "GTC"},
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
    assert "Hold" in result
    assert "Review" in result


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


def test_run_auto_places_bracket_after_market_buy(patched):
    """A MARKET buy fills immediately, so a full bracket is placed right after."""
    broker = _FakeBroker()
    decisions = {"NEW": _decision(yahoo="NEW", signal="Buy", side="BUY")}
    apply, _ = patched
    # entry 300 vs live 190 -> out of band -> MARKET buy; stop 172, target 200.
    apply(broker, decisions, {"entry_price": 300.0, "stop_loss": 172.0, "price_target": 200.0})

    auto.run_auto(ExecutionConfig(watchlist="NEW"), budget=1900.0)

    assert broker.orders == [
        {"symbol": "US.NEW", "side": "BUY", "quantity": 10.0, "price": None, "order_type": "MARKET", "time_in_force": "DAY"},
        {"symbol": "US.NEW", "side": "SELL", "quantity": 10.0, "price": 172.0, "order_type": "STOP", "time_in_force": "GTC"},
        {"symbol": "US.NEW", "side": "SELL", "quantity": 10.0, "price": 200.0, "order_type": "NORMAL", "time_in_force": "GTC"},
    ]
    plan = auto.auto_plan.load_plan()
    bracket = plan["plan"][0]["bracket"]
    assert bracket["quantity"] == 10.0
    assert bracket["stop_order_id"] is not None and bracket["target_order_id"] is not None


def test_run_auto_backfills_bracket_for_hold_rated_existing_holding(patched):
    """The actual bug report: a position held before /auto ever touched it
    (e.g. bought via /execute) gets protected the next time /auto runs, even
    though this run's rating is just Hold — no buy or sell happens."""
    broker = _FakeBroker(positions=[{"symbol": "NVDA", "quantity": 1, "average_price": 159.85}])
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Hold", side=None)}
    apply, _ = patched
    apply(broker, decisions, {"entry_price": 160.0, "stop_loss": 150.0, "price_target": 180.0})

    auto.run_auto(ExecutionConfig())  # universe = holdings only

    assert broker.orders == [
        {"symbol": "US.NVDA", "side": "SELL", "quantity": 1.0, "price": 150.0, "order_type": "STOP", "time_in_force": "GTC"},
        {"symbol": "US.NVDA", "side": "SELL", "quantity": 1.0, "price": 180.0, "order_type": "NORMAL", "time_in_force": "GTC"},
    ]


def test_run_auto_hold_never_buys_when_opted_out(patched):
    """Default behavior (unchanged): a Hold rating never buys, budget or not."""
    broker = _FakeBroker(last=190.0)
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Hold", side=None)}
    apply, _ = patched
    apply(broker, decisions, {"entry_price": 150.0, "stop_loss": None, "price_target": None})

    auto.run_auto(ExecutionConfig(watchlist="NVDA"), budget=1000.0)  # auto_buy_hold_pullback defaults off

    assert broker.orders == []


def test_run_auto_hold_pullback_buys_when_opted_in(patched):
    broker = _FakeBroker(last=190.0)
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Hold", side=None)}
    apply, _ = patched
    apply(broker, decisions, {"entry_price": 150.0, "stop_loss": 140.0, "price_target": None})

    result = auto.run_auto(
        ExecutionConfig(watchlist="NVDA", auto_buy_hold_pullback=True), budget=750.0,
    )

    # floor(750/150) = 5 shares, LIMIT at the pullback price — never MARKET.
    assert broker.orders == [
        {"symbol": "US.NVDA", "side": "BUY", "quantity": 5.0, "price": 150.0, "order_type": "NORMAL", "time_in_force": "GTC"},
    ]
    assert "NVDA    Buy @ 150" in result


def test_run_auto_hold_pullback_no_budget_shows_unset_verdict(patched):
    broker = _FakeBroker(last=190.0)
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Hold", side=None)}
    apply, _ = patched
    apply(broker, decisions, {"entry_price": 150.0, "stop_loss": None, "price_target": None})

    result = auto.run_auto(ExecutionConfig(watchlist="NVDA", auto_buy_hold_pullback=True))  # no budget

    assert broker.orders == []
    assert "NVDA    Buy @ 150?" in result  # opted in, just unfunded this run


def test_run_auto_hold_pullback_skipped_for_existing_holding(patched):
    """Hold on something already held stays informational — buying MORE isn't
    what a Hold rating on an owned position means; the bracket covers it."""
    broker = _FakeBroker(positions=[{"symbol": "NVDA", "quantity": 1, "average_price": 100.0}], last=190.0)
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Hold", side=None)}
    apply, _ = patched
    apply(broker, decisions, {"entry_price": 150.0, "stop_loss": 140.0, "price_target": 220.0})

    auto.run_auto(ExecutionConfig(auto_buy_hold_pullback=True), budget=750.0)  # universe = holdings only

    # No extra BUY — only the backfilled bracket (stop + target) for the share already held.
    assert broker.orders == [
        {"symbol": "US.NVDA", "side": "SELL", "quantity": 1.0, "price": 140.0, "order_type": "STOP", "time_in_force": "GTC"},
        {"symbol": "US.NVDA", "side": "SELL", "quantity": 1.0, "price": 220.0, "order_type": "NORMAL", "time_in_force": "GTC"},
    ]


def test_run_auto_hold_pullback_entry_above_live_is_skipped(patched):
    """A pullback at/above the live price would fill immediately — rejected."""
    broker = _FakeBroker(last=190.0)
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Hold", side=None)}
    apply, _ = patched
    apply(broker, decisions, {"entry_price": 200.0, "stop_loss": None, "price_target": None})

    auto.run_auto(ExecutionConfig(watchlist="NVDA", auto_buy_hold_pullback=True), budget=750.0)

    assert broker.orders == []


def test_run_auto_hold_pullback_outweighed_by_a_real_buy(patched):
    """A genuine Buy (weight 2) outweighs an opted-in Hold-pullback (0.5) when
    splitting the same budget — the LLM's own conviction wins the bigger share."""
    # live (160) a bit above entry (150): in-band for BUYME's LIMIT, and
    # genuinely a pullback (entry < live) for NVDA's Hold-pullback.
    broker = _FakeBroker(last=160.0)
    decisions = {
        "BUYME": _decision(yahoo="BUYME", signal="Buy", side="BUY"),
        "NVDA": _decision(yahoo="NVDA", signal="Hold", side=None),
    }
    apply, _ = patched
    apply(broker, decisions, {"entry_price": 150.0, "stop_loss": None, "price_target": None})

    auto.run_auto(
        ExecutionConfig(watchlist="BUYME,NVDA", auto_buy_hold_pullback=True), budget=2500.0,
    )

    orders_by_symbol = {o["symbol"]: o for o in broker.orders}
    # weight 2/2.5 * 2500 = 2000 -> floor(2000/160) = 12; weight 0.5/2.5 * 2500 = 500 -> floor(500/150) = 3
    assert orders_by_symbol["US.NVDA"]["quantity"] == 3.0
    assert orders_by_symbol["US.BUYME"]["quantity"] == 12.0


def test_run_auto_skips_bracket_without_usable_levels(patched):
    broker = _FakeBroker()
    decisions = {"NEW": _decision(yahoo="NEW", signal="Buy", side="BUY")}
    apply, _ = patched
    apply(broker, decisions, {"entry_price": 300.0, "stop_loss": None, "price_target": None})

    auto.run_auto(ExecutionConfig(watchlist="NEW"), budget=1900.0)

    # Only the BUY — no stop, no target, since the research gave no levels.
    assert broker.orders == [
        {"symbol": "US.NEW", "side": "BUY", "quantity": 10.0, "price": None, "order_type": "MARKET", "time_in_force": "DAY"},
    ]
    bracket = auto.auto_plan.load_plan()["plan"][0]["bracket"]
    assert bracket["stop_order_id"] is None and bracket["target_order_id"] is None


def test_run_auto_cancels_stale_stop_on_full_exit(patched):
    """A full exit means nothing is left to protect — the old stop is dropped."""
    broker = _FakeBroker(positions=[{"symbol": "NVDA", "quantity": 5, "average_price": 100.0}])
    broker.place_order("US.NVDA", "SELL", 5.0, order_type="STOP", price=90.0, time_in_force="GTC")
    decisions = {"NVDA": _decision(yahoo="NVDA", signal="Sell", side="SELL")}
    apply, _ = patched
    apply(broker, decisions, _prices())

    auto.run_auto(ExecutionConfig())  # universe = holdings only

    # The old stop (order 1) is cancelled; the exit itself is the only new order.
    assert broker.cancelled_ids == [1]
    assert len(broker.orders) == 2  # the pre-existing stop + the exit sell


def test_run_auto_bracket_one_side_failing_does_not_block_the_other(patched):
    broker = _FakeBroker()
    decisions = {"NEW": _decision(yahoo="NEW", signal="Buy", side="BUY")}
    apply, _ = patched
    apply(broker, decisions, {"entry_price": 300.0, "stop_loss": 172.0, "price_target": 200.0})

    real_place = broker.place_order

    def flaky_place_order(symbol, side, quantity, **kwargs):
        if kwargs.get("order_type") == "STOP":
            raise RuntimeError("stop rejected by broker")
        return real_place(symbol, side, quantity, **kwargs)

    broker.place_order = flaky_place_order

    result = auto.run_auto(ExecutionConfig(watchlist="NEW"), budget=1900.0)

    # The BUY and the take-profit still went through despite the stop failing.
    assert any(o["order_type"] == "NORMAL" and o["side"] == "SELL" for o in broker.orders)
    assert not any(o["order_type"] == "STOP" for o in broker.orders)
    bracket = auto.auto_plan.load_plan()["plan"][0]["bracket"]
    assert bracket["stop_order_id"] is None
    assert bracket["target_order_id"] is not None
    assert "NEW     Buy" in result  # the main trade itself is unaffected


def test_run_auto_no_bracket_for_pending_limit_buy(patched):
    """A limit buy hasn't filled, so no bracket is placed yet — nothing held."""
    broker = _FakeBroker()
    decisions = {"NEW": _decision(yahoo="NEW", signal="Buy", side="BUY")}
    apply, _ = patched
    apply(broker, decisions, _prices())  # entry 189.5 in-band -> LIMIT buy

    auto.run_auto(ExecutionConfig(watchlist="NEW"), budget=1900.0)

    # Only the LIMIT buy; no bracket (nothing to protect yet).
    assert len(broker.orders) == 1
    assert broker.orders[0]["order_type"] == "NORMAL"
    plan = auto.auto_plan.load_plan()
    assert plan["plan"][0]["stop_loss"] == 172.0  # recorded for a later run
    bracket = plan["plan"][0]["bracket"]
    assert bracket["stop_order_id"] is None and bracket["target_order_id"] is None


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
