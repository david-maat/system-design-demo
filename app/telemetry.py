"""Logs + traces, shared by the API (main.py) and the dummy services (dummy.py).

Every log line carries trace_id=..., so Grafana can jump from a log line in Loki to its
trace in Tempo. Traces are only exported when OTEL_EXPORTER_OTLP_ENDPOINT is set.
"""
import logging
import os
import socket

from opentelemetry.instrumentation.logging import LoggingInstrumentor

TRACING = bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))


def setup(logger_name: str) -> logging.Logger:
    """Call at import time, before creating DB pools or HTTP clients."""
    if TRACING:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        # Service name comes from OTEL_SERVICE_NAME; tag spans with the pod as well.
        resource = Resource.create({"service.instance.id": socket.gethostname()})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        # Outgoing HTTP calls get a client span and pass the trace id on in the
        # `traceparent` header, so the called service joins the same trace.
        HTTPXClientInstrumentor().instrument()

    LoggingInstrumentor().instrument(set_logging_format=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per outgoing call is noise
    return logging.getLogger(logger_name)


def instrument_app(app) -> None:
    """Server span per request; the parent comes from an incoming `traceparent` header."""
    if TRACING:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app, excluded_urls="healthz,metrics", exclude_spans=["send", "receive"]
        )


def add_access_log(app, log: logging.Logger) -> None:
    """One log line per request (uvicorn's access log is off, it can't carry trace ids)."""
    import time

    from fastapi import Request

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        if request.url.path not in ("/healthz", "/metrics"):
            log.info(
                "%s %s %d %.0fms",
                request.method, request.url.path, response.status_code,
                (time.perf_counter() - start) * 1000,
            )
        return response
