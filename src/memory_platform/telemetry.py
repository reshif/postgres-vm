"""OpenTelemetry tracing setup.

WHAT WAS WRONG. `OTEL_EXPORTER_OTLP_ENDPOINT` has been set on every service
since the first compose file, and the collector, Tempo, Prometheus and Grafana
have all been running and healthy. No span was ever emitted, because nothing was
ever installed to emit one. The pipeline carried zero bytes end to end and every
component reported itself healthy the whole time — which is the observability
failure mode that matters, since a monitoring stack that is confidently empty is
worse than an absent one.

WHY THESE INSTRUMENTATIONS. A context pack is the operation worth tracing, and
its latency decomposes into a few places:

  * FastAPI  — the request span everything else hangs from;
  * psycopg  — `mem.search_hybrid`, the five retrieval arms, and the RLS-scoped
               reads, which is where a missing index shows up;
  * httpx    — the MCP gateway's calls back into the API.

THE EMBEDDER AND CROSS-ENCODER ARE NOT COVERED HERE. Both call out over
`urllib`, not httpx, so no auto-instrumentation sees them — and between them
they are usually most of a pack's wall clock. The first trace captured after
this module landed showed a 774 ms request containing 30 ms of SQL and nothing
else, which points an investigation at the database, the one place the time was
not going. They carry explicit spans instead: `embed.http` in embeddings.py and
`rerank.http` in reranker.py. If either of those modules ever moves to httpx,
delete the manual spans rather than keeping both.

FAILS OPEN, ALWAYS. If the collector is unreachable the application must not
notice. Tracing is diagnostics; a memory platform that stops serving packs
because a telemetry sidecar is down has traded a real capability for an
imaginary one. Every failure path here logs once and returns.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("memory.telemetry")

_STARTED = False


def setup(service_name: str | None = None) -> bool:
    """Install tracing. Returns True if spans will actually be exported.

    Idempotent: uvicorn's reloader and the test harness both import the app more
    than once, and installing two exporters means every span is sent twice.
    """
    global _STARTED
    if _STARTED:
        return True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        log.info("tracing disabled: OTEL_EXPORTER_OTLP_ENDPOINT is unset")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        # A deployment that pinned the old dependency set still runs, untraced.
        log.warning("tracing unavailable, continuing without it: %s", exc)
        return False

    name = service_name or os.environ.get("MEMORY_ROLE", "memory-api")
    resource = Resource.create({
        "service.name": f"memory-{name}",
        "service.namespace": os.environ.get("OTEL_SERVICE_NAMESPACE", "memory-platform"),
    })

    provider = TracerProvider(resource=resource)
    # insecure=True because the collector is an in-compose sidecar on a private
    # network with no certificate. A prod overlay terminating TLS should drop it.
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
    trace.set_tracer_provider(provider)

    _STARTED = True
    log.info("tracing enabled: %s -> %s", resource.attributes["service.name"], endpoint)
    return True


_LOGS_STARTED = False


def setup_logs(service_name: str | None = None) -> bool:
    """Ship Python logging over OTLP, with trace ids attached.

    This is the half that makes an incident tractable. A trace shows that the
    embedder call took 480 ms; the log line written inside that call says which
    model and which batch size. Correlated, they are one investigation. Stored
    separately with no shared identifier, finding the log that belongs to a
    given span means grepping by timestamp and hoping.

    The OTel logging handler stamps trace_id and span_id onto every record
    emitted inside a span, and Loki keeps them as structured metadata — which is
    what lets Grafana offer "logs for this trace" as a link rather than a search.

    Attached as an ADDITIONAL handler, never replacing the stream handler:
    `docker compose logs` has to keep working. If Loki is down, the operator's
    first instinct is to read container output, and a telemetry change that
    breaks that has made the outage harder.
    """
    global _LOGS_STARTED
    if _LOGS_STARTED:
        return True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return False

    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter,
        )
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
    except ImportError as exc:
        log.warning("OTLP logging unavailable, container logs only: %s", exc)
        return False

    name = service_name or os.environ.get("MEMORY_ROLE", "memory-api")
    provider = LoggerProvider(resource=Resource.create({
        "service.name": f"memory-{name}",
        "service.namespace": os.environ.get("OTEL_SERVICE_NAMESPACE", "memory-platform"),
    }))
    provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint, insecure=True)))
    set_logger_provider(provider)

    root = logging.getLogger()

    # Make sure application logs still reach stdout.
    #
    # Uvicorn configures handlers for its OWN loggers (`uvicorn`, `uvicorn.access`)
    # and leaves the root logger untouched. So under uvicorn the `memory.*`
    # loggers propagated to a root with no handler at all: every INFO record was
    # discarded, and WARNING+ only escaped through logging.lastResort. That is
    # why `docker compose logs api` showed uvicorn access lines and not one line
    # from the application itself.
    #
    # Attaching only the OTLP handler would have "fixed" that by making Loki the
    # sole place application logs exist — so if Loki were down, the first thing an
    # operator reaches for would still be empty. stdout stays the local truth;
    # OTLP adds the trace correlation on top.
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(
            "%(levelname)s:%(name)s:%(message)s"))
        root.addHandler(stream)

    root.addHandler(LoggingHandler(level=logging.INFO, logger_provider=provider))
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)

    _LOGS_STARTED = True
    log.info("OTLP log export enabled -> %s", endpoint)
    return True


def instrument_app(app) -> None:
    """Attach FastAPI, httpx and psycopg instrumentation to a live app."""
    if not setup():
        return
    setup_logs()

    # Each instrumentation is attached independently and never in a way that can
    # take the process down. A missing optional package should cost a trace, not
    # an API.
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        # The probes are excluded for the same reason they are excluded from the
        # Prometheus histograms: compose health-checks /healthz every 10s, and a
        # trace store full of health checks buries the requests worth reading.
        FastAPIInstrumentor.instrument_app(
            app, excluded_urls="healthz,readyz,metrics")
    except Exception as exc:  # noqa: BLE001
        log.warning("FastAPI instrumentation failed: %s", exc)

    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001
        log.warning("httpx instrumentation failed: %s", exc)

    try:
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
        # No SQL comment injection: this database runs prepared statements under
        # RLS, and rewriting statement text to carry trace context is a change to
        # what the database executes for the sake of a diagnostic.
        PsycopgInstrumentor().instrument(enable_commenter=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("psycopg instrumentation failed: %s", exc)


def tracer(name: str = "memory"):
    """A tracer that is safe to call whether or not tracing was installed."""
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except ImportError:
        class _Noop:
            def start_as_current_span(self, *_a, **_k):
                from contextlib import nullcontext
                return nullcontext()
        return _Noop()
