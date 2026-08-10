---
id: PROC-0005
title: Verify the cross-client MCP milestone
status: active
date: 2026-08-10
---

# Verify the cross-client MCP milestone

Run this once two real MCP clients are configured against the same project. The
automated `test_mcp` suite proves the transport, isolation boundary and event
trail; this procedure supplies the human-visible evidence required by Phase 4.

## Preconditions

- The stack is ready and the MCP gateway reports `bound: true` at
  `http://localhost:8081/healthz`.
- Client A and Client B are distinct clients or installations, each configured
  with the memory MCP endpoint and the same intended project binding.
- The project has a reviewer who can promote a quarantined agent-written memory.

## Run

1. In a new Client A session, call `memory_write` with an `episode`, a short
   title and a unique marker in the content. Record the returned memory id. It
   must report `tier: inferred` and `status: quarantined`.
2. In the console Inbox, review that memory and promote it to `observed`. Confirm
   the status becomes `active`; do not promote it to authoritative.
3. Open a fresh Client B session. Call `memory_context` with a task containing
   the unique marker. The returned pack must contain the recorded memory without
   the `unverified` marker.
4. Call `memory_explain` with the returned `pack_id`. Confirm its `query_text`,
   `returned_ids`, `ranking_profile` and score decomposition match the pack.
5. Record the client names, memory id, pack id and the retrieval-event output in
   the Phase 4 evidence log. The event is the authoritative proof of which
   project produced the pack.

The acceptance fails if Client B requires a copied conversation, a manually
supplied tenant/project UUID, or any foreign-project memory appears in the pack.
Those scope values must always resolve at the server boundary.
