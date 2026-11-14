"""Moomoo broker adapter: account, price and order access via the local OpenD gateway.

``moomoo-api`` talks to the OpenD gateway at ``127.0.0.1:11111``. The SDK is
imported lazily inside the methods so this module imports and unit-tests without
it installed. Every call opens a context, does its work, and closes the context
in a ``finally`` so a failed call never leaks a connection.

Orders default to MARKET (the quantity is the only thing sent — TradingAgents
gives no reliable limit price on its own); a caller may pass ``price`` to place
a NORMAL (limit) order instead. Everything runs against an explicit ``trd_env``
(``SIMULATE`` by default, so nothing real can be hit accidentally).
"""

from __future__ import annotations

import math

from execution_agent.symbols import to_yahoo, trd_market


def _moomoo():
    """Import the SDK lazily (an optional extra) so the module is import-light."""
    import moomoo

    return moomoo


# OrderStatus values that are still resting on the book (cancelable): a fresh
# /auto run cancels these before placing its own orders. Filled/cancelled/failed
# states are terminal and left alone.
_OPEN_STATUSES = {"SUBMITTED", "SUBMITTING", "WAITING_SUBMIT", "FILLED_PART"}


def _is_open_status(status, moomoo) -> bool:
    """Whether a broker order-status value is still resting (and thus cancelable)."""
    if status is None:
        return False
    if isinstance(status, str):
        return status.strip().upper() in _OPEN_STATUSES
    # Some SDK paths return the numeric enum; resolve it back to a name.
    try:
        name = moomoo.OrderStatus.to_string(int(status))
    except Exception:
        return False
    return name in _OPEN_STATUSES


