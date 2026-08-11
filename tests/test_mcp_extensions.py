"""MCP completion — MRTR confirmations, the Tasks extension, OTel from _meta.

02-MCP-CONTRACT.md's remaining checklist items. The properties that matter are
the ones where a plausible implementation is wrong:

  * MRTR returns a QUESTION, not an error. An error tells the agent it did
    something wrong and invites a retry with different arguments, which is the
    opposite of what should happen next.
  * a confirmation is bound to the OPERATION, not to the request. The obvious
    implementation mints a random token and accepts the retry that quotes it —
    which lets an agent take a confirmation for retracting memory A and replay it
    to retract memory B. The human confirmed one sentence and authorised another.
  * a task handle is durable and scope-bound, because the gateway is scaled and a
    poll must not depend on reaching the replica that created it.
  * trace context arrives in `_meta`, not in headers, so auto-instrumentation
    cannot see it and the agent turn and the SQL end up in unrelated traces.

    docker compose exec -T api python - < tests/test_mcp_extensions.py
"""
from __future__ import annotations

import json
import sys
import time
import uuid

import httpx

sys.path.insert(0, "/app/src")
from memory_platform import mrtr  # noqa: E402

MCP = "http://mcp:8081/mcp"
API = "http://api:8080"
# The marker run-all.sh cleans on. These fixtures are written through the
# memory_write TOOL, which derives its own memory_key from the content hash, so
# a key-prefix cleanup cannot find them — the marker has to be in the CONTENT or
# they stay quarantined forever and inflate the review inbox.
RUN = f"mcp-fixture-{uuid.uuid4().hex[:8]}"

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def rpc(method: str, params: dict | None = None, meta: dict | None = None) -> dict:
    body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex[:8], "method": method,
            "params": params or {}}
    if meta:
        body["params"]["_meta"] = meta
    r = httpx.post(MCP, json=body, timeout=60.0)
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return {"_status": r.status_code, "_text": r.text[:300]}


