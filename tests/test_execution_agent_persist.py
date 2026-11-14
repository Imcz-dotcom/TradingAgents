"""Per-day history persistence: JSON-safe subset and save/load round-trip."""

from datetime import date

from execution_agent import persist
from execution_agent.decision import DecisionResult


def _final_state():
    return {
        "instrument_context": "NVDA — semiconductor",
        "market_report": "Market is up.",
        "sentiment_report": "Bullish.",
        "news_report": "Positive.",
        "fundamentals_report": "Strong.",
        "trader_investment_plan": "Hold through volatility.",
        "investment_plan": "Buy a starter position.",
        "final_trade_decision": "Rating: Buy",
        "past_context": "Last time we sold too early.",
        "investment_debate_state": {
            "bull_history": "The bull case.",
            "bear_history": "The bear case.",
            "history": [{"not": "json"}],  # dropped
            "current_response": "irrelevant",
        },
        "risk_debate_state": {
            "aggressive_history": "Aggressive.",
            "conservative_history": "Conservative.",
            "neutral_history": "Neutral.",
        },
        "messages": ["not json safe"],  # dropped entirely
    }


def test_research_subset_keeps_only_json_safe_keys():
    subset = persist.research_subset(_final_state())

    assert subset["market_report"] == "Market is up."
    assert subset["final_trade_decision"] == "Rating: Buy"
    assert subset["investment_debate_state"] == {
        "bull_history": "The bull case.",
        "bear_history": "The bear case.",
    }
    assert subset["risk_debate_state"]["neutral_history"] == "Neutral."
    assert "messages" not in subset
    assert "history" not in subset.get("investment_debate_state", {})


def test_save_and_load_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(persist, "history_path", lambda: str(tmp_path / "hist.json"))
    day = {
        "date": "2026-11-14",
        "researches": {"NVDA": {"executed": False, "decision": {"research": {}}}},
        "chat_history": [{"ticker": "NVDA", "q": "q", "a": "a"}],
    }

    persist.save_history(day)

    assert persist.load_history() == day


def test_load_missing_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(persist, "history_path", lambda: str(tmp_path / "nope.json"))

    assert persist.load_history() is None


def test_load_corrupt_returns_none(monkeypatch, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    monkeypatch.setattr(persist, "history_path", lambda: str(path))

    assert persist.load_history() is None


# --- same-day reuse (load_today_decision) ---------------------------------


def _entry(yahoo="NVDA", moomoo="US.NVDA", signal="Buy", side="BUY"):
    return {
        "executed": False,
        "decision": {
            "yahoo": yahoo,
            "moomoo": moomoo,
            "signal": signal,
            "side": side,
            "summary": "buy it",
            "cash": None,
            "position_quantity": None,
            "research": {"investment_plan": "**Price Target**: 200.0\n\n**Time Horizon**: 3-6 months"},
        },
    }


def _save(monkeypatch, tmp_path, researches, *, date_str=None):
    monkeypatch.setattr(persist, "history_path", lambda: str(tmp_path / "hist.json"))
    persist.save_history(
        {"date": date_str or date.today().isoformat(), "researches": researches, "chat_history": []}
    )


def test_decision_from_entry_rebuilds():
    d = persist.decision_from_entry(_entry()["decision"])

    assert isinstance(d, DecisionResult)
    assert d.yahoo == "NVDA"
    assert d.signal == "Buy"
    assert d.research["investment_plan"].startswith("**Price Target**")


def test_decision_from_entry_malformed_is_none():
    assert persist.decision_from_entry(None) is None
    assert persist.decision_from_entry({"yahoo": "NVDA"}) is None  # missing keys


def test_load_today_decision_finds_today(monkeypatch, tmp_path):
    _save(monkeypatch, tmp_path, {"NVDA": _entry()})

    d = persist.load_today_decision("NVDA")

    assert d is not None and d.yahoo == "NVDA"


def test_load_today_decision_matches_by_yahoo_not_key(monkeypatch, tmp_path):
    _save(monkeypatch, tmp_path, {"some_key": _entry(yahoo="NVDA")})

    assert persist.load_today_decision("NVDA") is not None


def test_load_today_decision_stale_date_is_none(monkeypatch, tmp_path):
    _save(monkeypatch, tmp_path, {"NVDA": _entry()}, date_str="2000-01-01")

    assert persist.load_today_decision("NVDA") is None


def test_load_today_decision_unknown_ticker_is_none(monkeypatch, tmp_path):
    _save(monkeypatch, tmp_path, {"NVDA": _entry()})

    assert persist.load_today_decision("AAPL") is None
