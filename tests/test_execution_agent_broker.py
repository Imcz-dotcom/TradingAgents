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

        def close(self):
            self.closed = True

    ns = types.SimpleNamespace(
        RET_OK=0,
        TrdEnv=types.SimpleNamespace(SIMULATE="SIMULATE", REAL="REAL"),
        TrdSide=types.SimpleNamespace(BUY="BUY", SELL="SELL"),
        OrderType=types.SimpleNamespace(MARKET="MARKET"),
        TrdMarket=types.SimpleNamespace(NONE="N/A"),
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
    assert args["qty"] == 10            # float 10.0 -> int 10
    assert args["order_type"] == "MARKET"
    assert args["trd_env"] == "SIMULATE"
    assert args["acc_id"] == 123
    assert args["price"] == 0.0         # market order ignores price
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