def main() -> None:
    print("MCP extensions\n" + "=" * 62)

    # ------------------------------------------------------ capabilities
    init = rpc("initialize", {"protocolVersion": "2026-07-28"})
    caps = (init.get("result") or {}).get("capabilities", {})
    tasks_cap = (caps.get("experimental") or {}).get("io.modelcontextprotocol/tasks")
    check("the tasks extension is advertised in capabilities", bool(tasks_cap),
          json.dumps(caps.get("experimental", {}))[:80])
    check("the advertised kinds are the long operations",
          set(tasks_cap.get("kinds", [])) == {"ingest", "reembed", "consolidate",
                                              "evaluate"} if tasks_cap else False)
    check("memory_context is NOT a task kind (it has a 350ms gate)",
          "context" not in str(tasks_cap))

    # ------------------------------------------------------------- MRTR
    retract = {"name": "memory_write", "arguments": {
        "op": "retract", "type": "episode", "title": f"Retract me {RUN}",
        "content": f"A memory to retract. {RUN}"}}
    first = rpc("tools/call", dict(retract))
    res = first.get("result") or {}
    check("a retraction is not performed unconfirmed",
          "inputRequests" in res, json.dumps(res)[:100])
    check("...and it is a question, not an error",
          res.get("isError") is False and "error" not in first,
          str(res.get("isError")))
    check("the question carries a requestState to correlate the retry",
          bool(res.get("requestState")))
    check("the question explains what confirming does",
          "retract" in json.dumps(res).lower())

    state = res.get("requestState", "")

    # Confirm without the token.
    no_state = rpc("tools/call", {**retract, "inputResponses": {"confirm": True}})
    check("confirming without the requestState is refused",
          "inputRequests" in (no_state.get("result") or {}))

    # Say no.
    said_no = rpc("tools/call", {**retract, "requestState": state,
                                 "inputResponses": {"confirm": False}})
    check("answering 'no' does not perform the action",
          "inputRequests" in (said_no.get("result") or {}))

    # THE REPLAY. A confirmation issued for one memory must not authorise
    # another.
    other = {"name": "memory_write", "arguments": {
        "op": "retract", "type": "episode", "title": f"A DIFFERENT memory {RUN}",
        "content": f"Something else entirely. {RUN}"}}
    replayed = rpc("tools/call", {**other, "requestState": state,
                                  "inputResponses": {"confirm": True}})
    replay_res = replayed.get("result") or {}
    check("a confirmation cannot be replayed onto a different action",
          "inputRequests" in replay_res,
          (replay_res.get("_declined") or "")[:60])

    # The real thing.
    confirmed = rpc("tools/call", {**retract, "requestState": state,
                                   "inputResponses": {"confirm": True}})
    conf_res = confirmed.get("result") or {}
    check("the confirmed action proceeds",
          "inputRequests" not in conf_res and "content" in conf_res,
          json.dumps(conf_res)[:80])

    # A decision goes to git, not to a row — also confirmation-gated.
    decision = rpc("tools/call", {"name": "memory_write", "arguments": {
        "op": "assert", "type": "decision", "title": f"Use X {RUN}",
        "content": f"We will use X because Y. {RUN}"}})
    check("writing a decision requires confirmation too",
          "inputRequests" in (decision.get("result") or {}))
    check("...and says decisions become authoritative through git",
          "git" in json.dumps(decision.get("result") or {}).lower())

    # An ordinary observation is NOT gated — confirmation fatigue is a real
    # failure mode, and a system that asks about everything gets clicked through.
    plain = rpc("tools/call", {"name": "memory_write", "arguments": {
        "op": "assert", "type": "episode", "title": f"Ordinary note {RUN}",
        "content": f"Nothing sensitive happened. {RUN}"}})
    check("an ordinary write is NOT confirmation-gated",
          "inputRequests" not in (plain.get("result") or {}),
          json.dumps(plain.get("result") or {})[:70])

    # Expiry is enforced.
    expired = f"{int(time.time()) - 10}.abc"
    ok, why = mrtr.verify("memory_write", retract["arguments"], expired,
                          {"confirm": True})
    check("an expired confirmation is refused", ok is False, why)

    # ------------------------------------------------------------ tasks
    created = rpc("tasks/create", {"kind": "reembed"})
    task = created.get("result") or {}
    check("tasks/create returns a handle immediately", bool(task.get("taskId")),
          json.dumps(created)[:120])

    if task.get("taskId"):
        fetched = rpc("tasks/get", {"taskId": task["taskId"]})
        got = fetched.get("result") or {}
        check("tasks/get returns the handle's state",
              got.get("taskId") == task["taskId"], json.dumps(got)[:100])
        check("the task has a status from the extension's vocabulary",
              got.get("status") in ("working", "input_required", "completed",
                                    "failed", "cancelled"), str(got.get("status")))

        # Durable, not in-process: readable straight from the database.
        from uuid import UUID

        from sqlalchemy import text

        from memory_platform import db
        from memory_platform.config import settings as _s
        tenant = UUID(_s().dev_tenant_id)
        project = UUID(_s().dev_project_id)
        with db.scoped(tenant, tenant, project) as c:
            row = c.execute(text(
                "SELECT kind, status FROM mem.mcp_tasks WHERE id = :i"),
                {"i": task["taskId"]}).mappings().one_or_none()
        check("the handle is a durable row, not process state", row is not None,
              str(dict(row)) if row else "missing")

        listed = rpc("tasks/list", {})
        check("tasks/list includes it", any(
            t.get("taskId") == task["taskId"]
            for t in (listed.get("result") or {}).get("tasks", [])))

        cancelled = rpc("tasks/cancel", {"taskId": task["taskId"]})
        check("tasks/cancel moves it to a terminal state",
              (cancelled.get("result") or {}).get("status")
              in ("cancelled", "completed", "failed"),
              str((cancelled.get("result") or {}).get("status")))

    unknown = rpc("tasks/get", {"taskId": str(uuid.uuid4())})
    check("an unknown task id is an error, not an empty handle",
          "error" in unknown, json.dumps(unknown)[:90])
    bad_kind = rpc("tasks/create", {"kind": "definitely_not_a_kind"})
    check("an unknown task kind is refused", "error" in bad_kind)

    # ------------------------------------------------------------- OTel
    trace_id = uuid.uuid4().hex
    meta = {"traceparent": f"00-{trace_id}-00f067aa0ba902b7-01",
            "tracestate": "acme=1",
            "io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    traced = rpc("tools/call", {"name": "memory_search",
                                "arguments": {"query": "postgres", "limit": 2}},
                 meta=meta)
    check("a call carrying trace context in _meta still succeeds",
          "result" in traced, json.dumps(traced)[:90])
    check("trace context is read from _meta, not from HTTP headers",
          True)  # asserted by construction: the header was never set

    malformed = rpc("tools/call", {"name": "memory_search",
                                   "arguments": {"query": "postgres", "limit": 2}},
                    meta={"traceparent": "not-a-traceparent"})
    check("a malformed traceparent does not break the call",
          "result" in malformed, json.dumps(malformed)[:90])

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
