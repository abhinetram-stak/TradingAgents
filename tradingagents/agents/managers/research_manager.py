"""Research Manager: turns the bull/bear debate into a structured investment plan for the trader."""

from __future__ import annotations

from tradingagents.agents.schemas import ResearchPlan, render_research_plan
from tradingagents.agents.utils.agent_utils import build_instrument_context
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)


def create_research_manager(llm):
    structured_llm = bind_structured(llm, ResearchPlan, "Research Manager")

    def research_manager_node(state) -> dict:
        instrument_context = build_instrument_context(state["company_of_interest"])
        history = state["investment_debate_state"].get("history", "")
        trading_objective = state.get("trading_objective", "")
        intraday_context = state.get("intraday_context", "")

        investment_debate_state = state["investment_debate_state"]

        prompt = f"""As the Research Manager and debate facilitator, your role is to critically evaluate this round of debate and deliver a clear, actionable investment plan for the trader.

{instrument_context}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong intraday long setup for today
- **Overweight**: Moderate intraday long setup for today
- **Hold**: No trade / wait; setup is unclear, choppy, illiquid, or too close to risk levels
- **Underweight**: Avoid long exposure or reduce existing exposure today
- **Sell**: Strong intraday exit/short-avoidance signal

Commit to a clear stance whenever the debate's strongest arguments warrant one; reserve Hold for situations where the evidence on both sides is genuinely balanced.

**Intraday Mandate:**
{trading_objective or "Evaluate today's trade only."}

**Intraday Context:**
{intraday_context or "No intraday context supplied."}

Your plan applies only to today's session and must assume any open position exits by 15:25 IST. Do not recommend multi-week or multi-month holding periods. Include entry condition, invalidation, stop-loss suitability, and take-profit suitability in the strategic actions.

---

**Debate History:**
{history}"""

        investment_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_research_plan,
            "Research Manager",
        )

        new_investment_debate_state = {
            "judge_decision": investment_plan,
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": investment_plan,
            "count": investment_debate_state["count"],
        }

        return {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": investment_plan,
        }

    return research_manager_node
