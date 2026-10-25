"""LangGraph orchestration for the two-agent financial briefing workflow."""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from financial_alert.agents.agent_orchestrator import AgentOrchestrator
from financial_alert.agents.market_analyst import MarketAnalystAgent
from financial_alert.agents.market_data import MarketDataAgent
from financial_alert.agents.news_researcher import NewsResearchAgent
from financial_alert.agents.research_planner import ResearchPlan, ResearchPlannerAgent
from financial_alert.config import Portfolio, Settings, load_portfolio
from financial_alert.slack import post_to_slack
from financial_alert.telemetry import get_tracer, record_error

logger = logging.getLogger(__name__)
TRACER = get_tracer(__name__)


class WorkflowState(TypedDict, total=False):
    portfolio: Portfolio
    research_plan: ResearchPlan
    research: dict[str, dict[str, Any]]
    market_data: dict[str, dict[str, Any]]
    catalysts: dict[str, str]
    report: str
    artifact_path: str


def _write_artifact(artifacts_dir: Path, report: str, report_date: str) -> Path:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifacts_dir / f"portfolio-briefing-{report_date}.md"
    artifact_path.write_text(report + "\n", encoding="utf-8")
    return artifact_path


def build_workflow(settings: Settings):
    """Create the data pipeline and the two report-generation agents."""
    planner = ResearchPlannerAgent()
    researcher = NewsResearchAgent(
        max_results_per_query=settings.news_results_per_asset,
        minimum_interval_seconds=settings.news_min_request_interval_seconds,
        max_retries=settings.news_max_retries,
    )
    market_agent = MarketDataAgent()
    market_analyst = MarketAnalystAgent(
        base_url=settings.local_llm_base_url,
        model=settings.local_llm_model,
        timeout_seconds=settings.local_llm_timeout_seconds,
    )
    orchestrator = AgentOrchestrator()

    def load_portfolio_node(_: WorkflowState) -> dict[str, Any]:
        return {"portfolio": load_portfolio(settings.portfolio_path)}

    def plan_research_node(state: WorkflowState) -> dict[str, Any]:
        portfolio = state["portfolio"]
        plan = planner.plan(
            portfolio.assets,
            portfolio.watch_list,
            precious_metals=portfolio.precious_metals,
        )
        logger.info("Planned research for %d assets.", len(plan.queries_by_ticker))
        return {"research_plan": plan}

    def gather_data_node(state: WorkflowState) -> dict[str, Any]:
        portfolio = state["portfolio"]
        plan = state["research_plan"]
        return {
            "research": researcher.research(plan.queries_by_ticker),
            "market_data": market_agent.collect(portfolio.symbols),
        }

    def market_analyst_node(state: WorkflowState) -> dict[str, dict[str, str]]:
        portfolio = state["portfolio"]
        market_data = state["market_data"]
        research = state["research"]
        return {
            "catalysts": {
                symbol: market_analyst.analyze(
                    symbol=symbol,
                    market_data=market_data.get(symbol, {}),
                    research=research.get(symbol, {}),
                )
                for symbol in portfolio.symbols
            }
        }

    def orchestrator_node(state: WorkflowState) -> dict[str, str]:
        return {
            "report": orchestrator.compile_report(
                portfolio=state["portfolio"],
                market_data=state["market_data"],
                research=state["research"],
                catalysts=state["catalysts"],
            )
        }

    def deliver_node(state: WorkflowState) -> dict[str, str]:
        report = state["report"]
        artifact_path = _write_artifact(settings.artifacts_dir, report, date.today().isoformat())
        print(report)
        logger.info("Saved report artifact to %s.", artifact_path)
        if settings.slack_webhook_url:
            logger.info("Posting the portfolio briefing to Slack.")
            post_to_slack(settings.slack_webhook_url, report)
            logger.info("Portfolio briefing posted to Slack.")
        else:
            logger.info("Slack delivery skipped: SLACK_WEBHOOK_URL is not configured.")
        return {"artifact_path": str(artifact_path)}

    graph = StateGraph(WorkflowState)
    graph.add_node("load_portfolio", load_portfolio_node)
    graph.add_node("plan_research", plan_research_node)
    graph.add_node("collect_news_and_prices", gather_data_node)
    graph.add_node("market_analyst_agent", market_analyst_node)
    graph.add_node("agent_orchestrator", orchestrator_node)
    graph.add_node("deliver_briefing", deliver_node)
    graph.add_edge(START, "load_portfolio")
    graph.add_edge("load_portfolio", "plan_research")
    graph.add_edge("plan_research", "collect_news_and_prices")
    graph.add_edge("collect_news_and_prices", "market_analyst_agent")
    graph.add_edge("market_analyst_agent", "agent_orchestrator")
    graph.add_edge("agent_orchestrator", "deliver_briefing")
    graph.add_edge("deliver_briefing", END)
    return graph.compile()


def run(settings: Settings) -> str:
    """Run the graph under one root span, so every node, search and model call is one trace."""
    run_id = f"portfolio-briefing-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    attributes = {"session.id": run_id, "langfuse.session.id": run_id,
                  "langfuse.trace.name": "portfolio-briefing", "app.workflow": "langgraph"}
    with TRACER.start_as_current_span("portfolio_briefing", attributes=attributes) as span:
        try:
            result = build_workflow(settings).invoke({})
        except Exception as exc:
            record_error(span, exc)
            raise
        span.set_attribute("briefing.asset_count", len(result.get("market_data", {})))
        span.set_attribute("briefing.artifact_path", result.get("artifact_path", ""))
        return result["report"]
