"""OAuth token verification and scope resolution (ADR-0004).

ADR-0004: "Memory identity, authorization and project binding never derive from
client-supplied IDs."

The forgery cases are the point of this suite. `alg: none` and
HS256-signed-with-the-RSA-public-key are the two classic JWT breaks, and both
work against any implementation that reads the algorithm out of the token header
instead of pinning it. They are cheap to test and catastrophic to miss.

Runs fully offline: it generates a keypair, signs its own tokens, and injects the
public key. Every claim check — expiry, audience, issuer, required claims — goes
through exactly the code path production uses.

    docker compose exec -T api python - < tests/test_auth.py
"""
from __future__ import annotations

import sys
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import auth, db  # noqa: E402
from memory_platform.config import Settings  # noqa: E402

RUN = uuid.uuid4().hex[:8]
ORG = f"auth-{RUN}"
ISSUER = "https://issuer.example.test/"
AUDIENCE = "https://memory.local/mcp"

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


PRIV = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUB = PRIV.public_key()


def make_token(*, alg="RS256", key=None, iss=ISSUER, aud=AUDIENCE, sub="user-1",
               exp_delta=300, org=ORG, project="app", extra=None, omit=()):
    claims = {"iss": iss, "aud": aud, "sub": sub,
              "exp": int(time.time()) + exp_delta, "iat": int(time.time()),
              "org": org, "project": project, **(extra or {})}
    for k in omit:
        claims.pop(k, None)
    return jwt.encode(claims, key if key is not None else PRIV, algorithm=alg)


def with_settings(**over):
    """Point auth at a test configuration without touching the environment."""
    base = dict(oauth_issuer=ISSUER, oauth_audience=AUDIENCE,
                oauth_algorithms="RS256", oauth_leeway_s=5,
                oauth_org_claim="org", oauth_project_claim="project")
    base.update(over)
    # Only the real (lru_cached) settings has cache_clear; after the first call
    # this attribute is the stub installed below.
    clear = getattr(auth.settings, "cache_clear", None)
    if clear:
        clear()
    auth.settings = lambda: Settings(**base)  # type: ignore[assignment]


