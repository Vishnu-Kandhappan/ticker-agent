# Ticker Agent

A local, privacy-friendly research assistant for a personal stock portfolio. It gathers market data and news, checks each stock against eight proven short-term trading strategies, and writes a plain-English report saying whether there is a trade setup today, and how sure it is. It is built to be honest by design: the program calculates every number, the local AI only writes or sorts text, and its output is checked before use. Missing data lowers confidence instead of being guessed, and "no setup" is a normal answer.

This project has two workflows:
- Portfolio briefing (`financial-alert.py`): a daily summary of all holdings, with the news behind each price move.
- Ticker review (`ticker-review.py`): a three-agent pipeline per stock. A research agent gathers data, a strategy analyzer applies the scoring rules, and a report composer writes the final report in ASD-STE100 Simplified Technical English.

View the live visual story: https://vishnu-kandhappan.github.io/ticker-agent/

## Tech stack

| Layer | Tools |
|---|---|
| Language | Python 3.10+ |
| Market data | Yahoo Finance (yfinance), with indicators (RSI, MACD, EMAs, ATR, ADX, Bollinger Bands) calculated locally in pandas |
| News | DuckDuckGo news search (ddgs) through a polite crawler: safe search, robots.txt, request pacing, one retry, trust labels |
| AI | Local Ollama (`gemma3:4b`), JSON output, every reply validated (grounded numbers, cited sources, sentence limits) |
| Orchestration | LangGraph (portfolio briefing), plain Python (ticker review) |
| Rules | A Markdown skill file defining factor ratings, strategy gates, confidence scoring and regime filters |
| Delivery | Markdown reports and Slack Block Kit (aligned tables, multi-message) |
| Observability | OpenTelemetry traces and metrics over OTLP, GenAI conventions for model calls (tokens, latency, tokens per second, cold starts, validation outcomes); works with Grafana, Tempo, Prometheus or Langfuse |

## How to tune it

| What to change | Where |
|---|---|
| Stocks to review | `config/portfolio.json` (`asset`, `watch_list`) |
| News depth and crawl politeness | `.env`: `MAX_NEWS_ITEMS`, `CRAWLER_SLEEP_MS`, `CRAWLER_TIMEOUT_MS`, `SAFE_SEARCH` |
| Search queries | `src/ticker_review/pipeline.py` (ticker and economy queries) |
| Source trust lists | `src/ticker_review/crawler.py`, documented in `config/crawler-rules.md` |
| Model and runtime | `.env`: `LOCAL_LLM_MODEL`, `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_TIMEOUT_SECONDS`; `--no-llm` for rules-only runs |
| Agent behaviour and tone | Prompt files in `agents/*.prompt` |
| Strategy thresholds and scoring | `skills/ticker-strategy-analyzer/skill-description.md`, with matching code in `src/ticker_review/strategies.py`. Change both together |
| Account size and risk per trade | `ACCOUNT_SIZE` and `RISK_FRACTION` in `strategies.py` |
| Telemetry | `.env`: `OTEL_TRACES_EXPORTER`, `OTEL_METRICS_EXPORTER`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` |
| Slack delivery | `SLACK_WEBHOOK_URL` in `.env`; `--slack` flag for ticker reviews |

## Quick start

1. Create the environment and install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

2. Configure your environment variables in `.env` (for example, set your local model, Slack webhook, and telemetry preferences).

3. Run the portfolio briefing:

```powershell
.\.venv\Scripts\python.exe financial-alert.py
```

4. Run a ticker review for one or more symbols:

```powershell
.\.venv\Scripts\python.exe ticker-review.py NVDA MSFT
```

5. Install Ollama and download the configured local model before running the local AI pipeline:

```powershell
ollama pull gemma3:4b
```

## Portfolio format

The portfolio lives in `config/portfolio.json`. `asset` contains current holdings. Add candidate tickers to `watch_list`:

```json
{
  "asset": ["AAPL", "RELIANCE.NS"],
  "watch_list": ["MSFT", "NVDA"],
  "precious-metals": ["Gold (XAU/USD)"],
  "manual_updates": "Add context for this report."
}
```

## What the agent is designed to do

- Gather reliable market data and recent news.
- Score each ticker using multiple short-term trading factors.
- Check whether a trade setup is supported by strategy rules and confidence thresholds.
- Write a clear report that separates facts from interpretation.
- Lower confidence when data is missing instead of making up a story.

## Important note

Educational analysis, not investment advice.

---

The project is built to be explicit about its limits. It does not know your investment horizon, risk tolerance, tax status, income, debts, liquidity needs, or position sizes. The workflow is meant to support research and decision-making with grounded, explainable outputs.

If you want to understand the architecture visually, open the live page at https://vishnu-kandhappan.github.io/ticker-agent/.
