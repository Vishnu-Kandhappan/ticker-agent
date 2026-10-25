# Ticker Review (three-agent pipeline)

`ticker-review.py` reviews one or more tickers and writes one ASD-STE100
report per ticker to `artifacts/ticker-review-<TICKER>-<date>.md`.

```powershell
.\.venv\Scripts\python.exe ticker-review.py NVDA MSFT      # named tickers
.\.venv\Scripts\python.exe ticker-review.py                # asset + watch_list from config/portfolio.json
.\.venv\Scripts\python.exe ticker-review.py TSLA --no-llm  # rule-based text only
.\.venv\Scripts\python.exe ticker-review.py AAPL --slack   # also post to SLACK_WEBHOOK_URL
.\.venv\Scripts\python.exe -m unittest discover -s tests   # offline tests
```

Precious metals in `portfolio.json` are skipped. The 8 strategies are for equities.

## Flow

```
config/portfolio.json ─┐
.env (crawler limits) ─┤
                       ▼
  ┌──────────────────────────────┐   yfinance: 2y daily bars, info, earnings dates,
  │ 1. Ticker Research Agent     │   ^GSPC, ^VIX, SPY, sector ETF
  │  agents/ticker-research-     │   SafeCrawler: ddgs news (safe search) → robots.txt
  │  agent.prompt                │   → page read, 1 retry, trust label, source log
  └──────────────┬───────────────┘   Local model: classify news per factor (validated)
                 ▼ intelligence summary
  ┌──────────────────────────────┐   skills/ticker-strategy-analyzer/skill-description.md
  │ 2. Ticker Strategy Analyzer  │   → ratings 0–5, risk, scenario map, 8-strategy gates,
  │  agents/ticker-strategy-     │     confidence score, trade levels, sizing
  │  analyzer.prompt             │   Local model: "intuition" text only (validated)
  └──────────────┬───────────────┘
                 ▼ analysis
  ┌──────────────────────────────┐   Deterministic: copies values only, so it cannot
  │ 3. Report Composer Agent     │   add data. Checks each sentence for ASD-STE100 length.
  └──────────────┬───────────────┘
                 ▼
  artifacts/ticker-review-<TICKER>-<date>.md  (+ optional Slack)
```

## Where the language model is used

The program calculates every number. The local Ollama model
(`LOCAL_LLM_MODEL`, default `gemma3:4b`) has two small jobs:

| Agent | Job | Validation (output rejected → rule-based text) |
|---|---|---|
| Research | Classify each news item per factor: positive / negative / neutral / no data | Evidence IDs must exist; company factors need company news; max 2 sentences, 25 words each; every number must appear in the cited source |
| Strategy | Write the 2–4 sentence "intuition" | ASD-STE100 length; no speculative words; every number must appear in the facts; must say "no setup" when confidence < 50 |

If Ollama is not running, the report still builds. The appendix states which
text came from the model and which from rules.

## .env keys

| Key | Default | Use |
|---|---|---|
| `MAX_NEWS_ITEMS` | 2 | Pages read per crawl (one ticker crawl + one shared macro crawl per market) |
| `CRAWLER_SLEEP_MS` | 1500 | Minimum gap between requests |
| `SAFE_SEARCH` | true | `safesearch=on` |
| `CRAWLER_TIMEOUT_MS` | 20000 | Per-request timeout |
| `PORTFOLIO_PATH` | config/portfolio.json | Shared with `financial-alert.py` |

See `config/crawler-rules.md` for blocked-page, robots.txt and trust-label rules.

## Known limits

- Yahoo Finance data can be delayed. On a weekend, "today" is the last session.
- `MAX_NEWS_ITEMS=2` gives thin news coverage. Many factors show "no data", and
  movement confidence is often Low. This is by design: missing data lowers confidence.
- Many news sites (Reuters, Bloomberg, WSJ) block bots or disallow them in
  robots.txt. Those pages are logged as blocked and skipped, as the rules require.
- Gates that need paid data (guidance changes, analyst upgrades, revenue
  surprise, ETF flows) are not checked. The report notes this per strategy.
- VCP contractions are approximated from three 20-session windows.
- Non-US tickers (`.NS`, `.BO`) use the S&P 500 regime and US sector ETFs. The report warns about this.
