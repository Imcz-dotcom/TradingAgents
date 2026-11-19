"""Moomoo broker adapter: price fetch + order placement, with the SDK mocked.

``_moomoo`` is monkeypatched to a fake namespace, so no OpenD gateway or network
is needed. The fake contexts read their return values from a mutable ``state``
dict the tests set before each call.
"""

import types

import pandas as pd
import pytest

import execution_agent.broker as broker_mod
from execution_agent.broker import MoomooBroker


@pytest.fixture
def fake_moomoo(monkeypatch):
    state = {
        "quote_ret": 0,
        "quote_data": pd.DataFrame({"last_price": [45.0]}),
        "acc_ret": 0,
        "acc_data": pd.DataFrame(
            {"acc_id": [123], "trd_env": ["SIMULATE"], "trdmarket_auth": [["US"]]}
        ),
        "order_ret": 0,
        "order_data": pd.DataFrame({"order_id": [999], "order_status": ["SUBMITTED"]}),
        "accinfo_ret": 0,
        "accinfo_data": pd.DataFrame({"cash": [10000.0], "currency": ["USD"]}),
        "pos_ret": 0,
        "pos_data": pd.DataFrame(
            {"code": ["US.NVDA"], "qty": [5], "cost_price": [100.0]}
        ),
        "list_ret": 0,
        "list_data": pd.DataFrame(
            {
                "order_id": [1, 2],
                "order_status": ["SUBMITTED", "FILLED_ALL"],
                "order_type": ["NORMAL", "NORMAL"],
            }
        ),
        "modify_ret": 0,
        "modify_data": "ok",
        "created": [],
    }

    class QuoteCtx:
        def __init__(self, host, port):
            self.host, self.port = host, port
            state["created"].append(self)

        def get_market_snapshot(self, code_list):
            self.code_list = code_list
            return state["quote_ret"], state["quote_data"]

        def close(self):
            self.closed = True

    class TradeCtx:
        def __init__(self, filter_trdmarket, host, port):
            self.filter_trdmarket, self.host, self.port = filter_trdmarket, host, port
            state["created"].append(self)

        def get_acc_list(self):
            return state["acc_ret"], state["acc_data"]

        def place_order(self, **kwargs):
            self.place_args = kwargs
            return state["order_ret"], state["order_data"]

        def accinfo_query(self, **kwargs):
            self.accinfo_args = kwargs
            return state["accinfo_ret"], state["accinfo_data"]

        def position_list_query(self, **kwargs):
            self.pos_args = kwargs
            return state["pos_ret"], state["pos_data"]

        def order_list_query(self, **kwargs):
            self.list_args = kwargs
            return state["list_ret"], state["list_data"]

        def modify_order(self, op, order_id, qty, price, **kwargs):
            self.modify_op = op
            self.modify_order_id = order_id
            self.modify_kwargs = kwargs
            return state["modify_ret"], state["modify_data"]

        def close(self):
            self.closed = True

    ns = types.SimpleNamespace(
        RET_OK=0,
        TrdEnv=types.SimpleNamespace(SIMULATE="SIMULATE", REAL="REAL"),
        TrdSide=types.SimpleNamespace(BUY="BUY", SELL="SELL"),
        OrderType=types.SimpleNamespace(MARKET="MARKET", NORMAL="NORMAL", STOP="STOP"),
        TimeInForce=types.SimpleNamespace(DAY="DAY", GTC="GTC"),
        ModifyOrderOp=types.SimpleNamespace(CANCEL="CANCEL"),
        OrderStatus=types.SimpleNamespace(),
        TrdMarket=types.SimpleNamespace(NONE="N/A"),
        Currency=types.SimpleNamespace(USD="USD", HKD="HKD"),
        OpenQuoteContext=QuoteCtx,
        OpenSecTradeContext=TradeCtx,
    )
    monkeypatch.setattr(broker_mod, "_moomoo", lambda: ns)
    return state


def test_last_price(fake_moomoo):
    broker = MoomooBroker(host="127.0.0.1", port=11111)

    assert broker.last_price("US.NVDA") == 45.0

    quote = fake_moomoo["created"][0]
    assert quote.code_list == ["US.NVDA"]
    assert quote.closed is True


def test_last_price_raises_on_quote_error(fake_moomoo):
    fake_moomoo["quote_ret"] = 1
    fake_moomoo["quote_data"] = "quote failed"
    broker = MoomooBroker()

    with pytest.raises(RuntimeError):
        broker.last_price("US.NVDA")


