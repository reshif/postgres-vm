# Knowledge Console

The curation and debugging instrument for the memory platform. Built in Phase 5,
Review Inbox first, per `03-FRONTEND-KNOWLEDGE-CONSOLE.md` §2.

```sh
docker compose --profile console up -d --build console
# http://localhost:3000
```

Profile-gated, so `docker compose up` never depends on it.

## What is here

| Screen | Spec | State |
| --- | --- | --- |
| Review Inbox | §3.1 | built — keyboard-first, ordered by consequence, undo, reject-with-reason |
| Retrieval Debugger | §3.6 | built — plan, per-arm counts, fusion with score decomposition, dropped-with-reasons, export as eval case |
| Memory Detail | §3.3 | built as a panel — provenance, history, supersessions |
| Conflicts | §3.7 | built — side-by-side with trust and dates, resolution with a reason |
| Project Health | §3.8 | built — transparent composite, formula shipped with the score, kill-switch state |
| Knowledge Explorer | §3.2 | not built |
| Entity Graph | §3.4 | not built |
| Timeline | §3.5 | not built |
| Evals | §3.9 | not built |

The unbuilt four are the ones §2 marks as "everything else follows". The graph in
particular is the trap the spec calls out: it demos well and the Inbox is what
keeps the system alive.

## Why this is not Next.js

The spec names Next.js, TanStack Table/Virtual, and Sigma.js + graphology. This
is instead ~900 lines of dependency-free ES2020 served by nginx. That is a real
deviation and worth being explicit about.

The reasoning: everything currently built is read-mostly and low-cardinality —
a review queue a human clears in three minutes, a debugger showing one pack. None
of it needs virtualisation, a router, or a rendering framework, and a `node_modules`
tree would have been the largest dependency in a project whose entire runtime is
otherwise Postgres and Python. The screens that genuinely justify those libraries
are the Explorer (thousands of rows, virtualised) and the Graph (WebGL over 5k
nodes) — both unbuilt. **Build those on the specified stack when you build them.**
Nothing here blocks it: the console talks to the same HTTP API any frontend would.

What was not traded away: the design tokens, the trust ramp, keyboard-first
triage, and the accessibility and security properties below.

## Two constraints that shaped the code

**Nothing from the database is ever assigned as HTML.** This console renders
quarantined memories, and quarantined memories are exactly where prompt-injection
payloads live — a reviewer reading the inbox is reading attacker-controlled text
by design. Every API value goes through `el()`, which sets `textContent`. `raw()`
exists for markup this file wrote itself and is used in three places, none of
which touch API data. nginx additionally serves `script-src 'self'` with no
`unsafe-eval`, so a stored-XSS bug could not fetch a remote script to escalate
with. `tests/test_console.js` writes a live `<img onerror>` payload through the
API and asserts nothing executes.

**The console never touches the database** (§1 principle 2). Every write goes
through the same API endpoints, and therefore the same RLS, policy engine and
audit trail, as an MCP write. There is no privileged path here.

## Scope and auth

`GET /v1/console/config` returns the dev binding when one is set, so the console
opens on a working screen. With no binding it asks for a tenant rather than
guessing one — guessing means showing an operator someone else's queue. When
OAuth lands (`MCP_OAUTH_ISSUER`), that endpoint is where the session identity
replaces the dev binding.

## Tests

```sh
docker compose --profile console up -d --build console
sh tests/run-console.sh          # 51 assertions, real Chromium, real API
```

Runs Playwright from its own image on the compose network — no browser or Node
toolchain on the host, nothing installed outside a container. Kept out of
`run-all.sh` on purpose: the browser image is a large pull, and someone working
on retrieval should not have to download Chromium to run the test suite.

Four defects this suite caught that reading the code did not:

- the selection highlight repainted `border-color` and wiped the trust ramp on
  the focused row — the one row whose trust the reviewer most needs;
- `.item`'s border overrode the trust ramp for *every* row, because both are
  single-class selectors and source order decided;
- undo of a promotion was implemented as a rejection, which is not an inverse and
  failed outright (`reject` requires `quarantined`; a promoted memory is `active`);
- the Dropped stage disappeared when empty, so "nothing was dropped" and "the
  debugger did not report" looked identical.