def _is_protective_stop(order_type, moomoo) -> bool:
    """Whether an order type is a protective stop — left alone on a fresh run.

    Stale *target* orders (limit buy/sell) go stale day to day and are cancelled;
    a stop-loss protects an existing position, so cancelling it on a fresh "Hold"
    would silently drop that protection.
    """
    if isinstance(order_type, str):
        return order_type.strip().upper() == "STOP"
    try:
        return moomoo.OrderType.to_string(int(order_type)) == "STOP"
    except Exception:
        return False


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

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        *,
        price: float | None = None,
        order_type: str = "MARKET",
        time_in_force: str = "DAY",
    ) -> dict:
        """Place an order and return ``{"order_id", "order_status"}``.

        ``side`` is "BUY" or "SELL"; ``quantity`` is whole units. Defaults to a
        MARKET order (price ignored, sent as 0.0). Pass ``price`` to place a
        NORMAL (limit) order at that price. Pass ``order_type="STOP"`` with a
        ``price`` to place a stop order (e.g. a protective stop-loss) at that
        trigger. ``time_in_force`` is "DAY" (dies at close) or "GTC" (rests until
        filled/cancelled) — the auto loop uses it so a limit survives the agent
        being offline. The caller is responsible for sanity-checking the level
        against the live quote. The account is looked up for ``trading_env`` so a
        SIMULATE run can never touch a REAL account id by accident.
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

            # STOP carries its trigger in ``price``; NORMAL (limit) carries a
            # limit price; MARKET ignores price (send 0.0).
            if order_type == "STOP":
                if price is None:
                    raise ValueError("a STOP order needs a trigger price")
                order_price = float(price)
            elif price is not None:
                order_type = "NORMAL"
                order_price = float(price)
            else:
                order_price = 0.0

            ret, data = trd_ctx.place_order(
                price=order_price,
                qty=int(quantity),
                code=symbol,
                trd_side=trd_side,
                order_type=getattr(moomoo.OrderType, order_type),
                trd_env=trd_env,
                acc_id=acc_id,
                time_in_force=getattr(moomoo.TimeInForce, time_in_force),
            )
            if ret != moomoo.RET_OK:
                raise RuntimeError(f"order failed for {symbol}: {data}")
            return {
                "order_id": int(data["order_id"].iloc[0]),
                "order_status": str(data["order_status"].iloc[0]),
            }
        finally:
            trd_ctx.close()

    def cancel_open_orders(self, symbol: str) -> int:
        """Cancel resting limit (target) orders on ``symbol``; return the count.

        Limit and GTC orders rest on the broker across days. A fresh ``/auto``
        run cancels the prior day's still-open target orders before placing new
        ones so they cannot double-fill or fire against a stale verdict.
        Protective stop-losses are left alone — they guard a position that may
        still be held. Best-effort: a per-order cancel that fails (already
        filled/cancelled elsewhere) is skipped, not raised, so the caller always
        proceeds to today's order.
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
            ret, data = trd_ctx.order_list_query(
                trd_env=trd_env, acc_id=acc_id, code=symbol
            )
            if ret != moomoo.RET_OK:
                raise RuntimeError(f"order list failed for {symbol}: {data}")
            cancelled = 0
            for _, row in data.iterrows():
                if not _is_open_status(row.get("order_status"), moomoo):
                    continue
                if _is_protective_stop(row.get("order_type"), moomoo):
                    continue
                try:
                    order_id = int(row["order_id"])
                except (TypeError, ValueError):
                    continue
                c_ret, _ = trd_ctx.modify_order(
                    moomoo.ModifyOrderOp.CANCEL, order_id, 0, 0,
                    trd_env=trd_env, acc_id=acc_id,
                )
                if c_ret == moomoo.RET_OK:
                    cancelled += 1
            return cancelled
        finally:
            trd_ctx.close()

    def get_account(self) -> dict:
        """The account's cash and currency for the first ``trading_env`` account.

        Returns ``{"cash": float, "currency": str}``. Uses the first account in
        the list (not market-matched), since the whole book is being read here.
        """
        moomoo = _moomoo()
        trd_ctx = moomoo.OpenSecTradeContext(
            filter_trdmarket=moomoo.TrdMarket.NONE,
            host=self.host,
            port=self.port,
        )
        try:
            trd_env = self._trd_env(moomoo)
            acc_id = self._default_acc_id(trd_ctx, trd_env, moomoo)
            ret, data = trd_ctx.accinfo_query(trd_env=trd_env, acc_id=acc_id)
            if ret != moomoo.RET_OK:
                raise RuntimeError(f"account query failed: {data}")
            row = data.iloc[0]
            return {
                "cash": float(row.get("cash")),
                "currency": str(row.get("currency")),
            }
        finally:
            trd_ctx.close()

    def get_positions(self) -> list[dict]:
        """Open positions as ``[{"symbol", "quantity", "average_price"}]``.

        Symbols come back as Moomoo codes (``US.NVDA``) and are translated to
        Yahoo form via :func:`to_yahoo` so they match the watchlist and
        ``PortfolioContext``. Rows with non-positive quantity are skipped.
        """
        moomoo = _moomoo()
        trd_ctx = moomoo.OpenSecTradeContext(
            filter_trdmarket=moomoo.TrdMarket.NONE,
            host=self.host,
            port=self.port,
        )
        try:
            trd_env = self._trd_env(moomoo)
            acc_id = self._default_acc_id(trd_ctx, trd_env, moomoo)
            ret, data = trd_ctx.position_list_query(trd_env=trd_env, acc_id=acc_id)
            if ret != moomoo.RET_OK:
                raise RuntimeError(f"position query failed: {data}")
            positions: list[dict] = []
            for _, row in data.iterrows():
                qty = float(row.get("qty") or 0.0)
                if qty <= 0:
                    continue
                positions.append(
                    {
                        "symbol": to_yahoo(str(row["code"])),
                        "quantity": qty,
                        "average_price": float(row.get("cost_price") or 0.0),
                    }
                )
            return positions
        finally:
            trd_ctx.close()

    def _default_acc_id(self, trd_ctx, trd_env, moomoo) -> int:
        """The first account id for ``trd_env``, regardless of market.

        Shared by the account/position readers, which span markets rather than
        targeting a single symbol.
        """
        ret, data = trd_ctx.get_acc_list()
        if ret != moomoo.RET_OK:
            raise RuntimeError(f"could not list accounts: {data}")
        matching = data[data["trd_env"] == trd_env]
        if matching.empty:
            raise RuntimeError(
                f"no {self.trading_env} account found — is OpenD logged in with one?"
            )
        return int(matching.iloc[0]["acc_id"])

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