def test_last_price_raises_on_non_positive(fake_moomoo):
    fake_moomoo["quote_data"] = pd.DataFrame({"last_price": [0.0]})
    broker = MoomooBroker()

    with pytest.raises(RuntimeError):
        broker.last_price("US.NVDA")


def test_place_order_buy(fake_moomoo):
    broker = MoomooBroker(trading_env="SIMULATE")

    result = broker.place_order("US.NVDA", "BUY", 10.0)

    assert result == {"order_id": 999, "order_status": "SUBMITTED"}
    trade = fake_moomoo["created"][0]
    assert trade.filter_trdmarket == "N/A"
    args = trade.place_args
    assert args["code"] == "US.NVDA"
    assert args["trd_side"] == "BUY"
    assert args["qty"] == 10.0
    assert args["order_type"] == "MARKET"
    assert args["trd_env"] == "SIMULATE"
    assert args["acc_id"] == 123
    assert args["price"] == 0.0         # market order ignores price
    assert args["time_in_force"] == "DAY"
    assert trade.closed is True


def test_place_order_sell_real_env(fake_moomoo):
    fake_moomoo["acc_data"] = pd.DataFrame(
        {"acc_id": [7], "trd_env": ["REAL"], "trdmarket_auth": [["US"]]}
    )
    broker = MoomooBroker(trading_env="REAL")

    broker.place_order("US.NVDA", "SELL", 3.0)

    args = fake_moomoo["created"][0].place_args
    assert args["trd_side"] == "SELL"
    assert args["trd_env"] == "REAL"
    assert args["acc_id"] == 7
    assert args["qty"] == 3


def test_place_order_fractional_quantity_is_not_truncated(fake_moomoo):
    """A dust position (e.g. 0.3135 shares left over from a corporate action)
    must reach Moomoo as-is — Moomoo's own SDK casts qty to float, so our
    adapter must not pre-truncate it to a zero-quantity order."""
    broker = MoomooBroker(trading_env="SIMULATE")

    broker.place_order("US.WDC", "SELL", 0.3135)

    assert fake_moomoo["created"][0].place_args["qty"] == 0.3135


def test_place_order_raises_when_no_account_for_env(fake_moomoo):
    fake_moomoo["acc_data"] = pd.DataFrame({"acc_id": [123], "trd_env": ["REAL"]})
    broker = MoomooBroker(trading_env="SIMULATE")  # only REAL account exists

    with pytest.raises(RuntimeError, match="no SIMULATE account"):
        broker.place_order("US.NVDA", "BUY", 1.0)


def test_place_order_prefers_account_matching_market(fake_moomoo):
    # A HK paper account is listed first; the US order must pick the US account.
    fake_moomoo["acc_data"] = pd.DataFrame(
        {
            "acc_id": [100, 200],
            "trd_env": ["SIMULATE", "SIMULATE"],
            "trdmarket_auth": [["HK"], ["US"]],
        }
    )
    broker = MoomooBroker(trading_env="SIMULATE")

    broker.place_order("US.NVDA", "BUY", 1.0)

    assert fake_moomoo["created"][0].place_args["acc_id"] == 200


def test_place_order_raises_when_no_account_matches_market(fake_moomoo):
    fake_moomoo["acc_data"] = pd.DataFrame(
        {"acc_id": [100], "trd_env": ["SIMULATE"], "trdmarket_auth": [["HK"]]}
    )
    broker = MoomooBroker(trading_env="SIMULATE")

    with pytest.raises(RuntimeError, match="no SIMULATE account supports US.NVDA"):
        broker.place_order("US.NVDA", "BUY", 1.0)


def test_place_order_raises_on_order_error(fake_moomoo):
    fake_moomoo["order_ret"] = 1
    fake_moomoo["order_data"] = "order rejected"
    broker = MoomooBroker()

    with pytest.raises(RuntimeError):
        broker.place_order("US.NVDA", "BUY", 1.0)


def test_place_order_limit_price(fake_moomoo):
    broker = MoomooBroker(trading_env="SIMULATE")

    result = broker.place_order("US.NVDA", "BUY", 10.0, price=189.5)

    assert result == {"order_id": 999, "order_status": "SUBMITTED"}
    args = fake_moomoo["created"][0].place_args
    assert args["order_type"] == "NORMAL"
    assert args["price"] == 189.5
    assert args["qty"] == 10


