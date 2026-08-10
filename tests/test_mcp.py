"""MCP cross-session integration proof against the running gateway.

This deliberately uses two independent JSON-RPC client handshakes rather than
calling the dispatcher in-process. It verifies the local dev binding at the
network boundary, the resource surface, conservative agent-write behavior, and
the retrieval event left for a later client to inspect.

    docker compose exec -T api python - < tests/test_mcp.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid


API = "http://localhost:8080"
MCP = "http://mcp:8081/mcp"
RUN = uuid.uuid4().hex[:8]
MARKER = f"mcp-cross-client-{RUN}"
FOREIGN_MARKER = f"mcp-foreign-{RUN}"
NO_EVIDENCE_QUERY = f"which unrecorded Zorblax archive policy applies to pgvector {RUN}"

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def request_json(url: str, payload: dict, *, client: str) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": client},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            return exc.code, {}


def api_request(path: str, body: dict) -> tuple[int, dict]:
    return request_json(API + path, body, client="mcp-cross-client-fixture")


def mcp_call(client: str, request_id: int, method: str, params: dict) -> tuple[int, dict]:
    return request_json(MCP, {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }, client=client)


def initialize(client: str, request_id: int) -> dict:
    status, payload = mcp_call(client, request_id, "initialize", {
        "protocolVersion": "2026-07-28",
        "capabilities": {},
        "clientInfo": {"name": client, "version": "acceptance"},
    })
    result = payload.get("result", {})
    check(f"{client} completes its own MCP handshake",
          status == 200 and result.get("protocolVersion") == "2026-07-28", str(payload)[:140])
    return result


def structured(payload: dict) -> dict:
    return payload.get("result", {}).get("structuredContent", {})


def all_items(pack: dict) -> list[dict]:
    return [item for section in pack.get("sections", {}).values() for item in section]


def main() -> None:
    binding = {key: os.environ.get(key, "") for key in (
        "MEMORY_DEV_TENANT_ID", "MEMORY_DEV_PROJECT_ID", "MEMORY_DEV_PRINCIPAL_ID")}
    check("the local MCP gateway has an explicit development binding", all(binding.values()))

    # Client one proposes knowledge. It must be quarantined: an agent cannot
    # promote its own statement merely by reaching the MCP write tool.
    initialize("writer-client", 1)
    status, write = mcp_call("writer-client", 2, "tools/call", {
        "name": "memory_write",
        "arguments": {
            "type": "episode",
            "title": f"Cross-client fixture {MARKER}",
            "content": f"The exact fixture marker is {MARKER}.",
        },
    })
    write_result = structured(write)
    written_id = str(write_result.get("id") or "")
    check("writer client records a quarantined memory",
          status == 200 and write_result.get("tier") == "inferred"
          and write_result.get("status") == "quarantined" and bool(written_id),
          str(write_result))

    # Client two starts from a distinct handshake and must not receive the
    # unreviewed write by default. It can request it explicitly, and the result
    # must remain visibly marked as unverified.
    initialize("reader-client", 3)

    status, no_evidence_response = mcp_call("reader-client", 17, "tools/call", {
        "name": "memory_search",
        "arguments": {"query": NO_EVIDENCE_QUERY, "limit": 5},
    })
    no_evidence = structured(no_evidence_response)
    check("MCP search reports absent project evidence instead of nearest memories",
          status == 200 and no_evidence.get("count") == 0
          and no_evidence.get("results") == []
          and no_evidence.get("answerability", {}).get("status") == "no_relevant_evidence"
          and "No relevant evidence" in no_evidence.get("notice", ""),
          str(no_evidence)[:180])

    status, no_evidence_pack_response = mcp_call("reader-client", 18, "tools/call", {
        "name": "memory_context",
        "arguments": {"task": NO_EVIDENCE_QUERY, "token_budget": 4000},
    })
    no_evidence_pack = structured(no_evidence_pack_response)
    pack_items = all_items(no_evidence_pack)
    check("MCP context separates no evidence from baseline project constraints",
          status == 200 and no_evidence_pack.get("evidence_count") == 0
          and no_evidence_pack.get("answerability", {}).get("status") == "no_relevant_evidence"
          and all(item.get("context_role") == "baseline" for item in pack_items)
          and "No relevant evidence" in no_evidence_pack.get("notice", ""),
          str(no_evidence_pack)[:180])

    status, resource_list_response = mcp_call("reader-client", 8, "resources/list", {})
    resource_result = resource_list_response.get("result", {})
    resources = resource_result.get("resources", [])
    profile = next((item for item in resources if item.get("name") == "Project profile"), None)
    resources_by_name = {item.get("name"): item for item in resources}
    check("resources/list returns bound project resources with private caching",
          status == 200 and len(resources) == 5 and profile is not None
          and resource_result.get("cacheScope") == "private"
          and resource_result.get("ttlMs") == 300000, str(resource_result)[:180])

    status, template_response = mcp_call("reader-client", 9, "resources/templates/list", {})
    templates = template_response.get("result", {}).get("resourceTemplates", [])
    check("resource templates advertise memory and entity reads",
          status == 200 and {item.get("uriTemplate") for item in templates} == {
              "memory://memory/{ref}", "memory://entity/{id}"}, str(templates))

    status, profile_response = mcp_call("reader-client", 10, "resources/read", {
        "uri": profile.get("uri") if profile else "",
    })
    contents = profile_response.get("result", {}).get("contents", [])
    check("profile resource returns the scoped project profile",
          status == 200 and len(contents) == 1 and contents[0].get("mimeType") == "text/markdown"
          and "Project profile" in contents[0].get("text", ""), str(contents)[:180])

    # Every concrete project resource advertised above must resolve through the
    # same bound gateway. Dynamic memory/entity resources are templates because
    # their identifiers are not knowable until the caller has discovered one.
    for request_id, name, marker, mime_type in (
        (12, "Project state", "Current knowledge", "text/markdown"),
        (13, "Project timeline", "Significant events", "text/markdown"),
        (14, "Procedure index", "Procedure index", "text/markdown"),
        (15, "Unresolved conflicts", '"conflicts"', "application/json"),
    ):
        item = resources_by_name.get(name, {})
        status, response = mcp_call("reader-client", request_id, "resources/read", {
            "uri": item.get("uri", ""),
        })
        resource_contents = response.get("result", {}).get("contents", [])
        text_body = resource_contents[0].get("text", "") if resource_contents else ""
        check(f"{name.lower()} resource resolves under the bound project",
              status == 200 and len(resource_contents) == 1
              and resource_contents[0].get("mimeType") == mime_type and marker in text_body,
              str(resource_contents)[:180])

    status, memory_response = mcp_call("reader-client", 16, "resources/read", {
        "uri": f"memory://memory/{written_id}",
    })
    memory_contents = memory_response.get("result", {}).get("contents", [])
    memory_body = memory_contents[0].get("text", "") if memory_contents else ""
    check("memory resource expands the in-scope memory with its lifecycle",
          status == 200 and len(memory_contents) == 1
          and memory_contents[0].get("mimeType") == "application/json"
          and json.loads(memory_body).get("memory", {}).get("id") == written_id
          and json.loads(memory_body).get("memory", {}).get("status") == "quarantined",
          memory_body[:180])

    status, default_pack_response = mcp_call("reader-client", 4, "tools/call", {
        "name": "memory_context",
        "arguments": {"task": MARKER, "token_budget": 4000},
    })
    default_pack = structured(default_pack_response)
    check("default context excludes the agent-written memory",
          status == 200 and MARKER not in json.dumps(all_items(default_pack)) and
          written_id not in {str(item.get("ref")) for item in all_items(default_pack)},
          str(default_pack.get("pack_id")))

    status, reviewed_pack_response = mcp_call("reader-client", 5, "tools/call", {
        "name": "memory_context",
        "arguments": {
            "task": MARKER,
            "token_budget": 4000,
            "include_unverified": True,
        },
    })
    reviewed_pack = structured(reviewed_pack_response)
    matching = [item for item in all_items(reviewed_pack) if str(item.get("ref")) == written_id]
    check("a new client can explicitly retrieve the writer's quarantined memory",
          status == 200 and len(matching) == 1 and matching[0].get("unverified") is True,
          str(matching))

    # The reader checks the stored event through the same MCP boundary. This is
    # the audit proof that a pack belongs to the server-side project binding.
    pack_id = reviewed_pack.get("pack_id")
    status, explain_response = mcp_call("reader-client", 6, "tools/call", {
        "name": "memory_explain",
        "arguments": {"pack_id": pack_id},
    })
    event = structured(explain_response)
    check("the reader can inspect the recorded retrieval event",
          status == 200 and event.get("tool") == "memory_context"
          and event.get("query_text") == MARKER and written_id in event.get("returned_ids", []),
          str(event)[:180])

    # Seed active foreign-project content through the API, then deliberately
    # smuggle its UUIDs into the MCP arguments. The gateway does not forward
    # caller-supplied scope, so the bound project remains the only one searched.
    foreign_org = f"mcp-{RUN}"
    status, foreign_scope = api_request("/v1/projects", {
        "org_slug": foreign_org,
        "project_slug": "foreign",
        "name": "MCP foreign fixture",
        "repo_url": f"git@example.test:{foreign_org}/foreign.git",
    })
    check("creates an unrelated-project fixture", status == 201 and bool(foreign_scope.get("project_id")),
          str(foreign_scope)[:140])
    status, foreign_memory = api_request("/v1/memories", {
        **{key: foreign_scope[key] for key in ("tenant_id", "project_id", "principal_id")},
        "type": "decision",
        "title": f"Foreign fixture {FOREIGN_MARKER}",
        "content": f"This active memory belongs only to {FOREIGN_MARKER}.",
        "source_type": "human",
        "memory_key": f"mcp-cross-client:foreign:{RUN}",
    })
    foreign_id = str(foreign_memory.get("id") or "")
    check("foreign fixture is active", status == 201 and foreign_memory.get("status") == "active", str(foreign_memory))

    status, forged_pack_response = mcp_call("reader-client", 7, "tools/call", {
        "name": "memory_context",
        "arguments": {
            "task": FOREIGN_MARKER,
            "include_unverified": True,
            "tenant_id": foreign_scope.get("tenant_id"),
            "project_id": foreign_scope.get("project_id"),
            "principal_id": foreign_scope.get("principal_id"),
        },
    })
    forged_pack = structured(forged_pack_response)
    visible_or_ranked = all_items(forged_pack) + forged_pack.get("dropped", [])
    check("forged foreign scope cannot cross the MCP project binding",
          status == 200 and FOREIGN_MARKER not in json.dumps(visible_or_ranked)
          and foreign_id not in {str(item.get("ref") or item.get("id")) for item in visible_or_ranked},
          str(forged_pack.get("pack_id")))

    status, foreign_resource_response = mcp_call("reader-client", 11, "resources/read", {
        "uri": f"memory://memory/{foreign_id}",
    })
    foreign_error = foreign_resource_response.get("error", {})
    check("foreign memory resources are indistinguishable from a missing resource",
          status == 200 and foreign_error.get("code") == -32003
          and "404" in foreign_error.get("message", ""), str(foreign_error))

    failed = [name for ok, name in results if not ok]
    print(f"\n{'=' * 62}\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
