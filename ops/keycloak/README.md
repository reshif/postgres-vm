# Local identity provider (verification only)

    docker compose --profile auth up -d keycloak
    sh ops/verify-oauth.sh

## Why this exists

`tests/test_auth.py` already proves the hard parts against a synthetic signing
key: `alg: none` rejection, an HS256 token signed with the public key, expiry,
audience, issuer, and that a forged project claim is refused at the API boundary
before any handler runs.

What it cannot prove is the part that only breaks on contact with a real
provider — OIDC discovery, a JWKS served over HTTP with real key ids and
rotation, and whether the claims an actual IdP emits are the claims
`auth.resolve_scope` expects. A verification that mints its own tokens will pass
on the day the real integration fails.

## Why the claims come from user attributes

`org` and `project` arrive as user attributes rather than hardcoded client
claims, so one realm carries several users bound to different projects. That is
what makes the negative tests possible:

| user | org / project | expected |
|---|---|---|
| `curator` | `tenant-e5a1e5a1` / `project-e5a1e5a1` | resolves, reads its own project |
| `mallory` | a project that does not exist | 403, worded identically to "not granted" |
| `noclaims` | no binding claims at all | 403, refused before any query |

`mallory` matters most. `resolve_scope` deliberately returns the same refusal for
"unknown project" and "not granted", because distinguishing them turns a 403 into
a directory of every project on the server.

## The JSON has no comments in it

Keycloak's realm importer rejects unrecognised fields outright — a `_comment` key
fails the whole import with `Unrecognized field`. That is why this file exists
instead.

## Not a production configuration

`start-dev` disables HTTPS enforcement and uses an in-memory database, so the
realm is rebuilt from the import on every start and no state survives to drift
from the file. The passwords here are fixed and public on purpose: they protect
nothing, and a generated one would only make the verification unrepeatable.

In production the issuer is whatever the operator already runs. Point
`MEMORY_OAUTH_ISSUER` and `MEMORY_OAUTH_AUDIENCE` at it and the same code path
applies — that is the point of verifying it here first.