def test_place_order_stop(fake_moomoo):
    """A plain STOP has no limit leg — Moomoo wants the trigger in its own
    ``aux_price`` parameter ("missing required parameter Trigger price" is
    exactly what Moomoo says if the trigger is sent via ``price`` instead)."""
    broker = MoomooBroker(trading_env="SIMULATE")

    result = broker.place_order("US.NVDA", "SELL", 5.0, order_type="STOP", price=172.0)

    assert result == {"order_id": 999, "order_status": "SUBMITTED"}
    args = fake_moomoo["created"][0].place_args
    assert args["order_type"] == "STOP"
    assert args["aux_price"] == 172.0
    assert args["price"] == 0.0  # no limit leg on a plain stop-market
    assert args["trd_side"] == "SELL"


def test_place_order_stop_requires_price(fake_moomoo):
    broker = MoomooBroker()

    with pytest.raises(ValueError, match="trigger price"):
        broker.place_order("US.NVDA", "SELL", 5.0, order_type="STOP")


def test_place_order_time_in_force_gtc_on_real(fake_moomoo):
    fake_moomoo["acc_data"] = pd.DataFrame(
        {"acc_id": [7], "trd_env": ["REAL"], "trdmarket_auth": [["US"]]}
    )
    broker = MoomooBroker(trading_env="REAL")

    broker.place_order("US.NVDA", "BUY", 10.0, price=189.5, time_in_force="GTC")

    assert fake_moomoo["created"][0].place_args["time_in_force"] == "GTC"


def test_place_order_gtc_downgrades_to_day_on_simulate(fake_moomoo):
    """Moomoo's paper environment rejects GTC outright ("Paper trading does
    not support GTC orders") — silently fall back to DAY instead of erroring."""
    broker = MoomooBroker(trading_env="SIMULATE")

    broker.place_order("US.NVDA", "BUY", 10.0, price=189.5, time_in_force="GTC")

    assert fake_moomoo["created"][0].place_args["time_in_force"] == "DAY"


def test_place_order_day_on_simulate_is_unaffected(fake_moomoo):
    broker = MoomooBroker(trading_env="SIMULATE")

    broker.place_order("US.NVDA", "BUY", 10.0, price=189.5, time_in_force="DAY")

    assert fake_moomoo["created"][0].place_args["time_in_force"] == "DAY"


def test_cancel_open_orders_cancels_only_open_limits(fake_moomoo):
    fake_moomoo["list_data"] = pd.DataFrame(
        {
            "order_id": [1, 2, 3],
            "order_status": ["SUBMITTED", "FILLED_ALL", "SUBMITTED"],
            "order_type": ["NORMAL", "NORMAL", "STOP"],
        }
    )
    broker = MoomooBroker(trading_env="SIMULATE")

    cancelled = broker.cancel_open_orders("US.NVDA")

    # Order 1 (open limit) cancelled; order 2 (filled) is terminal; order 3 (open
    # STOP) is protective and left alone.
    assert cancelled == 1
    trade = fake_moomoo["created"][0]
    assert trade.list_args["code"] == "US.NVDA"
    assert trade.modify_op == "CANCEL"
    assert trade.modify_order_id == 1
    assert trade.closed is True


def test_cancel_open_orders_leaves_stop_alone(fake_moomoo):
    fake_moomoo["list_data"] = pd.DataFrame(
        {"order_id": [9], "order_status": ["SUBMITTED"], "order_type": ["STOP"]}
    )
    broker = MoomooBroker(trading_env="SIMULATE")

    cancelled = broker.cancel_open_orders("US.NVDA")

    assert cancelled == 0
    assert not hasattr(fake_moomoo["created"][0], "modify_order_id")


def test_cancel_open_orders_skips_all_terminal(fake_moomoo):
    fake_moomoo["list_data"] = pd.DataFrame(
        {"order_id": [1, 2], "order_status": ["FILLED_ALL", "CANCELLED_ALL"]}
    )
    broker = MoomooBroker(trading_env="SIMULATE")

    cancelled = broker.cancel_open_orders("US.NVDA")

    assert cancelled == 0
    trade = fake_moomoo["created"][0]
    assert not hasattr(trade, "modify_order_id")  # nothing resting -> no cancel call


