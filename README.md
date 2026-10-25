# Financial Alert

A LangGraph workflow for portfolio research. It saves a dated Markdown report with a metrics dashboard and an appendix for search diagnostics and source links. It can also post the report to Slack.

## Agent roles

1. **MarketAnalystAgent** receives one asset's five-session trend and trusted, dated news snippets. It explains a catalyst only when a returned publisher supports that explanation. It uses a local Ollama model and returns a fixed no-catalyst sentence if the model is unavailable or its citation cannot be validated.
2. **AgentOrchestrator** compiles market data, catalyst explanations, and the user's note into the required Markdown briefing. It builds the metrics table and keeps raw URLs, search status, and data limits in the appendix.

Research planning, search retrieval, and price collection are data services. They do not generate prose or investment conclusions. Search queries include the current date and ask for company-specific fiscal calendars. Search results are limited to Bloomberg, Reuters, The Wall Street Journal, and CNBC.

The report uses observed market data for prices and momentum. The local model explains possible price catalysts from retrieved news. It cannot change numeric data or create source links. The report is not a forecast or a personalized investment recommendation.

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

The metrics dashboard shows the close and the price position in the one-year range when the provider returns those values. A low range position does not prove that a share is under-valued or suitable to buy.

## Ticker review (three agents)

`ticker-review.py` runs a per-ticker pipeline: **Ticker Research Agent → Ticker Strategy Analyzer → Report Composer**. It rates 9 short-term factors, checks 8 trading strategies (Mean Reversion, PEAD, VCP, Gap and Go, Momentum, Earnings Run-Up, 52-Week High, Sector Rotation), and writes an ASD-STE100 report to `artifacts/ticker-review-<TICKER>-<date>.md`.

```powershell
.\.venv\Scripts\python.exe ticker-review.py NVDA MSFT
```

Prompts are in `agents/`, the scoring rules in `skills/ticker-strategy-analyzer/skill-description.md`, and the crawler rules in `config/crawler-rules.md`. See `docs/ticker-review.md` for the flow, `.env` keys, and limits.

## Setup and run

Install the dependencies with Python 3.10 or newer:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Set the optional Slack Incoming Webhook value in `.env`. Then run:

```powershell
.\.venv\Scripts\python.exe financial-alert.py
```

Install Ollama and download the configured local model before running catalyst analysis:

```powershell
ollama pull gemma3:4b
```

The model endpoint and timeout are configurable with `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_MODEL`, and `LOCAL_LLM_TIMEOUT_SECONDS`. If local inference fails, the report states that no clear catalyst was found.

Search requests are sequential. `NEWS_MIN_REQUEST_INTERVAL_SECONDS` sets the minimum pause between requests (default and minimum 2 seconds), and `NEWS_MAX_RETRIES` sets the maximum attempts per backend. Retries use exponential backoff.

The app loads `.env` from the current directory or the project root. Existing shell variables take priority. Slack delivery runs when `SLACK_WEBHOOK_URL` is set.

## OpenTelemetry

Both apps export traces and metrics over OTLP/HTTP. Telemetry starts in each CLI after `.env` loads. Set `OTEL_TRACES_EXPORTER=console` to print spans locally, or `otlp` to export to `OTEL_EXPORTER_OTLP_ENDPOINT` (the collector base URL, for example `http://localhost:4318`). The app adds `/v1/traces` and `/v1/metrics`. Metrics export over OTLP by default when traces do; set `OTEL_METRICS_EXPORTER=none` for a trace-only backend. `financial-alert.py` reports as `OTEL_SERVICE_NAME` and `ticker-review.py` as `TICKER_REVIEW_SERVICE_NAME` (default `ticker-review`).

Each run is one trace (`portfolio_briefing`) and each ticker review is one trace (`ticker_review`). A shared `session.id` groups the traces of one run. Market-data, search, and crawler spans record counts, failures, and latency. Every Ollama call is a `chat <model>` span with OpenTelemetry GenAI attributes (model, input and output tokens, finish reason), Ollama timings (load, prompt, and generation time, tokens per second, cold starts), and the outcome of the program checks on the model output. Prompts and responses are recorded only when `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`. See `docs/observability.md` for the full span and metric reference, example queries, and Langfuse settings.

Gold is listed under `precious-metals` in `portfolio.json`. The workflow maps it to `GC=F` and uses the latest one-day futures close. This is a continuous gold futures instrument, not spot XAU/USD. If the provider fails, the report uses the same unavailable market snapshot schema used for other instruments.

## Data limits

Prices and volume use Yahoo Finance through yfinance. The latest daily close can be delayed. Coverage and timestamps vary by ticker. The dashboard's direction label shows the sign of the five-session change. It is not a forecast or a buy or sell recommendation. Search results can be missing. A missing result does not prove that no event occurred.

The report is educational. It is not personal investment advice. The workflow does not know your investment horizon, risk tolerance, tax status, income, debts, liquidity needs, or position sizes. Review those limits before you make an investment decision.
