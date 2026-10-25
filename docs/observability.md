# Observability (OpenTelemetry)

Both apps export **traces and metrics over OTLP/HTTP** with the same setup
(`src/financial_alert/telemetry.py`). Every local Ollama call is instrumented with
the OpenTelemetry **GenAI semantic conventions** (`src/financial_alert/genai_telemetry.py`).
No extra packages are needed: `opentelemetry-sdk` and
`opentelemetry-exporter-otlp-proto-http` (already in `requirements.txt`) include both exporters.

## Turn it on

```
OTEL_TRACES_EXPORTER=otlp
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318      # collector base URL; /v1/traces and /v1/metrics are added
# OTEL_METRICS_EXPORTER=otlp                           # default: otlp when traces use otlp; set none for trace-only backends
# OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64 public:secret>
# OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false   # true = record prompts and responses
```

| Service (`service.name`) | Set by | Namespace |
|---|---|---|
| `financial-alert` | `OTEL_SERVICE_NAME` | `stock-review` |
| `ticker-review` | `TICKER_REVIEW_SERVICE_NAME` (default `ticker-review`) | `stock-review` |

## Trace shape

```
financial-alert.py                               ticker-review.py  (one trace per ticker)
portfolio_briefing   (session.id = run id)       ticker_review   (session.id = run id, ticker.* results)
├─ fetch_all_market_news                         ├─ market_history (xN)
│  └─ fetch_news_<SYM> → search_request          ├─ crawler_collect → crawler_request
├─ fetch_all_market_data                         ├─ ticker_research_agent
│  └─ fetch_market_data_<SYM>                    │  └─ chat gemma3:4b      ← GenAI span
└─ chat gemma3:4b (one per asset) ← GenAI span   ├─ ticker_strategy_analyzer
                                                 │  └─ chat gemma3:4b      ← GenAI span
                                                 └─ report_composer_agent
```

Root span attributes on `ticker_review`: `ticker.symbol`, `ticker.movement`,
`ticker.movement_confidence`, `ticker.best_strategy`, `ticker.strategy_confidence`,
`ticker.setup`, `ticker.news_items`, `ticker.blocked_sources`, `ticker.intuition_source`.

## GenAI span (`chat <model>`, kind CLIENT)

| Attribute | Source |
|---|---|
| `gen_ai.operation.name` = `chat`, `gen_ai.provider.name` = `ollama` | constant |
| `gen_ai.request.model`, `gen_ai.response.model` | request / Ollama `model` |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | `prompt_eval_count`, `eval_count` |
| `gen_ai.response.finish_reasons` | `done_reason` |
| `gen_ai.request.temperature`, `gen_ai.output.type` | request |
| `gen_ai.agent.name` | `ticker_research_agent`, `ticker_strategy_analyzer`, `market_analyst_agent` |
| `server.address`, `server.port` | `LOCAL_LLM_BASE_URL` |
| `ollama.total_duration_ms`, `ollama.load_duration_ms`, `ollama.prompt_eval_duration_ms`, `ollama.eval_duration_ms` | Ollama timings |
| `ollama.output_tokens_per_second`, `ollama.input_tokens_per_second`, `ollama.cold_start` | derived |
| `error.type` | on failure (span status ERROR) |
| `gen_ai.input.messages`, `gen_ai.output.messages` (+ `gen_ai.prompt`, `gen_ai.completion`) | only with content capture on |
| `langfuse.observation.type` = `generation` | lets Langfuse show the span as a generation |

Span events `llm.output.validation` record each program check:
`llm.validation.outcome` (accepted / rejected / fallback), `llm.validation.reason`, `llm.validation.item`.

## Metrics

| Metric | Type | Key attributes |
|---|---|---|
| `gen_ai.client.operation.duration` (s) | histogram | model, agent, `error.type` |
| `gen_ai.client.token.usage` ({token}) | histogram | `gen_ai.token.type` = input / output |
| `ollama.output.tokens_per_second` | histogram | model, agent |
| `ollama.model_load.duration` (s) | histogram | model |
| `ollama.cold_starts` | counter | model (load > 1 s) |
| `llm.output.validations` | counter | agent, outcome, reason |

Rejection reasons: `invalid_json`, `model_unavailable`, `missing_factor`, `invalid_effect`,
`unknown_evidence_id`, `no_evidence`, `ste_violation`, `macro_only_evidence`,
`ungrounded_number`, `sentence_count`, `missing_no_setup`, `too_long`, `contains_url`,
`missing_citation`, `unknown_publisher`, `citation_not_at_end`, `empty_output`.

Useful queries. Prometheus names depend on the collector's name translation (dots become underscores, unit suffixes are added); check the exact names in your Prometheus:

```promql
# p95 model latency per agent
histogram_quantile(0.95, sum by (le, gen_ai_agent_name) (rate(gen_ai_client_operation_duration_seconds_bucket[1h])))
# generation speed
histogram_quantile(0.5, sum by (le) (rate(ollama_output_tokens_per_second_bucket[1h])))
# share of model outputs rejected by the program checks
sum(rate(llm_output_validations_total{llm_validation_outcome="rejected"}[1d]))
  / sum(rate(llm_output_validations_total[1d]))
```

## Backends

- **OTel Collector → Tempo/Jaeger + Prometheus/Grafana**: traces and metrics.
- **Langfuse** (traces only, OTLP/HTTP): set `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:3000/api/public/otel`,
  the Basic auth header, and `OTEL_METRICS_EXPORTER=none`. Runs group by `langfuse.session.id`.
- **Both**: point the apps at a collector and fan out traces to Langfuse and Tempo.

The GenAI conventions are in "Development" status, so attribute names may change in later versions.