def test_cancel_open_orders_raises_on_list_error(fake_moomoo):
    fake_moomoo["list_ret"] = 1
    fake_moomoo["list_data"] = "list failed"
    broker = MoomooBroker(trading_env="SIMULATE")

    with pytest.raises(RuntimeError, match="order list failed"):
        broker.cancel_open_orders("US.NVDA")


def test_get_account(fake_moomoo):
    broker = MoomooBroker(trading_env="SIMULATE")

    result = broker.get_account()

    assert result == {
        "cash": 10000.0,
        "currency": "USD",
        "total_assets": None,  # column absent in this account snapshot
        "market_value": None,
    }
    trade = fake_moomoo["created"][0]
    assert trade.accinfo_args["trd_env"] == "SIMULATE"
    assert trade.accinfo_args["acc_id"] == 123
    assert trade.closed is True


def test_get_account_includes_worth_when_present(fake_moomoo):
    fake_moomoo["accinfo_data"] = pd.DataFrame(
        {
            "cash": [10000.0],
            "currency": ["USD"],
            "total_assets": [15000.0],
            "market_val": [5000.0],
        }
    )
    broker = MoomooBroker(trading_env="SIMULATE")

    result = broker.get_account()

    assert result["total_assets"] == 15000.0
    assert result["market_value"] == 5000.0


def test_get_account_raises_on_no_account(fake_moomoo):
    fake_moomoo["acc_data"] = pd.DataFrame({"acc_id": [123], "trd_env": ["REAL"]})
    broker = MoomooBroker(trading_env="SIMULATE")

    with pytest.raises(RuntimeError, match="no SIMULATE account"):
        broker.get_account()


def test_get_positions_translates_symbols(fake_moomoo):
    fake_moomoo["pos_data"] = pd.DataFrame(
        {
            "code": ["US.NVDA", "HK.00700", "US.SPOT"],
            "qty": [5, 100, 0],
            "cost_price": [100.0, 50.0, 10.0],
        }
    )
    broker = MoomooBroker(trading_env="SIMULATE")

    result = broker.get_positions()

    assert [(p["symbol"], p["quantity"], p["average_price"]) for p in result] == [
        ("NVDA", 5.0, 100.0),
        ("0700.HK", 100.0, 50.0),
    ]
    assert result[0]["pl_value"] is None  # P/L columns absent in this snapshot
    trade = fake_moomoo["created"][0]
    assert trade.pos_args["trd_env"] == "SIMULATE"
    assert trade.pos_args["acc_id"] == 123


def test_get_positions_includes_pl_when_present(fake_moomoo):
    fake_moomoo["pos_data"] = pd.DataFrame(
        {
            "code": ["US.NVDA"],
            "qty": [5],
            "cost_price": [100.0],
            "nominal_price": [120.0],
            "market_val": [600.0],
            "pl_val": [100.0],
            "pl_ratio": [20.0],
        }
    )
    broker = MoomooBroker(trading_env="SIMULATE")

    (pos,) = broker.get_positions()

    assert pos["current_price"] == 120.0
    assert pos["market_value"] == 600.0
    assert pos["pl_value"] == 100.0
    assert pos["pl_ratio"] == 20.0


def test_get_positions_raises_on_query_error(fake_moomoo):
    fake_moomoo["pos_ret"] = 1
    fake_moomoo["pos_data"] = "position query failed"
    broker = MoomooBroker()

    with pytest.raises(RuntimeError, match="position query failed"):
        broker.get_positions()


# ---- list_orders / cancel_order --------------------------------------------


def _order_rows():
    return pd.DataFrame(
        {
            "order_id": [1, 2],
            "code": ["US.NVDA", "US.AAPL"],
            "trd_side": ["BUY", "SELL"],
            "qty": [2, 3],
            "price": [200.0, 150.0],
            "order_type": ["NORMAL", "NORMAL"],
            "order_status": ["SUBMITTED", "FILLED_ALL"],
        }
    )


def test_list_orders_returns_all_with_yahoo_symbols(fake_moomoo):
    fake_moomoo["list_data"] = _order_rows()
    broker = MoomooBroker(trading_env="SIMULATE")

    orders = broker.list_orders()

    assert [(o["order_id"], o["symbol"], o["side"], o["order_status"]) for o in orders] == [
        (1, "NVDA", "BUY", "SUBMITTED"),
        (2, "AAPL", "SELL", "FILLED_ALL"),
    ]
    assert orders[0]["price"] == 200.0
    assert "code" not in fake_moomoo["created"][0].list_args  # account-wide query


