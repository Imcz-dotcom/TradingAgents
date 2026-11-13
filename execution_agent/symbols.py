"""Yahoo -> Moomoo symbol translation for the execution agent.

``tradingagents.dataflows.symbols.normalize_symbol`` produces canonical Yahoo
Finance symbols (``NVDA``, ``0700.HK``, ``600519.SS``). Moomoo, via the local
OpenD gateway, wants its own ``<MARKET>.<CODE>`` form:

    Yahoo            Moomoo        notes
    ---------------- ------------- ------------------------------------
    NVDA             US.NVDA       no suffix -> US equity/ETF
    0700.HK          HK.00700      Yahoo pads HK to 4 digits; Moomoo 5
    600519.SS        SH.600519     Yahoo spells Shanghai ``.SS``
    000001.SZ        SZ.000001     Shenzhen passes through
    7203.T           JP.7203       Tokyo
    VOD.L            UK.VOD        London
    SHOP.TO          CA.SHOP       Toronto
    BHP.AX           AU.BHP        Australia
    D05.SI           SG.D05        Singapore
    005930.KS        KR.005930     Korea

Only plain equities and ETFs are supported in v1. Crypto, forex, futures, and
indices are rejected with a clear ``ValueError`` rather than silently mapped to
a symbol Moomoo would either not find or interpret differently. This is purely
syntactic — no network calls — so it is safe to apply on every request.
"""

from __future__ import annotations

import logging
import re

from tradingagents.dataflows.symbols import crypto_base, normalize_symbol

logger = logging.getLogger(__name__)

# Yahoo market suffix -> Moomoo market code. The empty suffix is a plain US
# equity/ETF; every other entry is a dot-suffixed Yahoo symbol. Extend by adding
# a row — no call-site changes required.
_MARKET_BY_SUFFIX = {
    "": "US",
    "HK": "HK",
    "SS": "SH",  # Yahoo spells Shanghai ".SS" (the broker form is ".SH")
    "SZ": "SZ",
    "T": "JP",
    "L": "UK",
    "TO": "CA",
    "AX": "AU",
    "SI": "SG",
    "KS": "KR",
}

# A trailing ``.<1-2 letters>`` on the canonical Yahoo symbol is the market
# suffix. Anything with a suffix not in _MARKET_BY_SUFFIX is rejected.
_SUFFIX_RE = re.compile(r"^(?P<base>.+)\.(?P<suffix>[A-Z]{1,2})$")


def to_moomoo(symbol: str) -> str:
    """Translate a Yahoo symbol (or raw user input) to Moomoo ``MARKET.CODE``.

    The input is normalised through ``normalize_symbol`` first, so both the
    broker form (``600519.SH``) and the Yahoo form (``600519.SS``) resolve to
    ``SH.600519``. Raises ``ValueError`` for crypto, forex, futures, indices,
    and any suffix without a Moomoo mapping.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError(f"symbol must be a non-empty string, got {symbol!r}")

    canonical = normalize_symbol(symbol)

    if crypto_base(canonical) is not None:
        raise ValueError(f"crypto is not supported for execution: {canonical!r}")
    if canonical.endswith("=X"):
        raise ValueError(f"forex is not supported for execution: {canonical!r}")
    if canonical.endswith("=F"):
        raise ValueError(f"futures are not supported for execution: {canonical!r}")
    if canonical.startswith("^"):
        raise ValueError(f"indices are not supported for execution: {canonical!r}")

    suffix = ""
    base = canonical
    if m := _SUFFIX_RE.match(canonical):
        base, suffix = m.group("base"), m.group("suffix")

    market = _MARKET_BY_SUFFIX.get(suffix)
    if market is None:
        raise ValueError(
            f"no Moomoo market mapping for symbol {canonical!r} (suffix {suffix!r})"
        )

    # Moomoo pads HK codes to 5 digits; Yahoo's 4-digit form is not accepted.
    if market == "HK":
        if not base.isdigit():
            raise ValueError(f"invalid HK symbol {canonical!r}: HK codes must be numeric")
        code = f"{int(base):05d}"
    else:
        code = base

    result = f"{market}.{code}"
    if result != canonical:
        logger.info("Mapped Yahoo symbol %r to Moomoo symbol %r", canonical, result)
    return result


# Moomoo reports each account's tradable markets via ``trdmarket_auth`` using
# TrdMarket strings ("US", "HK", "CN", "JP", ...). China A-shares are one market
# there ("CN") even though the broker symbol spells Shanghai/Shenzhen separately.
_TRD_MARKET_BY_PREFIX = {
    "SH": "CN",
    "SZ": "CN",
}


def trd_market(moomoo_symbol: str) -> str:
    """The Moomoo market code (a TrdMarket string) for a ``MARKET.CODE`` symbol.

    ``US.NVDA`` -> ``"US"``, ``HK.00700`` -> ``"HK"``, ``SH.600519`` -> ``"CN"``.
    Used to pick the account that can actually trade a symbol: a multi-account
    login often lists a HK/CN paper account first, which would otherwise be
    selected for a US order and rejected with "does not support trading".
    """
    prefix = str(moomoo_symbol).split(".", 1)[0].upper()
    return _TRD_MARKET_BY_PREFIX.get(prefix, prefix)
