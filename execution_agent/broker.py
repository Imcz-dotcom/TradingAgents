"""Moomoo broker adapter: fetch a price and place an order via the local OpenD gateway.

``moomoo-api`` talks to the OpenD gateway at ``127.0.0.1:11111``. The SDK is
imported lazily inside the methods so this module imports and unit-tests without
it installed. Every call opens a context, does its work, and closes the context
in a ``finally`` so a failed call never leaks a connection.

Orders are MARKET only (the quantity is the only thing sent — TradingAgents gives
no reliable limit price), and always placed against an explicit ``trd_env``
(``SIMULATE`` by default, so nothing real can be hit accidentally).
"""

from __future__ import annotations

import math

from execution_agent.symbols import trd_market


def _moomoo():
    """Import the SDK lazily (an optional extra) so the module is import-light."""
    import moomoo

    return moomoo


class MoomooBroker:
    """A thin, connection-per-call wrapper over OpenD's trade and quote contexts."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 11111, trading_env: str = "SIMULATE"):
        self.host = host
        self.port = port
        self.trading_env = trading_env  # "SIMULATE" (paper) or "REAL"

    def _trd_env(self, moomoo):
        return getattr(moomoo.TrdEnv, self.trading_env)

    def last_price(self, symbol: str) -> float:
        """The last traded price of ``symbol``, or a clear error when unavailable."""
        moomoo = _moomoo()
        quote_ctx = moomoo.OpenQuoteContext(host=self.host, port=self.port)
        try:
            ret, data = quote_ctx.get_market_snapshot([symbol])
            if ret != moomoo.RET_OK:
                raise RuntimeError(f"quote failed for {symbol}: {data}")
            try:
                price = float(data["last_price"].iloc[0])
            except (TypeError, ValueError, IndexError) as exc:
                raise RuntimeError(f"no last price returned for {symbol}") from exc
            if math.isnan(price) or price <= 0:
                raise RuntimeError(f"no usable last price for {symbol}")
            return price
        finally:
            quote_ctx.close()

    def place_order(self, symbol: str, side: str, quantity: float) -> dict:
        """Place a MARKET order and return ``{"order_id", "order_status"}``.

        ``side`` is "BUY" or "SELL"; ``quantity`` is whole units. The account is
        looked up for ``trading_env`` so a SIMULATE run can never touch a REAL
        account id by accident.
        """
        moomoo = _moomoo()
        trd_ctx = moomoo.OpenSecTradeContext(
            filter_trdmarket=moomoo.TrdMarket.NONE,
            host=self.host,
            port=self.port,
        )
        try:
            trd_env = self._trd_env(moomoo)
            acc_id = self._resolve_acc_id(trd_ctx, trd_env, moomoo, symbol)
            trd_side = moomoo.TrdSide.BUY if side == "BUY" else moomoo.TrdSide.SELL

            # MARKET order: price is ignored by the broker, so send 0.0.
            ret, data = trd_ctx.place_order(
                price=0.0,
                qty=int(quantity),
                code=symbol,
                trd_side=trd_side,
                order_type=moomoo.OrderType.MARKET,
                trd_env=trd_env,
                acc_id=acc_id,
            )
            if ret != moomoo.RET_OK:
                raise RuntimeError(f"order failed for {symbol}: {data}")
            return {
                "order_id": int(data["order_id"].iloc[0]),
                "order_status": str(data["order_status"].iloc[0]),
            }
        finally:
            trd_ctx.close()

    def _resolve_acc_id(self, trd_ctx, trd_env, moomoo, symbol) -> int:
        """The account id for ``trd_env`` (SIMULATE/REAL), or a clear error.

        Prefers an account whose ``trdmarket_auth`` covers ``symbol``'s market.
        A multi-account login often lists a HK/CN paper account first, which
        would otherwise be chosen for a US order and rejected by the broker with
        "does not support trading US.NVDA". If no account lists the market, the
        error names every account and its markets instead of leaving the user to
        guess.
        """
        ret, data = trd_ctx.get_acc_list()
        if ret != moomoo.RET_OK:
            raise RuntimeError(f"could not list accounts: {data}")
        matching = data[data["trd_env"] == trd_env]
        if matching.empty:
            raise RuntimeError(
                f"no {self.trading_env} account found — is OpenD logged in with one?"
            )
        market = trd_market(symbol)
        for _, row in matching.iterrows():
            if market in (row.get("trdmarket_auth") or []):
                return int(row["acc_id"])
        accounts = ", ".join(
            f"{int(r['acc_id'])} ({', '.join(map(str, r.get('trdmarket_auth') or ['none']))})"
            for _, r in matching.iterrows()
        )
        raise RuntimeError(
            f"no {self.trading_env} account supports {symbol} (market {market}); "
            f"available accounts: {accounts}"
        )