def test_list_orders_open_only_filters_terminal(fake_moomoo):
    fake_moomoo["list_data"] = _order_rows()
    broker = MoomooBroker(trading_env="SIMULATE")

    orders = broker.list_orders(open_only=True)

    assert [o["order_id"] for o in orders] == [1]  # FILLED_ALL dropped


def test_list_orders_by_symbol_passes_code(fake_moomoo):
    fake_moomoo["list_data"] = _order_rows()
    broker = MoomooBroker(trading_env="SIMULATE")

    broker.list_orders("US.NVDA")

    assert fake_moomoo["created"][0].list_args["code"] == "US.NVDA"


def test_list_orders_raises_on_error(fake_moomoo):
    fake_moomoo["list_ret"] = 1
    fake_moomoo["list_data"] = "boom"
    broker = MoomooBroker(trading_env="SIMULATE")

    with pytest.raises(RuntimeError, match="order list failed"):
        broker.list_orders()


def test_cancel_order_cancels_by_id(fake_moomoo):
    broker = MoomooBroker(trading_env="SIMULATE")

    broker.cancel_order(42)

    trade = fake_moomoo["created"][0]
    assert trade.modify_op == "CANCEL"
    assert trade.modify_order_id == 42
    assert trade.closed is True


def test_cancel_order_raises_on_failure(fake_moomoo):
    fake_moomoo["modify_ret"] = 1
    fake_moomoo["modify_data"] = "no such order"
    broker = MoomooBroker(trading_env="SIMULATE")

    with pytest.raises(RuntimeError, match="cancel failed for order 42"):
        broker.cancel_order(42)


def test_status_name_resolves_numeric_enum():
    moomoo = types.SimpleNamespace(
        OrderStatus=types.SimpleNamespace(to_string=lambda v: "FILLED_ALL" if v == 11 else "?")
    )

    assert broker_mod._status_name(11, moomoo) == "FILLED_ALL"
    assert broker_mod._status_name(" submitted ", moomoo) == "SUBMITTED"
    assert broker_mod._status_name(None, moomoo) == ""


def test_list_orders_canonicalizes_numeric_statuses(fake_moomoo, monkeypatch):
    fake_moomoo["list_data"] = pd.DataFrame(
        {
            "order_id": [1],
            "code": ["US.NVDA"],
            "trd_side": ["BUY"],
            "qty": [2],
            "price": [200.0],
            "order_type": ["NORMAL"],
            "order_status": [11],  # numeric enum, not a name
        }
    )
    # Teach the fake SDK to resolve the enum like the real one does.
    ns_status = types.SimpleNamespace(to_string=lambda v: "FILLED_ALL" if v == 11 else "?")

    real_moomoo = broker_mod._moomoo

    def patched():
        ns = real_moomoo()
        ns.OrderStatus = ns_status
        return ns

    monkeypatch.setattr(broker_mod, "_moomoo", patched)
    broker = MoomooBroker(trading_env="SIMULATE")

    (order,) = broker.list_orders()

    assert order["order_status"] == "FILLED_ALL"  # canonical name, not "11"


# ---- multi-account logins (HK-only + US accounts for the same trd_env) -----


def _two_account_fixture(fake_moomoo):
    """An HK-cash paper account (listed first) plus a US-margin one."""
    fake_moomoo["acc_data"] = pd.DataFrame(
        {
            "acc_id": [5804383, 2261948],
            "trd_env": ["SIMULATE", "SIMULATE"],
            "trdmarket_auth": [["HK"], ["US"]],
        }
    )


def test_default_acc_id_prefers_us_over_hk_listed_first(fake_moomoo):
    _two_account_fixture(fake_moomoo)
    broker = MoomooBroker(trading_env="SIMULATE")

    result = broker.get_account()

    trade = fake_moomoo["created"][0]
    assert trade.accinfo_args["acc_id"] == 2261948  # US account, not the first-listed HK one


