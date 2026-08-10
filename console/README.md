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
| Memory Detail | §3.3 | built — content, provenance links, history, relations, usage, raw record, and scoped lifecycle actions |
| Conflicts | §3.7 | built — side-by-side with trust and dates, resolution with a reason |
| Project Health | §3.8 | built — transparent composite, formula shipped with the score, kill-switch state |
| Knowledge Explorer | §3.2 | built — virtualized table, server-side as-of filtering, URL-shaped filters, saved views, and audited archive/pin/re-embed actions |
| Entity Graph | §3.4 | built — bounded two-hop Sigma.js view, shared time cursor, and accessible relationship table |
| Timeline | §3.5 | built — distinct valid and record-time lanes, with the cursor shared across explorer and graph |
| Evals | §3.9 | built — persisted run history, recall trend, and per-case evidence |

## Architecture

The console uses the specified Next.js App Router stack with TanStack Query,
Table and Virtual, Sigma.js + graphology, and visx. It is statically exported
and nginx proxies only `/v1/` to the API, so the browser never has a database
credential or a privileged console-only write path.

What was not traded away: the design tokens, the trust ramp, keyboard-first
triage, and the accessibility and security properties below.

## Two constraints that shaped the code

**Nothing from the database is ever assigned as HTML.** This console renders
quarantined memories, and quarantined memories are exactly where prompt-injection
payloads live. React renders API values as text and the application contains no
`dangerouslySetInnerHTML` path. nginx additionally serves a strict script CSP,
so a stored-XSS bug cannot fetch a remote script to escalate. The browser suite
writes a live `<img onerror>` payload through the API and asserts nothing
executes.

**The console never touches the database** (§1 principle 2). Every write goes
through the same API endpoints, and therefore the same RLS, policy engine and
audit trail, as an MCP write. There is no privileged path here.

## Scope and auth

`GET /v1/console/config` returns the development binding when one is set, so
the console opens on a working screen. Without one, development operators enter
the tenant and project UUIDs created by `memory init`; the console never guesses
a scope.

An OAuth-enabled deployment uses a public OIDC client and authorization-code
flow with PKCE. Set `CONSOLE_OIDC_CLIENT_ID`, register the console URL as its
redirect URI, and configure either the issuer discovery settings or both OIDC
endpoints. The access token lives only in tab session storage; the API verifies
it and resolves the tenant, project, and principal from its claims. Browser
supplied scope IDs cannot select another project.

## Tests

```sh
docker compose --profile console up -d --build console
sh tests/run-console.sh          # 33 assertions, real Chromium, real API
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
