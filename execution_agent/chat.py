"""Chat with a finished research run: answer questions about it in one LLM call.

The research graph is a multi-agent pipeline; this is deliberately *not* that.
It is a single deep-tier LLM given the full research — all four analyst reports,
the bull/bear debate, the trader's plan, the risk debate, the investment plan and
the final decision — plus the running Q&A, and asked to answer one question. The
LLM client is imported lazily so this module imports and unit-tests cleanly
without an API key.
"""

from __future__ import annotations

from tradingagents.default_config import DEFAULT_CONFIG

_llm = None


def _deep_llm():
    """The deep-tier model, built once and cached for the bot's lifetime."""
    global _llm
    if _llm is None:
        from tradingagents.llm_clients import create_tier_client

        _llm = create_tier_client(DEFAULT_CONFIG, "deep").get_llm()
    return _llm


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def research_transcript(final_state: dict) -> str:
    """The full research as one text block the chat agent can reason over.

    ``final_state`` is the graph's finished state (the same dict
    ``DecisionResult.research`` holds). Every meaningful section is included, so
    a question can be answered against the actual reports and debates rather
    than the one-line summary alone.
    """
    if not final_state:
        return ""
    parts: list[str] = []

    def add(title: str, body) -> None:
        body = _text(body)
        if body:
            parts.append(f"## {title}\n{body}")

    add("Instrument", final_state.get("instrument_context"))
    add("Market analysis", final_state.get("market_report"))
    add("Sentiment analysis", final_state.get("sentiment_report"))
    add("News analysis", final_state.get("news_report"))
    add("Fundamentals analysis", final_state.get("fundamentals_report"))

    debate = final_state.get("investment_debate_state") or {}
    bull, bear = _text(debate.get("bull_history")), _text(debate.get("bear_history"))
    if bull or bear:
        block = []
        if bull:
            block.append(f"### Bull case\n{bull}")
        if bear:
            block.append(f"### Bear case\n{bear}")
        parts.append("## Bull/bear debate\n" + "\n\n".join(block))

    add("Trader plan", final_state.get("trader_investment_plan"))

    risk = final_state.get("risk_debate_state") or {}
    aggressive = _text(risk.get("aggressive_history"))
    conservative = _text(risk.get("conservative_history"))
    neutral = _text(risk.get("neutral_history"))
    if aggressive or conservative or neutral:
        block = []
        if aggressive:
            block.append(f"### Aggressive\n{aggressive}")
        if conservative:
            block.append(f"### Conservative\n{conservative}")
        if neutral:
            block.append(f"### Neutral\n{neutral}")
        parts.append("## Risk debate\n" + "\n\n".join(block))

    add("Investment plan", final_state.get("investment_plan"))
    add("Final decision", final_state.get("final_trade_decision"))
    add("Past lessons", final_state.get("past_context"))

    return "\n\n".join(parts)


def build_prompt(final_state: dict, ticker: str, history: list[tuple[str, str]], question: str) -> str:
    """One chat turn's prompt: the research, the Q&A so far, then the question."""
    transcript = research_transcript(final_state)
    history_block = ""
    if history:
        history_block = "## Conversation so far\n" + "\n\n".join(
            f"Q: {q}\nA: {a}" for q, a in history
        )

    return (
        "You are a research assistant answering questions about a completed "
        f"trading research run on {ticker}. Answer using only the research "
        "below. If the research does not cover something, say so instead of "
        "guessing. Be concise and specific, and say which report or debate a "
        "claim comes from.\n\n"
        f"## Full research on {ticker}\n{transcript}\n\n"
        f"{history_block}\n\n"
        f"## Question\n{question}"
    )


def answer(final_state: dict, ticker: str, history: list[tuple[str, str]], question: str) -> str:
    """Answer one question about the research, returning the model's reply."""
    result = _deep_llm().invoke(build_prompt(final_state, ticker, history, question)).content
    return result if isinstance(result, str) else str(result)