def test_get_positions_merges_across_every_account(fake_moomoo, monkeypatch):
    _two_account_fixture(fake_moomoo)
    by_acc = {
        5804383: pd.DataFrame({"code": [], "qty": [], "cost_price": []}),
        2261948: pd.DataFrame({"code": ["US.NVDA"], "qty": [1], "cost_price": [159.85]}),
    }

    class TradeCtx:
        def __init__(self, **kw):
            fake_moomoo["created"].append(self)

        def get_acc_list(self):
            return fake_moomoo["acc_ret"], fake_moomoo["acc_data"]

        def position_list_query(self, *, trd_env, acc_id):
            return 0, by_acc[acc_id]

        def close(self):
            self.closed = True

    monkeypatch.setattr(broker_mod._moomoo(), "OpenSecTradeContext", TradeCtx)
    broker = MoomooBroker(trading_env="SIMULATE")

    positions = broker.get_positions()

    # The NVDA position in the *second* (US) account is not lost even though
    # the HK account (queried first) holds nothing.
    assert [p["symbol"] for p in positions] == ["NVDA"]


def test_list_orders_merges_across_every_account(fake_moomoo, monkeypatch):
    _two_account_fixture(fake_moomoo)
    by_acc = {
        5804383: pd.DataFrame(
            {"order_id": [], "code": [], "trd_side": [], "qty": [],
             "price": [], "order_type": [], "order_status": []}
        ),
        2261948: pd.DataFrame(
            {"order_id": [7], "code": ["US.NVDA"], "trd_side": ["BUY"], "qty": [1],
             "price": [160.0], "order_type": ["NORMAL"], "order_status": ["SUBMITTED"]}
        ),
    }

    class TradeCtx:
        def __init__(self, **kw):
            fake_moomoo["created"].append(self)

        def get_acc_list(self):
            return fake_moomoo["acc_ret"], fake_moomoo["acc_data"]

        def order_list_query(self, *, trd_env, acc_id, code=None):
            return 0, by_acc[acc_id]

        def close(self):
            self.closed = True

    monkeypatch.setattr(broker_mod._moomoo(), "OpenSecTradeContext", TradeCtx)
    broker = MoomooBroker(trading_env="SIMULATE")

    orders = broker.list_orders()

    assert [o["order_id"] for o in orders] == [7]


def test_cancel_order_tries_every_account_until_one_succeeds(fake_moomoo, monkeypatch):
    _two_account_fixture(fake_moomoo)
    calls = []

    class TradeCtx:
        def __init__(self, **kw):
            fake_moomoo["created"].append(self)

        def get_acc_list(self):
            return fake_moomoo["acc_ret"], fake_moomoo["acc_data"]

        def modify_order(self, op, order_id, qty, price, *, trd_env, acc_id):
            calls.append(acc_id)
            # Only the US account (queried second) actually holds this order.
            return (0, "ok") if acc_id == 2261948 else (1, "not found on this account")

        def close(self):
            self.closed = True

    monkeypatch.setattr(broker_mod._moomoo(), "OpenSecTradeContext", TradeCtx)
    broker = MoomooBroker(trading_env="SIMULATE")

    broker.cancel_order(7)  # must not raise — the second account accepts it

    assert calls == [5804383, 2261948]


def test_cancel_order_raises_when_no_account_accepts(fake_moomoo, monkeypatch):
    _two_account_fixture(fake_moomoo)

    class TradeCtx:
        def __init__(self, **kw):
            fake_moomoo["created"].append(self)

        def get_acc_list(self):
            return fake_moomoo["acc_ret"], fake_moomoo["acc_data"]

        def modify_order(self, op, order_id, qty, price, **kw):
            return 1, "no such order"

        def close(self):
            self.closed = True

    monkeypatch.setattr(broker_mod._moomoo(), "OpenSecTradeContext", TradeCtx)
    broker = MoomooBroker(trading_env="SIMULATE")

    with pytest.raises(RuntimeError, match="cancel failed for order 7"):
        broker.cancel_order(7)


def test_place_order_rounds_limit_price_to_two_decimals(fake_moomoo):
    """Moomoo rejects a price with too many decimals ("The precision of
    Price in Place Order does not meet the specification")."""
    broker = MoomooBroker(trading_env="SIMULATE")

    broker.place_order("US.NVDA", "BUY", 10.0, price=159.59333333)

    assert fake_moomoo["created"][0].place_args["price"] == 159.59


def test_place_order_rounds_stop_trigger_to_two_decimals(fake_moomoo):
    broker = MoomooBroker(trading_env="SIMULATE")

    broker.place_order("US.NVDA", "SELL", 5.0, order_type="STOP", price=172.567)

    assert fake_moomoo["created"][0].place_args["aux_price"] == 172.57
