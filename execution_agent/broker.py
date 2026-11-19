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
from contextlib import contextmanager

from execution_agent.symbols import to_yahoo, trd_market


def _moomoo():
    """Import the SDK lazily (an optional extra) so the module is import-light."""
    import moomoo

    return moomoo


# OrderStatus values that are still resting on the book (cancelable): a fresh
# /auto run cancels these before placing its own orders. Filled/cancelled/failed
# states are terminal and left alone.
OPEN_STATUSES = {"SUBMITTED", "SUBMITTING", "WAITING_SUBMIT", "FILLED_PART"}

# Market preference order for picking one "default" account out of several for
# the same trd_env (see MoomooBroker._default_acc_id). US first since the
# agent's tickers are Yahoo/US-centric by default.
_PREFERRED_MARKETS = ("US", "HK", "CN", "SG", "MY", "JP", "AU")


def _opt_float(value) -> float | None:
    """A float, or None when the broker omitted the column or sent junk/NaN."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def _status_name(status, moomoo) -> str:
    """A broker order-status value as its canonical name (e.g. ``SUBMITTED``).

    Some SDK paths return the numeric enum instead of the name; resolving it
    here means every consumer (open-only filters, the serve bot's fill watcher)
    compares one canonical spelling. Unresolvable values come back as ``""``.
    """
    if status is None:
        return ""
    if isinstance(status, str):
        return status.strip().upper()
    try:
        return str(moomoo.OrderStatus.to_string(int(status)))
    except Exception:
        return ""


def _is_open_status(status, moomoo) -> bool:
    """Whether a broker order-status value is still resting (and thus cancelable)."""
    return _status_name(status, moomoo) in OPEN_STATUSES


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

    @contextmanager
    def _trade_ctx(self):
        """Open a trade context for one call, yielding ``(moomoo, ctx, trd_env)``.

        Every trade method shares this connect/close ritual; keeping it in one
        place means a cross-cutting change (timeout, retry, market filter)
        lands once instead of in each method.
        """
        moomoo = _moomoo()
        trd_ctx = moomoo.OpenSecTradeContext(
            filter_trdmarket=moomoo.TrdMarket.NONE,
            host=self.host,
            port=self.port,
        )
        try:
            yield moomoo, trd_ctx, self._trd_env(moomoo)
        finally:
            trd_ctx.close()

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

        ``side`` is "BUY" or "SELL"; ``quantity`` is in units of the instrument
        (most callers compute whole shares, but a full exit or bracket on a
        fractional holding — e.g. a dust remainder from a split/spin-off —
        passes that fractional quantity straight through; Moomoo's own SDK
        casts ``qty`` to ``float``, so this never truncates it to zero).
        Defaults to a MARKET order (price ignored, sent as 0.0). Pass ``price`` to place a
        NORMAL (limit) order at that price. Pass ``order_type="STOP"`` with a
        ``price`` to place a stop order (e.g. a protective stop-loss) at that
        trigger. ``time_in_force`` is "DAY" (dies at close) or "GTC" (rests until
        filled/cancelled) — the auto loop uses it so a limit survives the agent
        being offline. The caller is responsible for sanity-checking the level
        against the live quote. The account is looked up for ``trading_env`` so a
        SIMULATE run can never touch a REAL account id by accident.

        Moomoo's paper (SIMULATE) environment rejects GTC outright — "Paper
        trading does not support GTC orders" — so a GTC request is silently
        downgraded to DAY here when not trading REAL. This affects paper only:
        the order still gets replaced on every fresh ``/auto`` run regardless
        (stale orders are cancelled and reissued each time), so losing true
        multi-day persistence in paper costs nothing functionally — it just
        means a paper order may need the next day's exchange session to still
        be resting, whereas DAY dies at that day's close either way.
        """
        with self._trade_ctx() as (moomoo, trd_ctx, trd_env):
            if time_in_force == "GTC" and self.trading_env != "REAL":
                time_in_force = "DAY"
            acc_id = self._resolve_acc_id(trd_ctx, trd_env, moomoo, symbol)
            trd_side = moomoo.TrdSide.BUY if side == "BUY" else moomoo.TrdSide.SELL

            # A plain STOP (stop-market) has no limit component — its trigger
            # goes in Moomoo's dedicated ``aux_price`` parameter, not ``price``
            # (that confusion is exactly what produced "missing required
            # parameter Trigger price" from Moomoo's own API: ``price`` is the
            # limit leg, meaningless for a stop-market order). NORMAL (limit)
            # carries its level in ``price``; MARKET ignores price (send 0.0).
            aux_price = None
            if order_type == "STOP":
                if price is None:
                    raise ValueError("a STOP order needs a trigger price")
                aux_price = round(float(price), 2)
                order_price = 0.0
            elif price is not None:
                order_type = "NORMAL"
                order_price = round(float(price), 2)
            else:
                order_price = 0.0

            ret, data = trd_ctx.place_order(
                price=order_price,
                qty=float(quantity),
                code=symbol,
                trd_side=trd_side,
                order_type=getattr(moomoo.OrderType, order_type),
                trd_env=trd_env,
                acc_id=acc_id,
                time_in_force=getattr(moomoo.TimeInForce, time_in_force),
                aux_price=aux_price,
            )
            if ret != moomoo.RET_OK:
                raise RuntimeError(f"order failed for {symbol}: {data}")
            return {
                "order_id": int(data["order_id"].iloc[0]),
                "order_status": str(data["order_status"].iloc[0]),
            }

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
        with self._trade_ctx() as (moomoo, trd_ctx, trd_env):
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

    def get_account(self) -> dict:
        """The account's cash, currency and worth for the preferred ``trading_env`` account.

        Returns ``{"cash", "currency", "total_assets", "market_value"}`` — the
        last two are ``None`` when the broker omits them. A multi-market login
        (e.g. a separate HK-only and US paper account) reports one account's
        figures, not a sum — cash in different currencies can't be added
        meaningfully — so :meth:`_default_acc_id` is biased toward the account
        that actually trades (see its docstring).

        ``accinfo_query``'s own SDK default converts every figure to HKD
        regardless of which account/market is queried; TradingAgents' tickers
        are USD-centric, so USD is requested explicitly here.
        """
        with self._trade_ctx() as (moomoo, trd_ctx, trd_env):
            acc_id = self._default_acc_id(trd_ctx, trd_env, moomoo)
            ret, data = trd_ctx.accinfo_query(
                trd_env=trd_env, acc_id=acc_id, currency=moomoo.Currency.USD
            )
            if ret != moomoo.RET_OK:
                raise RuntimeError(f"account query failed: {data}")
            row = data.iloc[0]
            return {
                "cash": float(row.get("cash")),
                "currency": str(row.get("currency")),
                "total_assets": _opt_float(row.get("total_assets")),
                "market_value": _opt_float(row.get("market_val")),
            }

    def get_positions(self) -> list[dict]:
        """Open positions with cost, worth and profit/loss, across every account.

        Returns ``[{"symbol", "quantity", "average_price", "current_price",
        "market_value", "pl_value", "pl_ratio"}]`` — the last four are ``None``
        when the broker omits them. Symbols come back as Moomoo codes
        (``US.NVDA``) and are translated to Yahoo form via :func:`to_yahoo` so
        they match the watchlist and ``PortfolioContext``. Rows with
        non-positive quantity are skipped.

        A multi-market login can split holdings across several accounts for the
        same ``trd_env`` (e.g. an HK-only paper account alongside a US one);
        reading only one would silently hide positions held in the other, so
        every matching account is queried and the results combined.
        """
        with self._trade_ctx() as (moomoo, trd_ctx, trd_env):
            positions: list[dict] = []
            for acc_id in self._env_accounts(trd_ctx, trd_env, moomoo)["acc_id"]:
                ret, data = trd_ctx.position_list_query(
                    trd_env=trd_env, acc_id=int(acc_id)
                )
                if ret != moomoo.RET_OK:
                    raise RuntimeError(f"position query failed: {data}")
                for _, row in data.iterrows():
                    qty = float(row.get("qty") or 0.0)
                    if qty <= 0:
                        continue
                    positions.append(
                        {
                            "symbol": to_yahoo(str(row["code"])),
                            "quantity": qty,
                            "average_price": float(row.get("cost_price") or 0.0),
                            "current_price": _opt_float(row.get("nominal_price")),
                            "market_value": _opt_float(row.get("market_val")),
                            "pl_value": _opt_float(row.get("pl_val")),
                            "pl_ratio": _opt_float(row.get("pl_ratio")),
                        }
                    )
            return positions

    def list_orders(self, symbol: str | None = None, *, open_only: bool = False) -> list[dict]:
        """Today's orders (optionally only the still-resting ones), across every account.

        Returns ``[{"order_id", "symbol" (Yahoo form), "side", "quantity",
        "price", "order_type", "order_status"}]``. ``open_only=True`` keeps just
        the orders still resting on the book (cancelable); the full list also
        carries filled/cancelled orders, which the serve bot's fill-watcher
        diffs against to notice "your order filled" transitions. Every account
        for ``trd_env`` is queried (see :meth:`get_positions` for why), so an
        order placed on a non-default account is never missed.
        """
        with self._trade_ctx() as (moomoo, trd_ctx, trd_env):
            orders: list[dict] = []
            for acc_id in self._env_accounts(trd_ctx, trd_env, moomoo)["acc_id"]:
                kwargs = {"trd_env": trd_env, "acc_id": int(acc_id)}
                if symbol is not None:
                    kwargs["code"] = symbol
                ret, data = trd_ctx.order_list_query(**kwargs)
                if ret != moomoo.RET_OK:
                    raise RuntimeError(f"order list failed: {data}")
                for _, row in data.iterrows():
                    # Canonical status name (numeric enums resolved), so
                    # consumers like the fill watcher compare one spelling.
                    status = _status_name(row.get("order_status"), moomoo)
                    if open_only and status not in OPEN_STATUSES:
                        continue
                    try:
                        order_id = int(row["order_id"])
                    except (TypeError, ValueError):
                        continue
                    code = str(row.get("code") or "")
                    try:
                        yahoo = to_yahoo(code)
                    except ValueError:
                        yahoo = code
                    orders.append(
                        {
                            "order_id": order_id,
                            "symbol": yahoo,
                            "side": str(row.get("trd_side") or ""),
                            "quantity": float(row.get("qty") or 0.0),
                            "price": _opt_float(row.get("price")),
                            "order_type": str(row.get("order_type") or ""),
                            "order_status": status,
                        }
                    )
            return orders

    def cancel_order(self, order_id: int) -> None:
        """Cancel one resting order by id, raising a clear error on failure.

        The order may rest on any of this ``trd_env``'s accounts (see
        :meth:`get_positions`), so a cancel is attempted against each in turn
        and stops at the first one Moomoo accepts.
        """
        with self._trade_ctx() as (moomoo, trd_ctx, trd_env):
            last_data = None
            for acc_id in self._env_accounts(trd_ctx, trd_env, moomoo)["acc_id"]:
                ret, data = trd_ctx.modify_order(
                    moomoo.ModifyOrderOp.CANCEL, int(order_id), 0, 0,
                    trd_env=trd_env, acc_id=int(acc_id),
                )
                if ret == moomoo.RET_OK:
                    return
                last_data = data
            raise RuntimeError(f"cancel failed for order {order_id}: {last_data}")

    def _env_accounts(self, trd_ctx, trd_env, moomoo):
        """The account rows for ``trd_env``, or a clear error when none exist."""
        ret, data = trd_ctx.get_acc_list()
        if ret != moomoo.RET_OK:
            raise RuntimeError(f"could not list accounts: {data}")
        matching = data[data["trd_env"] == trd_env]
        if matching.empty:
            raise RuntimeError(
                f"no {self.trading_env} account found — is OpenD logged in with one?"
            )
        return matching

    def _default_acc_id(self, trd_ctx, trd_env, moomoo) -> int:
        """A single account id for ``trd_env``, for callers that need just one.

        Prefers the first account whose ``trdmarket_auth`` covers a market in
        ``_PREFERRED_MARKETS`` (US first — TradingAgents tickers are
        Yahoo/US-centric by default); falls back to the very first account
        when none of those markets match. A multi-market login often lists a
        non-trading HK/CN cash account before the one actually in use, so
        picking blindly (as this used to) can silently point ``get_account``'s
        single cash figure at the wrong account.
        """
        matching = self._env_accounts(trd_ctx, trd_env, moomoo)
        for market in _PREFERRED_MARKETS:
            for _, row in matching.iterrows():
                if market in (row.get("trdmarket_auth") or []):
                    return int(row["acc_id"])
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
        matching = self._env_accounts(trd_ctx, trd_env, moomoo)
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
