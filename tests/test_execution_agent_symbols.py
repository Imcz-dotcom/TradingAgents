"""Yahoo -> Moomoo symbol translation (execution_agent.symbols.to_moomoo)."""

import pytest

from execution_agent.symbols import to_moomoo, to_yahoo, trd_market


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        # US (no suffix)
        ("NVDA", "US.NVDA"),
        ("nvda", "US.NVDA"),
        ("AAPL", "US.AAPL"),
        ("SPY", "US.SPY"),
        # HK: Yahoo 4-digit -> Moomoo 5-digit
        ("0700.HK", "HK.00700"),
        ("700.HK", "HK.00700"),
        ("09992.HK", "HK.09992"),
        # China A-shares: Shanghai and Shenzhen
        ("600519.SS", "SH.600519"),
        ("600519.SH", "SH.600519"),  # broker form normalizes to .SS first
        ("000001.SZ", "SZ.000001"),
        # Other markets
        ("7203.T", "JP.7203"),
        ("VOD.L", "UK.VOD"),
        ("SHOP.TO", "CA.SHOP"),
        ("BHP.AX", "AU.BHP"),
        ("D05.SI", "SG.D05"),
        ("005930.KS", "KR.005930"),
    ],
)
def test_to_moomoo_maps_yahoo_to_moomoo(symbol, expected):
    assert to_moomoo(symbol) == expected


@pytest.mark.parametrize(
    "symbol",
    [
        # Crypto
        "BTC-USD",
        "BTCUSD",
        "ETHUSDT",
        # Forex (normalizes to a =X pair)
        "EURUSD",
        # Futures
        "GC=F",
        "XAUUSD",  # normalizes to GC=F via the alias table
        # Indices
        "^GSPC",
        "SPX500",  # normalizes to ^GSPC via the alias table
        # Unknown market suffix
        "XYZ.ZZ",
        # HK codes must be numeric
        "ABC.HK",
        # Non-strings / blank
        "",
        "   ",
    ],
)
def test_to_moomoo_rejects_unsupported(symbol):
    with pytest.raises(ValueError):
        to_moomoo(symbol)


def test_to_moomoo_rejects_non_string():
    with pytest.raises(ValueError):
        to_moomoo(None)


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("US.NVDA", "US"),
        ("HK.00700", "HK"),
        ("SH.600519", "CN"),  # China A-shares collapse to one Moomoo market
        ("SZ.000001", "CN"),
        ("JP.7203", "JP"),
        ("SG.D05", "SG"),
        ("UK.VOD", "UK"),
    ],
)
def test_trd_market(symbol, expected):
    assert trd_market(symbol) == expected


@pytest.mark.parametrize(
    ("moomoo_symbol", "expected"),
    [
        ("US.NVDA", "NVDA"),
        ("HK.00700", "0700.HK"),
        ("SH.600519", "600519.SS"),
        ("SZ.000001", "000001.SZ"),
        ("JP.7203", "7203.T"),
        ("UK.VOD", "VOD.L"),
        ("CA.SHOP", "SHOP.TO"),
        ("AU.BHP", "BHP.AX"),
        ("SG.D05", "D05.SI"),
        ("KR.005930", "005930.KS"),
    ],
)
def test_to_yahoo_maps_moomoo_back(moomoo_symbol, expected):
    assert to_yahoo(moomoo_symbol) == expected


@pytest.mark.parametrize(
    "moomoo_symbol",
    [
        "NVDA",  # no market prefix
        "",
        "XX.NVDA",  # unknown market
        None,
    ],
)
def test_to_yahoo_rejects_unknown(moomoo_symbol):
    with pytest.raises(ValueError):
        to_yahoo(moomoo_symbol)
