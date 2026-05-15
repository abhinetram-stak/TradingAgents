"""Trader: turns the Research Manager's investment plan into a concrete transaction proposal."""

from __future__ import annotations

import functools

from langchain_core.messages import AIMessage

from tradingagents.agents.schemas import TraderProposal, render_trader_proposal
from tradingagents.agents.utils.agent_utils import build_instrument_context
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)


def create_trader(llm):
    structured_llm = bind_structured(llm, TraderProposal, "Trader")

    def trader_node(state, name):
        company_name = state["company_of_interest"]
        instrument_context = build_instrument_context(company_name)
        investment_plan = state["investment_plan"]
        trading_objective = state.get("trading_objective", "")
        intraday_context = state.get("intraday_context", "")

        messages = [
            {
                "role": "system",
                "content": (
                    "You are an intraday trading agent. Your decision applies only to today's "
                    "Indian market session, and all positions must be closed by 15:25 IST. "
                    "Do not make long-term investment recommendations. Provide a specific "
                    "Buy, Sell, or Hold transaction proposal anchored in the analysts' reports, "
                    "the research plan, and the intraday setup."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Based on a comprehensive analysis by a team of analysts, here is an investment "
                    f"plan tailored for {company_name}. {instrument_context} This plan incorporates "
                    f"insights from current technical market trends, macroeconomic indicators, and "
                    f"social media sentiment. Use this plan as a foundation for evaluating your next "
                    f"trading decision.\n\nTrading objective: {trading_objective}\n\n"
                    f"Intraday context:\n{intraday_context}\n\n"
                    f"Proposed Investment Plan: {investment_plan}\n\n"
                    f"Return an intraday proposal with entry condition, stop-loss, position sizing, "
                    f"and a hard end-of-day exit assumption. If the setup is not clean, choose Hold."
                ),
            },
        ]

        trader_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            messages,
            render_trader_proposal,
            "Trader",
        )

        return {
            "messages": [AIMessage(content=trader_plan)],
            "trader_investment_plan": trader_plan,
            "sender": name,
        }

    return functools.partial(trader_node, name="Trader")
