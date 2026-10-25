"""OpenTelemetry setup (traces + metrics) shared by financial-alert and ticker-review.

Environment variables (standard OTel names):
  OTEL_TRACES_EXPORTER    console | otlp | none           (default console)
  OTEL_METRICS_EXPORTER   console | otlp | none           (default: otlp when traces use otlp, else none)
  OTEL_EXPORTER_OTLP_ENDPOINT          base URL, e.g. http://localhost:4318
  OTEL_EXPORTER_OTLP_TRACES_ENDPOINT   optional full URL override for traces
  OTEL_EXPORTER_OTLP_METRICS_ENDPOINT  optional full URL override for metrics
  OTEL_EXPORTER_OTLP_HEADERS           e.g. Authorization=Basic <token> (read by the exporters)
  OTEL_METRIC_EXPORT_INTERVAL          milliseconds (default 15000)
  OTEL_SERVICE_NAME / OTEL_SERVICE_NAMESPACE / OTEL_RESOURCE_ATTRIBUTES
"""

from __future__ import annotations

import logging
import os
import re
from threading import Lock
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.trace import Span, Status, StatusCode

logger = logging.getLogger(__name__)
_providers: tuple[Any, Any] | None = None
_provider_lock = Lock()


def _signal_endpoint(signal: str) -> str:
    specific = os.getenv(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT", "").strip()
    if specific:
        return specific
    base = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip().rstrip("/")
    if not base:
        raise ValueError("OTEL_EXPORTER_OTLP_ENDPOINT is required when an OTLP exporter is enabled.")
    base = re.sub(r"/v1/(traces|metrics|logs)$", "", base)  # accept an older signal-specific value
    return f"{base}/v1/{signal}"


def _exporter_name(variable: str, default: str) -> str:
    name = os.getenv(variable, default).strip().lower() or default
    if name not in {"console", "otlp", "none"}:
        raise ValueError(f"{variable} must be 'console', 'otlp' or 'none'.")
    return name


def initialize_telemetry(service_name: str | None = None) -> Any:
    """Configure one tracer provider and one meter provider from environment variables.

    `service_name` overrides OTEL_SERVICE_NAME so two apps sharing one .env stay distinct.
    """
    global _providers
    with _provider_lock:
        if _providers is not None:
            return _providers[0]

        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        from financial_alert import __version__

        traces_exporter = _exporter_name("OTEL_TRACES_EXPORTER", "console")
        metrics_exporter = _exporter_name("OTEL_METRICS_EXPORTER", "otlp" if traces_exporter == "otlp" else "none")
        name = (service_name or os.getenv("OTEL_SERVICE_NAME", "") or "financial-alert").strip()
        resource = Resource.create({
            SERVICE_NAME: name,
            "service.namespace": os.getenv("OTEL_SERVICE_NAMESPACE", "stock-review"),
            "service.version": __version__,
        })

        tracer_provider = TracerProvider(resource=resource)
        if traces_exporter == "otlp":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=_signal_endpoint("traces"))))
        elif traces_exporter == "console":
            tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(tracer_provider)

        readers = []
        if metrics_exporter != "none":
            if metrics_exporter == "otlp":
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

                exporter: Any = OTLPMetricExporter(endpoint=_signal_endpoint("metrics"))
            else:
                exporter = ConsoleMetricExporter()
            interval = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "15000"))
            readers.append(PeriodicExportingMetricReader(exporter, export_interval_millis=interval))
        meter_provider = MeterProvider(resource=resource, metric_readers=readers)
        metrics.set_meter_provider(meter_provider)

        _providers = (tracer_provider, meter_provider)
        logger.info("OpenTelemetry enabled for %s: traces=%s, metrics=%s.", name, traces_exporter, metrics_exporter)
        return tracer_provider


def get_tracer(name: str):
    return trace.get_tracer(name)


def get_meter(name: str):
    return metrics.get_meter(name)


def record_error(span: Span, error: BaseException) -> None:
    """Attach an exception and error status without leaking it to business output."""
    span.record_exception(error)
    span.set_status(Status(StatusCode.ERROR, str(error)))


def shutdown_telemetry() -> None:
    """Flush and close the providers at process shutdown (short CLI runs need this)."""
    global _providers
    with _provider_lock:
        providers = _providers
        _providers = None
    if providers is not None:
        tracer_provider, meter_provider = providers
        tracer_provider.shutdown()
        meter_provider.shutdown()
