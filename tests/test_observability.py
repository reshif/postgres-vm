"""Suite 9 — the observability stack actually carries data.

WHY THIS SUITE EXISTS. The collector, Tempo, Prometheus and Grafana all ran
healthy for the entire build while the traces pipeline carried zero bytes and
Grafana had no dashboards at all. `OTEL_EXPORTER_OTLP_ENDPOINT` was set on every
service; nothing was installed to emit a span. Every healthcheck was green.

That is the observability failure mode worth guarding against: not a component
that crashes, but a stack that is confidently empty. Container health tells you
processes are up. Only asserting on delivered data tells you the pipeline works.

So every check here follows something end to end — emit, then look for it at the
far side — rather than asking a service whether it feels well.

    docker compose exec -T api python - < tests/test_observability.py
"""
from __future__ import annotations

import json
import logging
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from uuid import UUID

sys.path.insert(0, "/app/src")
from memory_platform.config import settings  # noqa: E402

RUN = uuid.uuid4().hex[:8]
TENANT = UUID(settings().dev_tenant_id) if settings().dev_tenant_id else None
PROJECT = UUID(settings().dev_project_id) if settings().dev_project_id else None
PRINCIPAL = UUID(settings().dev_principal_id) if settings().dev_principal_id else None

API = "http://localhost:8080"
PROM = "http://prometheus:9090"
TEMPO = "http://tempo:3200"
GRAFANA = "http://grafana:3000"
SCHEDULER = "http://scheduler:9100"
WORKER = "http://worker:9101"
LOKI = "http://loki:3100"
ALERTMANAGER = "http://alertmanager:9093"

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def get(url: str, timeout: float = 15.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode()


def get_json(url: str, timeout: float = 15.0):
    return json.loads(get(url, timeout))


def post_json(
    url: str,
    payload: dict,
    timeout: float = 120.0,
    headers: dict[str, str] | None = None,
):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def promql(query: str):
    return get_json(PROM + "/api/v1/query?query=" + urllib.parse.quote(query))["data"]["result"]


def main() -> None:
    import urllib.parse  # noqa: F401 - used via promql

    if not (TENANT and PROJECT):
        print("  SKIP  no dev scope binding configured")
        return

    # Traffic first, deliberately.
    #
    # A labelled Prometheus histogram exports NO series until a label
    # combination is observed, so on a freshly restarted API
    # `memory_context_duration_seconds` is absent from /metrics rather than
    # present-and-zero. Asserting on the families before generating a pack made
    # this suite pass or fail on whether someone had used the system recently —
    # which is exactly the kind of order dependence that makes a test useless as
    # a gate.
    # Give this request a known W3C trace ID. Searching only the latest N traces
    # is not an end-to-end assertion under suite load: the application's SQL
    # spans can push the endpoint trace outside that arbitrary result window.
    request_trace_id = uuid.uuid4().hex
    traceparent = f"00-{request_trace_id}-{uuid.uuid4().hex[:16]}-01"
    pack = post_json(API + "/v1/context", {
        "tenant_id": str(TENANT), "project_id": str(PROJECT),
        "principal_id": str(PRINCIPAL or TENANT),
        "task": f"why did we choose pgvector {RUN}", "token_budget": 4000,
    }, headers={"traceparent": traceparent})

    # ---- 1. the app emits domain metrics, not just generic HTTP ones -------
    print("\n1. The API exposes metrics about THIS system")
    body = get(API + "/metrics")
    families = {line.split("{")[0].split(" ")[0]
                for line in body.splitlines() if line.startswith("memory_")}

    # Each of these corresponds to a number in 04-EVALUATION.md, not to whatever
    # was convenient to instrument.
    for want, why in [
        ("memory_context_duration_seconds_bucket", "p95 context < 350 ms gate"),
        ("memory_context_stage_duration_seconds_bucket", "where pack time goes"),
        ("memory_context_pack_tokens_bucket", "what memory costs the agent"),
        ("memory_pack_items_total", "trust attribution (C4)"),
        ("memory_write_duration_seconds_bucket", "write -> retrievable p99 < 5 s"),
    ]:
        check(f"{want.replace('_bucket','')} — {why}", want in families)

    check("latency buckets straddle the 350 ms gate",
          'le="0.35"' in body,
          "a histogram that jumps 0.25 -> 0.5 cannot measure a 350 ms gate")

    # ---- 2. curation gauges come from the scheduler ------------------------
    # They cannot come from the API: it runs as memory_app (NOBYPASSRLS) and a
    # Prometheus scrape carries no scope, so cross-project aggregates are not
    # computable there without a role that can read every tenant.
    print("\n2. Curation gauges are published by the scheduler")
    try:
        sched = get(SCHEDULER + "/metrics")
    except (urllib.error.URLError, OSError) as exc:
        sched = ""
        check("the scheduler serves a metrics endpoint", False, str(exc)[:50])
    else:
        check("the scheduler serves a metrics endpoint", True)

    for want in ("memory_inbox_depth", "memory_extraction_enabled",
                 "memory_conflicts_open", "memory_memories"):
        check(f"{want} is published", want in sched)

    check("the API does NOT publish cross-project gauges",
          "memory_inbox_depth" not in families,
          "would require a BYPASSRLS connection in the serving process")

    try:
        worker = get(WORKER + "/metrics")
    except (urllib.error.URLError, OSError) as exc:
        worker = ""
        check("the worker serves a metrics endpoint", False, str(exc)[:50])
    else:
        check("the worker serves a metrics endpoint", True)
    check("worker publishes an event-loop heartbeat",
          "memory_service_heartbeat_timestamp_seconds" in worker)

    # ---- 3. Prometheus is scraping all of it -------------------------------
    print("\n3. Prometheus is scraping every target")
    # Polled rather than sampled once. The scrape interval is 15 s, so a target
    # that restarted moments ago is legitimately DOWN for one cycle — and a gate
    # that fails whenever the stack was just rebuilt teaches people to re-run it
    # until it goes green, which is the same as not having it.
    wanted = ("memory-api", "memory-scheduler", "memory-worker",
              "blackbox-exporter", "service-probes", "tcp-service-probes",
              "otel-collector")
    health: dict[str, str] = {}
    deadline = time.time() + 75
    while time.time() < deadline:
        targets = get_json(PROM + "/api/v1/targets")["data"]["activeTargets"]
        health = {t["labels"].get("job"): t["health"] for t in targets}
        if all(health.get(j) == "up" for j in wanted):
            break
        time.sleep(5)
    for job in wanted:
        check(f"target {job} is up", health.get(job) == "up", f"{health.get(job)}")

    # `up` only proves the exporter answered Prometheus. The blackbox result is
    # the service-level contract: API readiness, console proxy, MCP upstream,
    # database TCP paths, and each observability UI must all answer.
    probes = {
        row["metric"].get("service"): float(row["value"][1])
        for row in promql("probe_success")
        if row["metric"].get("service")
    }
    required_probes = {
        "api", "console", "mcp", "postgres", "pgbouncer",
        "otel-collector", "tempo", "loki", "prometheus",
        "alertmanager", "grafana",
    }
    check("every required service has a synthetic probe",
          required_probes <= set(probes), str(sorted(probes))[:100])
    failed_probes = sorted(service for service in required_probes if probes.get(service) != 1)
    check("every required service endpoint is healthy", not failed_probes,
          ", ".join(failed_probes))

    # The console's static files can remain healthy after the API container is
    # recreated while nginx keeps an old Docker DNS result for its proxy. The
    # resolver is part of the readiness contract, not just proxy configuration.
    nginx = Path("/repo/console/nginx.conf").read_text("utf-8")
    check("console re-resolves the API after container replacement",
          "resolver 127.0.0.11" in nginx
          and "proxy_pass http://$api_host:8080" in nginx)

    heartbeats = {
        row["metric"].get("service"): float(row["value"][1])
        for row in promql("memory_service_heartbeat_timestamp_seconds")
    }
    stale = sorted(service for service in ("worker", "scheduler")
                   if time.time() - heartbeats.get(service, 0) > 90)
    check("worker and scheduler heartbeats are fresh", not stale,
          ", ".join(stale))

    rules = get_json(PROM + "/api/v1/rules")["data"]["groups"]
    rule_names = {r["name"] for g in rules for r in g["rules"]}
    check("alert rules are loaded", len(rule_names) >= 5, f"{len(rule_names)} rules")
    for want in ("ContextPackSlow", "ReviewBacklogGrowing",
                 "ExtractionDisabledByKillSwitch", "CriticalServiceEndpointDown",
                 "ServiceHeartbeatStale", "PostgresUnavailable",
                 "AlertNotificationDeliveryFailed"):
        check(f"alert {want} exists", want in rule_names)

    # ---- 4. a real request produces a real trace ---------------------------
    # The end-to-end assertion. Emit a pack, then find its trace in Tempo.
    print("\n4. A context pack produces a trace with its latency decomposed")
    check("the pack was built", bool(pack.get("pack_id")), pack.get("pack_id", "")[:16])

    # BatchSpanProcessor flushes on an interval; poll Tempo for this exact trace
    # rather than a non-deterministic sample of recent traces.
    found: dict | None = None
    # Generous, because three things buffer between the request and the answer:
    # BatchSpanProcessor's export interval, the collector's batch processor, and
    # Tempo's ingester block duration.
    deadline = time.time() + 120
    while time.time() < deadline and not found:
        time.sleep(3)
        try:
            detail = get_json(TEMPO + f"/api/traces/{request_trace_id}")
        except (urllib.error.URLError, OSError, KeyError):
            continue
        names = [
            span["name"]
            for batch in detail.get("batches", [])
            for scope_spans in batch.get("scopeSpans", [])
            for span in scope_spans.get("spans", [])
        ]
        # Tempo returns a trace as soon as its first OTLP batch is ingested. The
        # database spans can arrive before the request and urllib spans from a
        # later BatchSpanProcessor export, so keep polling until the trace is
        # complete enough to establish the latency decomposition we promise.
        if "POST /v1/context" in names and "embed.http" in names:
            found = detail

    check("the request reached Tempo as a trace", found is not None,
          request_trace_id[:16] if found else "")

    if found:
        names = []
        for b in found.get("batches", []):
            for ss in b.get("scopeSpans", []):
                for s in ss.get("spans", []):
                    names.append(s["name"])

        check("the trace contains the context request",
              "POST /v1/context" in names, f"{len(names)} spans")
        check("the trace contains database spans",
              any(n.upper().startswith(("SELECT", "INSERT", "UPDATE")) for n in names),
              f"{len(names)} spans")

        # The one that regressed silently: the embedder calls out over urllib,
        # which no auto-instrumentation sees. Without an explicit span, a 780 ms
        # pack shows 30 ms of SQL and nothing else, and the trace points the
        # investigation at the database — the one place the time is not going.
        check("the embedder call is traced (urllib, not httpx)",
              "embed.http" in names,
              "explicit span in embeddings._post")

    # ---- 5. the numbers the dashboards ask for actually resolve ------------
    print("\n5. Every dashboard query returns series")
    for query, label in [
        ("histogram_quantile(0.95, sum by (le) (rate("
         "memory_context_duration_seconds_bucket[10m])))", "p95 pack latency"),
        ("histogram_quantile(0.95, sum by (le, stage) (rate("
         "memory_context_stage_duration_seconds_bucket[10m])))", "p95 by stage"),
        ("sum by (tier) (increase(memory_pack_items_total[6h]))", "items by trust tier"),
        ("sum(memory_inbox_depth)", "review backlog"),
        ("min(memory_extraction_enabled)", "kill-switch state"),
    ]:
        try:
            series = promql(query)
        except Exception as exc:  # noqa: BLE001
            series = []
            print(f"      query error: {exc}")
        check(f"{label} resolves", len(series) >= 1, f"{len(series)} series")

    # Tier 1 must never appear in a pack. Suite 2 asserts it at the API; this
    # asserts the dashboard would show it if it ever did.
    tiers = {s["metric"].get("tier"): float(s["value"][1])
             for s in promql("sum by (tier) (increase(memory_pack_items_total[6h]))")}
    check("no untrusted item has ever been returned in a pack",
          tiers.get("untrusted", 0) == 0, str(tiers)[:60])

    # ---- 6. Grafana has dashboards, not just datasources -------------------
    # The original failure: ./ops/grafana held only datasources/, so Grafana had
    # somewhere to read data FROM and nothing to draw. Neither provider warns
    # about the other's absence.
    print("\n6. Grafana is provisioned with dashboards")
    try:
        dashboards = get_json(GRAFANA + "/api/search?type=dash-db")
    except (urllib.error.URLError, OSError) as exc:
        dashboards = []
        check("Grafana is reachable", False, str(exc)[:50])
    else:
        check("Grafana is reachable", True)

    uids = {d.get("uid") for d in dashboards}
    check("the retrieval dashboard is provisioned", "memory-retrieval" in uids,
          str(sorted(u for u in uids if u))[:60])
    check("the curation dashboard is provisioned", "memory-curation" in uids)
    check("the operations dashboard is provisioned", "memory-operations" in uids)

    try:
        datasources = get_json(GRAFANA + "/api/datasources")
    except (urllib.error.URLError, OSError):
        datasources = []
    sources = {d["type"] for d in datasources}
    check("metrics, traces, and Alertmanager datasources are provisioned",
          {"prometheus", "tempo", "alertmanager"} <= sources,
          str(sorted(sources)))

    # ---- 7. every panel actually points at a datasource that exists --------
    #
    # THE BUG THIS EXISTS FOR. `datasources.yml` did not pin a `uid`, so Grafana
    # generated one (`PBFA97CFB590B2093`), while the dashboard JSON referenced
    # `uid: prometheus`. Every panel rendered "No data" with an error triangle.
    #
    # It survived the checks above because each half was genuinely healthy: the
    # datasource answered queries when asked by its real uid, the dashboards
    # were provisioned, Prometheus had the data. Only the REFERENCE between them
    # was broken — and nothing that tests the two halves separately can see it.
    print("\n7. Dashboard panels resolve against real datasources")
    known_uids = {d["uid"] for d in datasources}
    check("datasource uids are pinned, not generated",
          {"prometheus", "alertmanager"} <= known_uids,
          "unpinned uids break every provisioned dashboard")

    dangling: list[str] = []
    panels_checked = 0
    for uid in ("memory-retrieval", "memory-curation", "memory-operations"):
        try:
            dash = get_json(GRAFANA + f"/api/dashboards/uid/{uid}")["dashboard"]
        except (urllib.error.URLError, OSError, KeyError):
            continue
        for panel in dash.get("panels", []):
            if panel.get("type") == "row":
                continue
            panels_checked += 1
            ref = (panel.get("datasource") or {}).get("uid")
            if ref and ref not in known_uids:
                dangling.append(f"{panel.get('title', '?')[:24]}->{ref}")

    check("every panel was inspected", panels_checked > 15, f"{panels_checked} panels")
    check("no panel references a datasource that does not exist",
          not dangling, "; ".join(dangling[:3]) or "all resolve")

    # ---- 8. alerting has a delivery path and reports failure ---------------
    print("\n8. Alerts route somewhere and report delivery failures")
    ams = get_json(PROM + "/api/v1/alertmanagers")["data"]["activeAlertmanagers"]
    check("an Alertmanager is registered", len(ams) >= 1,
          str([a["url"] for a in ams])[:60])

    rule_types: dict[str, int] = {}
    for g in get_json(PROM + "/api/v1/rules")["data"]["groups"]:
        for r in g["rules"]:
            rule_types[r["type"]] = rule_types.get(r["type"], 0) + 1
    # The §7.3 gates are multi-day windows. Evaluating them from raw histogram
    # buckets on every dashboard refresh is slow enough that people stop asking,
    # so they are pre-computed under a stable name.
    check("recording rules pre-compute the gates",
          rule_types.get("recording", 0) >= 8, str(rule_types))
    check("alert rules are loaded", rule_types.get("alerting", 0) >= 8, str(rule_types))

    try:
        am_ok = get_json(ALERTMANAGER + "/api/v2/status")
        check("Alertmanager is healthy and has a config",
              bool(am_ok.get("config", {}).get("original")), "")
    except (urllib.error.URLError, OSError) as exc:
        check("Alertmanager is healthy and has a config", False, str(exc)[:40])

    alertmanager_metrics = get(ALERTMANAGER + "/metrics")
    check("Alertmanager exposes notification delivery failures",
          "alertmanager_notifications_failed_total" in alertmanager_metrics)
    renderer = Path("/repo/ops/alertmanager/render-config.sh").read_text()
    check("Alertmanager supports a configured outbound webhook",
          "ALERT_WEBHOOK_URL" in renderer and "webhook_configs" in
          Path("/repo/ops/alertmanager/webhook.yml").read_text())

    # ---- 9. logs exist at all ---------------------------------------------
    # The OTel logs pipeline exported to `debug`, which prints a summary line and
    # discards the payload. There was no log store and no Grafana logs
    # datasource, so "logs" was a word in a config file.
    print("\n9. Logs are collected and queryable")
    try:
        services = get_json(
            LOKI + "/loki/api/v1/label/service/values").get("data") or []
    except (urllib.error.URLError, OSError) as exc:
        services = []
        check("Loki is reachable", False, str(exc)[:40])
    else:
        check("Loki is reachable", True)

    check("container logs are shipped for many services", len(services) >= 8,
          f"{len(services)} services")
    # The ones that will never speak OTLP and are exactly what gets read at 3am.
    for want in ("postgres", "pgbouncer"):
        check(f"{want} logs are collected", want in services, str(services)[:50])

    indexed = set(get_json(LOKI + "/loki/api/v1/labels").get("data") or [])
    # Loki merges structured metadata into the stream view when querying, so the
    # label API is the only place that shows what is genuinely INDEXED — the only
    # thing that costs cardinality.
    check("the indexed label set stays small", len(indexed) <= 15,
          f"{len(indexed)} labels")
    for forbidden in ("observed_timestamp", "trace_id", "code_line_number"):
        check(f"`{forbidden}` is not an indexed label", forbidden not in indexed,
              "per-line values as labels would create a stream per log line")

    # ---- 10. logs and traces are joined ------------------------------------
    print("\n10. A log line can be found from its trace")
    marker = f"obs-suite-{RUN}"
    probe_log = logging.getLogger("memory.observability_probe")
    probe_log.setLevel(logging.INFO)
    from memory_platform import telemetry as _t  # noqa: E402

    _t.setup()
    _t.setup_logs()
    trace_id = ""
    with _t.tracer("obs-suite").start_as_current_span("obs.probe") as span:
        try:
            trace_id = format(span.get_span_context().trace_id, "032x")
        except Exception:  # noqa: BLE001
            trace_id = ""
        probe_log.info("observability suite probe %s", marker)

    try:
        from opentelemetry._logs import get_logger_provider
        get_logger_provider().force_flush(10000)
    except Exception:  # noqa: BLE001
        pass

    check("the probe ran inside a real span", len(trace_id) == 32, trace_id[:16])

    found_line = 0
    if trace_id:
        deadline = time.time() + 60
        while time.time() < deadline and not found_line:
            time.sleep(4)
            q = urllib.parse.quote(
                '{service_name="memory-api"} | trace_id=`' + trace_id + "`")
            now_ns = int(time.time() * 1e9)
            try:
                d = get_json(f"{LOKI}/loki/api/v1/query_range?query={q}"
                             f"&start={now_ns - 900 * 10**9}&end={now_ns}&limit=10")
                found_line = sum(len(r["values"]) for r in d["data"]["result"])
            except (urllib.error.URLError, OSError, KeyError):
                continue

    # This is the whole point of exporting application logs over OTLP: a trace
    # says the embedder took 480 ms, the log line inside that span says which
    # model and batch size. Joined, they are one investigation.
    check("logs are retrievable by trace id", found_line >= 1,
          f"{found_line} lines for trace {trace_id[:12]}")

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    import urllib.parse
    main()
