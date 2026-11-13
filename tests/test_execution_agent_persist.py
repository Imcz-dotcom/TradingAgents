"""Per-day history persistence: JSON-safe subset and save/load round-trip."""

from execution_agent import persist


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