def main() -> None:
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO mem.organizations (id,slug,name) "
                       "VALUES (gen_random_uuid(),:s,:s) ON CONFLICT DO NOTHING"),
                  {"s": ORG})
        c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                       "SELECT gen_random_uuid(), o.id, 'app', 'App' "
                       "  FROM mem.organizations o WHERE o.slug = :s "
                       "ON CONFLICT DO NOTHING"), {"s": ORG})

    with_settings()
    auth.configure_test_key(PUB)

    # ---- 1. the happy path -------------------------------------------------
    print("\n1. A valid token")
    claims = auth.verify_token(make_token())
    check("verifies and returns claims", claims["sub"] == "user-1", str(claims.get("sub")))
    check("carries the org claim", claims["org"] == ORG)

    # ---- 2. forgery: algorithm confusion -----------------------------------
    # The two breaks that work against any implementation trusting header.alg.
    print("\n2. Algorithm confusion (the classic JWT breaks)")
    unsigned = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "attacker",
                           "exp": int(time.time()) + 300, "org": ORG,
                           "project": "app"}, key="", algorithm="none")
    try:
        auth.verify_token(unsigned)
        check("`alg: none` is rejected", False, "ACCEPTED AN UNSIGNED TOKEN")
    except auth.AuthError:
        check("`alg: none` is rejected", True)

    # HS256 signed with the RSA PUBLIC key: anyone who can read the JWKS can do
    # this, and it validates on any server that trusts header.alg.
    #
    # Hand-rolled, because PyJWT's own encode() refuses to use an asymmetric key
    # as an HMAC secret — a good guardrail, and precisely why the forgery has to
    # be built manually to prove our VERIFIER rejects it rather than relying on
    # the library declining to produce it.
    import base64, hashlib, hmac, json as _json
    from cryptography.hazmat.primitives import serialization

    pub_pem = PUB.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    head = b64(_json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(_json.dumps({"iss": ISSUER, "aud": AUDIENCE, "sub": "attacker",
                               "exp": int(time.time()) + 300, "org": ORG,
                               "project": "app"}).encode())
    signing_input = head + b"." + payload
    sig = b64(hmac.new(pub_pem, signing_input, hashlib.sha256).digest())
    hs = (signing_input + b"." + sig).decode()

    try:
        auth.verify_token(hs)
        check("HS256-signed-with-the-public-key is rejected", False, "ACCEPTED A FORGERY")
    except auth.AuthError:
        check("HS256-signed-with-the-public-key is rejected", True)

    # ---- 3. signature and claim checks -------------------------------------
    print("\n3. Signature and standard claims")
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for label, tok in [
        ("a token signed by a different key", make_token(key=other)),
        ("an expired token", make_token(exp_delta=-3600)),
        ("a token for another audience", make_token(aud="https://elsewhere/")),
        ("a token from an untrusted issuer", make_token(iss="https://evil.test/")),
        ("a token with no subject", make_token(omit=("sub",))),
        ("a token with no expiry", make_token(omit=("exp",))),
        ("a structurally broken token", "not.a.jwt"),
    ]:
        try:
            auth.verify_token(tok)
            check(f"{label} is rejected", False, "ACCEPTED")
        except auth.AuthError:
            check(f"{label} is rejected", True)

    print("\n4. Rejection messages help without leaking")
    try:
        auth.verify_token(make_token(exp_delta=-3600))
    except auth.AuthError as exc:
        check("expiry is named specifically", "expired" in str(exc), str(exc))
    try:
        auth.verify_token(make_token(aud="https://elsewhere/"))
    except auth.AuthError as exc:
        check("audience mismatch is named specifically", "audience" in str(exc), str(exc))

    # ---- 5. clock skew -----------------------------------------------------
    print("\n5. Clock skew")
    check("a token expiring within the leeway still verifies",
          auth.verify_token(make_token(exp_delta=-2))["sub"] == "user-1")

    # ---- 6. scope resolution ----------------------------------------------
    print("\n6. Scope resolution — slugs in, UUIDs out")
    with db.engine().begin() as c:
        scope = auth.resolve_scope(c, auth.verify_token(make_token()))
    check("resolves to a real project", scope.project_slug == "app", scope.project_slug)
    check("returns server-side UUIDs, never client ones",
          str(scope.tenant_id) != ORG and len(str(scope.project_id)) == 36)
    check("provisions a principal for the subject", scope.principal_id is not None)
    check("as_params gives exactly the scope triple",
          set(scope.as_params()) == {"tenant_id", "project_id", "principal_id"})

    with db.engine().begin() as c:
        again = auth.resolve_scope(c, auth.verify_token(make_token()))
    check("the same subject resolves to the same principal",
          again.principal_id == scope.principal_id)

    # ---- 7. a token cannot name a project it was not granted --------------
    print("\n7. Claims cannot conjure access")
    with db.engine().begin() as c:
        for label, tok in [
            ("an unknown project", make_token(project="does-not-exist")),
            ("an unknown org", make_token(org="no-such-org")),
            ("no binding claims at all", make_token(omit=())),
        ]:
            if label == "no binding claims at all":
                tok = make_token(org="", project="")
            try:
                auth.resolve_scope(c, auth.verify_token(tok))
                check(f"{label} is refused", False, "GRANTED")
            except auth.Forbidden:
                check(f"{label} is refused", True)

        # The two refusals must be indistinguishable, or 403 becomes a directory.
        msgs = []
        for tok in (make_token(project="does-not-exist"), make_token(org="no-such-org")):
            try:
                auth.resolve_scope(c, auth.verify_token(tok))
            except auth.Forbidden as exc:
                msgs.append(str(exc))
    check("unknown-project and unknown-org are indistinguishable",
          len(set(msgs)) == 1, str(set(msgs)))

    # ---- 8. disabled means disabled ---------------------------------------
    print("\n8. With OAuth unconfigured")
    with_settings(oauth_issuer="")
    check("auth reports itself disabled", auth.enabled() is False)
    try:
        auth.verify_token(make_token())
        check("verification refuses rather than passing silently", False, "VERIFIED")
    except auth.AuthError as exc:
        check("verification refuses rather than passing silently",
              "not configured" in str(exc), str(exc)[:40])

    # ---- 9. bearer parsing -------------------------------------------------
    print("\n9. Authorization header parsing")
    check("extracts a bearer token", auth.bearer("Bearer abc.def.ghi") == "abc.def.ghi")
    check("is case-insensitive on the scheme", auth.bearer("bearer x") == "x")
    for bad in (None, "", "abc.def.ghi", "Basic dXNlcjpwYXNz", "Bearer"):
        try:
            auth.bearer(bad)
            check(f"rejects {bad!r}", False, "ACCEPTED")
        except auth.AuthError:
            check(f"rejects {bad!r}", True)

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
