"""Minimal local Ollama client that returns JSON or None (never raises).

Instrumented with OpenTelemetry GenAI conventions (financial_alert.genai_telemetry).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from financial_alert.genai_telemetry import chat_span, record_validation

logger = logging.getLogger(__name__)
TEMPERATURE = 0.1


class LocalLLM:
    def __init__(self, base_url: str, model: str, timeout_seconds: float = 90.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def chat_json(self, agent: str, system_prompt: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Send one system + user message and parse a JSON object reply."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ]
        body = json.dumps({
            "model": self.model, "stream": False, "format": "json",
            "messages": messages, "options": {"temperature": TEMPERATURE},
        }).encode("utf-8")
        request = Request(f"{self.base_url}/api/chat", data=body,
                          headers={"Content-Type": "application/json"}, method="POST")
        with chat_span(agent, self.model, self.base_url, messages, TEMPERATURE, output_type="json") as call:
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    result = json.loads(response.read().decode("utf-8"))
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                call.fail(exc)
                record_validation(agent, "fallback", "model_unavailable")
                logger.warning("Local model unavailable for %s (%s); using rule-based text.", agent, type(exc).__name__)
                return None
            call.record_response(result)
            content = str((result.get("message") or {}).get("content", "")).strip()
            call.record_output(content)
            try:
                parsed = json.loads(content)
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict):
                record_validation(agent, "rejected", "invalid_json")
                logger.warning("Local model returned invalid JSON for %s; using rule-based text.", agent)
                return None
            return parsed
