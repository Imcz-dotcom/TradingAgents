"""Chat prompt assembly: transcript sections and cross-ticker history labelling."""

from execution_agent import chat


def test_research_transcript_includes_all_sections():
    state = {
        "instrument_context": "NVDA",
        "market_report": "Market up.",
        "sentiment_report": "Bullish.",
        "news_report": "Positive.",
        "fundamentals_report": "Strong.",
        "trader_investment_plan": "Plan.",
        "investment_plan": "Buy.",
        "final_trade_decision": "Buy",
        "past_context": "Lessons.",
        "investment_debate_state": {"bull_history": "Bull.", "bear_history": "Bear."},
        "risk_debate_state": {
            "aggressive_history": "Agg.",
            "conservative_history": "Cons.",
            "neutral_history": "Neut.",
        },
    }
    transcript = chat.research_transcript(state)
    for section in (
        "Instrument",
        "Market analysis",
        "Bull/bear debate",
        "Risk debate",
        "Final decision",
        "Past lessons",
    ):
        assert section in transcript


def test_research_transcript_skips_empty():
    assert chat.research_transcript({}) == ""
    assert chat.research_transcript({"market_report": None}) == ""


def test_build_prompt_labels_cross_ticker_history():
    state = {"final_trade_decision": "Buy NVDA"}
    history = [
        {"ticker": "INTC", "q": "what now?", "a": "hold"},
        {"ticker": "NVDA", "q": "risk?", "a": "concentration"},
    ]
    prompt = chat.build_prompt({"NVDA": state}, "NVDA", history, "confirm?")
    assert "Q (about INTC): what now?" in prompt
    assert "Q: risk?" in prompt
    assert "A: concentration" in prompt


def test_build_prompt_includes_other_tickers_digest():
    researches = {
        "NVDA": {"final_trade_decision": "**Rating**: Buy\n\n**Executive Summary**: Buy it.\n\n**Investment Thesis**: nvidia thesis"},
        "AAPL": {"final_trade_decision": "**Rating**: Sell\n\n**Executive Summary**: Avoid it.\n\n**Investment Thesis**: apple thesis"},
    }
    prompt = chat.build_prompt(researches, "NVDA", [], "why skip AAPL?")
    assert "## Other tickers researched today" in prompt
    assert "### AAPL" in prompt
    assert "**Rating**: Sell" in prompt
    assert "Avoid it." in prompt
    # The other ticker's long thesis is dropped from the digest; the target's is kept.
    assert "apple thesis" not in prompt
    assert "nvidia thesis" in prompt
