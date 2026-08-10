"""Project registration and binding resolution.

05-BUILD-PLAN Phase 2: "Server-side project binding verification against the
registry — ambiguous binding is an error with a fix instruction, never a fallback
to a broader scope."

That last clause is a security property, not ergonomics. The tempting behaviour
when a remote matches two projects is to pick one or widen to the org; both
silently blend one project's memory into another's context, which is the exact
failure RLS exists to prevent, arriving with valid credentials.

    docker compose exec -T api python - < tests/test_binding.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import db  # noqa: E402

API = "http://localhost:8080"
RUN = uuid.uuid4().hex[:8]
ORG = f"bind-{RUN}"

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def req(method: str, path: str, body: dict | None = None):
    r = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as x:
            return x.status, json.load(x)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {}


def resolve(url: str):
    return req("GET", "/v1/projects/resolve?" + urllib.parse.urlencode({"repo_url": url}))


def main() -> None:
    # ---- 1. registration ---------------------------------------------------
    print("\n1. Registration")
    s, a = req("POST", "/v1/projects", {
        "org_slug": ORG, "project_slug": "payments", "name": "Payments",
        "repo_url": f"git@github.com:{ORG}/payments.git"})
    check("registers a new project", s == 201 and a.get("created") is True, str(s))
    check("returns a full scope triple",
          all(a.get(k) for k in ("tenant_id", "project_id", "principal_id")))

    s, b = req("POST", "/v1/projects", {
        "org_slug": ORG, "project_slug": "payments",
        "repo_url": f"git@github.com:{ORG}/payments.git"})
    check("re-registration is idempotent", b.get("project_id") == a.get("project_id"))
    check("re-registration reports not-created", b.get("created") is False)

    s, c2 = req("POST", "/v1/projects", {
        "org_slug": ORG, "project_slug": "ledger",
        "repo_url": f"git@github.com:{ORG}/ledger.git"})
    check("a second project in the same org gets its own id",
          c2.get("project_id") != a.get("project_id"))
    check("both projects share one tenant", c2.get("tenant_id") == a.get("tenant_id"))

    # ---- 2. remote normalisation ------------------------------------------
    print("\n2. Remote normalisation (one repo, many spellings)")
    for form in (
        f"git@github.com:{ORG}/payments.git",
        f"https://github.com/{ORG}/payments",
        f"https://github.com/{ORG}/payments.git",
        f"https://github.com/{ORG}/payments/",
        f"GIT@GITHUB.COM:{ORG}/PAYMENTS.GIT",
    ):
        s, r = resolve(form)
        check(f"resolves {form[:44]}",
              s == 200 and r.get("project_id") == a.get("project_id"), str(s))

    # ---- 3. refusals -------------------------------------------------------
    print("\n3. Refusals (the part that must never guess)")
    s, r = resolve(f"git@github.com:{ORG}/does-not-exist.git")
    check("unknown remote -> 404", s == 404, str(s))
    check("404 says how to fix it", "memory init" in str(r.get("detail", "")))

    # Two projects claiming one remote: a re-init under a different slug, or a
    # fork registered in the same org.
    req("POST", "/v1/projects", {
        "org_slug": ORG, "project_slug": "payments-v2",
        "repo_url": f"https://github.com/{ORG}/payments"})
    s, r = resolve(f"git@github.com:{ORG}/payments.git")
    check("ambiguous binding -> 409, never a guess", s == 409, str(s))
    detail = str(r.get("detail", ""))
    check("409 names both claimants",
          "payments" in detail and "payments-v2" in detail, detail[:70])
    check("409 does not fall back to the org",
          "project_id" not in r, str(list(r))[:40])

    s, r = req("POST", "/v1/projects", {
        "org_slug": ORG, "project_slug": "ledger",
        "repo_url": f"git@github.com:{ORG}/something-else.git"})
    check("refuses to silently repoint an existing project", s == 409, str(s))
    check("repoint refusal explains the current binding",
          "already bound" in str(r.get("detail", "")), str(r.get("detail", ""))[:60])

    # ---- 4. the registry is not RLS-protected, and that is deliberate ------
    print("\n4. Registry visibility")
    # projects/principals are named exemptions in test_rls_coverage.py because
    # binding must resolve BEFORE a scope exists. Assert the reason still holds:
    # resolution works with no scope context set at all.
    with db.engine().connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM mem.projects WHERE slug = 'payments'")
                         ).scalar_one()
    check("registry is readable without a scope (binding needs this)", n >= 1, str(n))

    # ...but it must not leak memory content.
    with db.engine().connect() as conn:
        m = conn.execute(text("SELECT count(*) FROM mem.memories")).scalar_one()
    check("memories remain invisible without a scope", m == 0, str(m))

    # No cleanup here on purpose. memory_app holds no DELETE grant
    # (01-SCHEMA.sql: "deletion is an explicit, audited admin operation"), and a
    # test that needs a privilege the application role does not have is a test
    # running as the wrong role. tests/run-all.sh purges the `bind-%` orgs
    # owner-side after the suites finish.

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
