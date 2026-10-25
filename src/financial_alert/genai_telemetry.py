"""LLM observability for local Ollama calls (OpenTelemetry GenAI semantic conventions).

Every model call becomes a CLIENT span named "chat <model>" with gen_ai.* attributes,
Ollama timing data, and metrics:

  gen_ai.client.operation.duration   histogram, s      (GenAI semconv)
  gen_ai.client.token.usage          histogram, token  (GenAI semconv; gen_ai.token.type=input|output)
  ollama.output.tokens_per_second    histogram         (generation speed)
  ollama.model_load.duration         histogram, s      (load_duration reported by Ollama)
  ollama.cold_starts                 counter           (load_duration above 1 s)
  llm.output.validations             counter           (accepted | rejected | fallback, with reason)

Prompt and response text are recorded only when
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true.
GenAI conventions are still in "Development" status; names can change.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urlparse

from opentelemetry import trace
from opentelemetry.trace import SpanKind

from financial_alert.telemetry import get_meter, get_tracer, record_error

CAPTURE_ENV = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
MAX_CONTENT_CHARS = 16_000
COLD_START_SECONDS = 1.0
PROVIDER = "ollama"

_TRACER = get_tracer("stock_review.genai")
_METER = get_meter("stock_review.genai")
_DURATION = _METER.create_histogram(
    "gen_ai.client.operation.duration", unit="s", description="GenAI operation duration.",
    explicit_bucket_boundaries_advisory=[0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12,
                                         10.24, 20.48, 40.96, 81.92],
)
_TOKENS = _METER.create_histogram(
    "gen_ai.client.token.usage", unit="{token}", description="Input and output tokens used.",
    explicit_bucket_boundaries_advisory=[1, 4, 16, 64, 256, 1024, 4096, 16384, 65536],
)
_TPS = _METER.create_histogram("ollama.output.tokens_per_second", unit="{token}/s",
                               description="Ollama generation speed (eval_count / eval_duration).")
_LOAD = _METER.create_histogram("ollama.model_load.duration", unit="s",
                                description="Model load time reported by Ollama.")
_COLD = _METER.create_counter("ollama.cold_starts", unit="{start}",
                              description="Calls where Ollama had to load the model (load_duration > 1 s).")
_VALIDATIONS = _METER.create_counter("llm.output.validations", unit="{output}",
                                     description="Outcome of program checks on model output.")


def capture_content() -> bool:
    return os.getenv(CAPTURE_ENV, "false").strip().lower() in {"1", "true", "yes"}


def _messages(messages: list[dict[str, Any]]) -> str:
    converted = [{"role": m.get("role", "user"), "parts": [{"type": "text", "content": str(m.get("content", ""))}]}
                 for m in messages]
    return json.dumps(converted, ensure_ascii=False)[:MAX_CONTENT_CHARS]


class ChatObservation:
    """Handle returned by chat_span(); call record_response / record_output / fail."""

    def __init__(self, span: Any, metric_attributes: dict[str, Any]) -> None:
        self.span = span
        self.metric_attributes = metric_attributes
        self.error_type: str | None = None

    def record_response(self, result: dict[str, Any]) -> None:
        span, attrs = self.span, self.metric_attributes
        model = result.get("model")
        if model:
            span.set_attribute("gen_ai.response.model", str(model))
            attrs["gen_ai.response.model"] = str(model)
        if result.get("done_reason"):
            span.set_attribute("gen_ai.response.finish_reasons", [str(result["done_reason"])])
        input_tokens, output_tokens = result.get("prompt_eval_count"), result.get("eval_count")
        if isinstance(input_tokens, int):
            span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
            _TOKENS.record(input_tokens, {**attrs, "gen_ai.token.type": "input"})
        if isinstance(output_tokens, int):
            span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
            _TOKENS.record(output_tokens, {**attrs, "gen_ai.token.type": "output"})

        durations = {key: result.get(key) for key in ("total_duration", "load_duration",
                                                      "prompt_eval_duration", "eval_duration")}
        for key, value in durations.items():
            if isinstance(value, (int, float)):
                span.set_attribute(f"ollama.{key}_ms", round(value / 1e6, 2))
        load, prompt_eval, evaluate = (durations["load_duration"], durations["prompt_eval_duration"],
                                       durations["eval_duration"])
        if isinstance(load, (int, float)):
            seconds = load / 1e9
            _LOAD.record(seconds, attrs)
            cold = seconds > COLD_START_SECONDS
            span.set_attribute("ollama.cold_start", cold)
            if cold:
                _COLD.add(1, attrs)
        if isinstance(output_tokens, int) and isinstance(evaluate, (int, float)) and evaluate > 0:
            tps = output_tokens / (evaluate / 1e9)
            span.set_attribute("ollama.output_tokens_per_second", round(tps, 2))
            _TPS.record(tps, attrs)
        if isinstance(input_tokens, int) and isinstance(prompt_eval, (int, float)) and prompt_eval > 0:
            span.set_attribute("ollama.input_tokens_per_second", round(input_tokens / (prompt_eval / 1e9), 2))

    def record_output(self, text: str) -> None:
        if capture_content():
            output = _messages([{"role": "assistant", "content": text}])
            self.span.set_attribute("gen_ai.output.messages", output)
            self.span.set_attribute("gen_ai.completion", text[:MAX_CONTENT_CHARS])  # Langfuse output

    def fail(self, error: BaseException) -> None:
        self.error_type = type(error).__name__
        self.span.set_attribute("error.type", self.error_type)
        record_error(self.span, error)


@contextmanager
def chat_span(agent: str, model: str, base_url: str, messages: list[dict[str, Any]],
              temperature: float | None = None, output_type: str | None = None) -> Iterator[ChatObservation]:
    """Wrap one Ollama /api/chat call."""
    parsed = urlparse(base_url)
    metric_attributes: dict[str, Any] = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": PROVIDER,
        "gen_ai.request.model": model,
        "gen_ai.agent.name": agent,
    }
    attributes: dict[str, Any] = {
        **metric_attributes,
        "server.address": parsed.hostname or "localhost",
        "server.port": parsed.port or 11434,
        "langfuse.observation.type": "generation",
    }
    if temperature is not None:
        attributes["gen_ai.request.temperature"] = temperature
    if output_type:
        attributes["gen_ai.output.type"] = output_type
    start = time.perf_counter()
    with _TRACER.start_as_current_span(f"chat {model}", kind=SpanKind.CLIENT, attributes=attributes) as span:
        if capture_content():
            span.set_attribute("gen_ai.input.messages", _messages(messages))
            span.set_attribute("gen_ai.prompt", _messages(messages))  # Langfuse input
        observation = ChatObservation(span, metric_attributes)
        try:
            yield observation
        except BaseException as exc:
            observation.fail(exc)
            raise
        finally:
            attrs = dict(observation.metric_attributes)
            if observation.error_type:
                attrs["error.type"] = observation.error_type
            _DURATION.record(time.perf_counter() - start, attrs)


def record_validation(agent: str, outcome: str, reason: str | None = None, item: str | None = None) -> None:
    """Count a program check on model output and add an event to the current span.

    outcome: accepted | rejected | fallback
    """
    attributes = {"gen_ai.agent.name": agent, "llm.validation.outcome": outcome,
                  "llm.validation.reason": reason or "none"}
    _VALIDATIONS.add(1, attributes)
    span = trace.get_current_span()
    if span.is_recording():
        event = dict(attributes)
        if item:
            event["llm.validation.item"] = item
        span.add_event("llm.output.validation", event)
