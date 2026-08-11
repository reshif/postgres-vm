"""`memory` — the project-facing CLI (05-BUILD-PLAN Phase 2).

    memory init            scaffold legacy .memory/ knowledge and register the project
    memory init --github   bind source and evidence repositories without a checkout
    memory status          what this repo is bound to, and what the platform holds
    memory search <query>  search this project's memory
    memory why <topic>     the rationale questions, specifically
    memory doctor          diagnose a setup that is not working

STANDARD LIBRARY ONLY, ON PURPOSE. This runs on a developer's machine, in a repo
that has nothing to do with this codebase, quite possibly before the platform is
even reachable. `memory doctor` has to work when everything else is broken, so it
cannot depend on the package's own runtime deps being installed.

It talks to the context API over HTTP and holds no database credentials. Same
reason as the MCP gateway: the security perimeter stays in one auditable place.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = os.environ.get("MEMORY_API_URL", "http://localhost:8080").rstrip("/")
LEGACY_BINDING_FILE = ".memory/binding.json"
GITHUB_BINDING_FILE = ".memory-platform/binding.json"


# ----------------------------------------------------------------- plumbing
def _req(method: str, path: str, body: dict | None = None, timeout: float = 120.0):
    url = f"{API}{path}"
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {}
    except urllib.error.URLError as e:
        die(f"cannot reach the memory API at {API}: {e.reason}\n"
            f"       is the stack up?  docker compose up -d --wait")


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, timeout=15)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def repo_root() -> Path:
    top = git("rev-parse", "--show-toplevel")
    if not top:
        die("not inside a git repository (memory binds to a git remote)")
    return Path(top)


def remote_url() -> str:
    return git("remote", "get-url", "origin")


def binding_path(root: Path) -> Path | None:
    """Return the binding without making a GitHub project create `.memory/`."""
    for relative in (GITHUB_BINDING_FILE, LEGACY_BINDING_FILE):
        candidate = root / relative
        if candidate.is_file():
            return candidate
    return None


def load_binding(root: Path) -> dict:
    path = binding_path(root)
    if path is None:
        die("this repo is not bound to a project (.memory-platform/binding.json or "
            ".memory/binding.json missing).\n       run:  memory init --github")
    return json.loads(path.read_text(encoding="utf-8"))


def scope_params(b: dict) -> dict:
    return {"tenant_id": b["tenant_id"], "project_id": b["project_id"],
            "principal_id": b.get("principal_id", "")}


# --------------------------------------------------------------------- init
PROJECT_YAML = """---
id: project
title: {name}
status: active
---

# {name}

## Hard constraints

<!-- The highest-leverage file in the system: it is in every context pack.
     Keep it under 400 tokens and review it quarterly. -->

- (replace me) e.g. "All persistence goes through the repository layer."

## Stack

- (replace me)
"""

CONVENTIONS = """---
id: conventions
title: Team conventions
status: active
---

# Conventions

<!-- Things an agent must follow that are not obvious from the code. -->

- (replace me)
"""

GLOSSARY = """---
id: glossary
title: Glossary
status: active
---

# Glossary

<!-- Entities and their canonical names, so retrieval can resolve them. -->

**(term)** — (definition)
"""

AGENTS_SECTION = """
## Project memory

This repository is bound to a memory platform project. Durable knowledge lives in
`.memory/` and is ingested automatically.

- Before non-trivial work, call `memory_context` with the task.
- Record decisions as files in `.memory/decisions/`, reviewed through a PR.
- Anything you write through `memory_write` is quarantined until a human promotes
  it. That is deliberate — do not work around it.
"""

GITHUB_AGENTS_SECTION = """
## Project knowledge

This repository is bound to a GitHub-native knowledge project. Durable
engineering knowledge is reviewed in the paired evidence repository, not stored
in a host-local `.memory/` directory.

- Before non-trivial work, call `memory_context` with the task.
- Propose assertions and evaluation cases through a pull request to the evidence
  repository, each citing immutable source commit SHAs.
- An agent session may propose evidence but cannot accept its own claim.
"""


def cmd_init(args) -> int:
    root = repo_root()
    remote = remote_url()
    if not remote:
        print("! no `origin` remote; the project will be registered without a "
              "repo binding and cannot be auto-resolved later.")

    org = args.org or "default"
    slug = args.project or root.name
    github_native = bool(args.github)
    if github_native:
        if not remote:
            die("a GitHub-native project requires an `origin` GitHub remote")
        if not args.evidence_repo:
            die("--github requires --evidence-repo https://github.com/<org>/<project>-evidence")
        if not args.installation_id:
            die("--github requires --installation-id from the GitHub App installation")

    print(f"repository : {root}")
    print(f"remote     : {remote or '(none)'}")
    print(f"project    : {org}/{slug}")

    # 1. The legacy path scaffolds local knowledge. GitHub-native projects keep
    # their durable claims in the paired evidence repository and need no local
    # checkout or .memory directory.
    binding_relative = GITHUB_BINDING_FILE if github_native else LEGACY_BINDING_FILE
    if github_native:
        print("knowledge  : GitHub-native evidence repository (no local scaffold)")
    else:
        mem = root / ".memory"
        made = []
        for path, body in (
            (mem / "project.yaml", PROJECT_YAML.format(name=slug)),
            (mem / "conventions.md", CONVENTIONS),
            (mem / "glossary.md", GLOSSARY),
        ):
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
                made.append(path.relative_to(root).as_posix())
        for d in ("decisions", "procedures"):
            (mem / d).mkdir(parents=True, exist_ok=True)
        print(f"scaffolded : {', '.join(made) if made else '(already present)'}")

    # 2. register
    registration = {
        "org_slug": org, "project_slug": slug,
        "name": slug, "repo_url": remote or None,
    }
    if github_native:
        registration.update({
            "source_provider": "github",
            "evidence_repo_url": args.evidence_repo,
            "github_installation_id": args.installation_id,
            "git_default_branch": args.branch or "main",
        })
    status, data = _req("POST", "/v1/projects", registration)
    if status == 409:
        die(f"{data.get('detail', 'binding conflict')}")
    if status not in (200, 201):
        die(f"registration failed ({status}): {data}")
    print(f"registered : project {data['project_id']}"
          f"{' (new)' if data.get('created') else ' (existing)'}")

    # 3. binding file, so every later command knows its scope without guessing
    binding_file = root / binding_relative
    binding_file.parent.mkdir(parents=True, exist_ok=True)
    binding_file.write_text(json.dumps({
        "org_slug": org, "project_slug": slug, "repo_url": remote,
        "tenant_id": data["tenant_id"], "project_id": data["project_id"],
        "principal_id": data["principal_id"], "api": API,
        "source_provider": "github" if github_native else "legacy",
        "evidence_repo_url": args.evidence_repo if github_native else None,
        "git_default_branch": (args.branch or "main") if github_native else None,
    }, indent=2) + "\n", encoding="utf-8")
    print(f"binding    : {binding_relative}")

    # 4. .mcp.json for agent clients
    mcp = root / ".mcp.json"
    if not mcp.exists():
        mcp.write_text(json.dumps({"mcpServers": {"memory-platform": {
            "type": "http", "url": "http://localhost:8081/mcp"}}}, indent=2) + "\n",
            encoding="utf-8")
        print("mcp config : .mcp.json")
    else:
        print("mcp config : .mcp.json already present, left alone")

    # 5. AGENTS.md pointer
    agents = root / "AGENTS.md"
    existing = agents.read_text(encoding="utf-8") if agents.exists() else ""
    heading = "## Project knowledge" if github_native else "## Project memory"
    if heading not in existing:
        agents.write_text(existing + (GITHUB_AGENTS_SECTION if github_native else AGENTS_SECTION),
                          encoding="utf-8")
        print("agents     : appended a knowledge section to AGENTS.md")

    if github_native:
        print("\nnext: create reviewed assertions in the evidence repository and install the GitHub App.")
    else:
        print("\nnext: write a decision into .memory/decisions/ and commit it.")
    return 0


# ------------------------------------------------------------------- status
def cmd_status(args) -> int:
    root = repo_root()
    b = load_binding(root)
    print(f"project    : {b['org_slug']}/{b['project_slug']}")
    print(f"repo       : {b.get('repo_url') or '(unbound)'}")
    print(f"api        : {b.get('api', API)}")

    status, ready = _req("GET", "/readyz", timeout=15)
    checks = ready.get("checks", {})
    iso = checks.get("isolation", {})
    print(f"platform   : {'ready' if ready.get('ready') else 'NOT READY'}"
          f"  db={checks.get('database', {}).get('ok')}"
          f"  isolation={iso.get('pass')}"
          f"  embeddings={checks.get('embeddings', {}).get('ok')}")

    if b.get("source_provider") == "github":
        print(f"knowledge  : GitHub-native ({b.get('evidence_repo_url') or 'evidence repo missing'})")
    else:
        files = sorted(p for p in (root / ".memory").rglob("*")
                       if p.is_file() and p.name != "binding.json")
        print(f"local      : {len(files)} file(s) under .memory/")

    q = urllib.parse.urlencode({**scope_params(b), "q": "project", "limit": 1})
    st, _ = _req("GET", f"/v1/search?{q}")
    print(f"retrieval  : {'ok' if st == 200 else f'HTTP {st}'}")
    return 0


# ------------------------------------------------------------------- search
def _print_hits(results: list[dict]) -> None:
    if not results:
        print("(nothing found)")
        return
    for h in results:
        trust = h.get("tier") or h.get("trust") or "?"
        print(f"\n  [{trust}] {h.get('title', '')}")
        digest = (h.get("digest") or "").strip()
        if digest:
            print(f"      {digest[:220]}")
        if h.get("source_uri"):
            print(f"      source: {h['source_uri']}")


def cmd_search(args) -> int:
    b = load_binding(repo_root())
    q = urllib.parse.urlencode({**scope_params(b), "q": " ".join(args.query),
                                "limit": args.limit})
    st, d = _req("GET", f"/v1/search?{q}")
    if st != 200:
        die(f"search failed ({st}): {d}")
    if d.get("degraded"):
        print("! embedder unavailable — lexical results only")
    _print_hits(d.get("results", []))
    return 0


def cmd_why(args) -> int:
    """Rationale questions. Phrased so stage-1 planning classifies it as such."""
    args.query = ["why", "did", "we"] + list(args.query)
    return cmd_search(args)


# ------------------------------------------------------------------- doctor
def cmd_explain(args) -> int:
    """Retrieval Debugger — 05-BUILD-PLAN Phase 3 acceptance.

    "The Retrieval Debugger output for any query explains every returned and
    dropped item." Dropped items matter as much as returned ones: a pack that
    silently omits three near-duplicates looks identical to one that never found
    them, and only one of those is working correctly.
    """
    b = load_binding(repo_root())

    if args.ref or args.pack:
        params = {**scope_params(b)}
        if args.ref:
            params["ref"] = args.ref
        if args.pack:
            params["pack_id"] = args.pack
        st, d = _req("GET", "/v1/explain?" + urllib.parse.urlencode(params))
        if st != 200:
            die(f"explain failed ({st}): {d.get('detail', d)}")
        print(json.dumps(d, indent=2)[:4000])
        return 0

    # No ref/pack: build a pack for the query and explain it end to end.
    st, pack = _req("POST", "/v1/context", {
        **scope_params(b), "task": " ".join(args.query),
        "token_budget": args.budget})
    if st != 200:
        die(f"context failed ({st}): {pack}")

    print(f"pack     : {pack['pack_id']}")
    print(f"plan     : intent={pack['plan']['intent']}"
          f"  matched={pack['plan'].get('matched_on')}"
          f"  entities={len(pack['plan'].get('identifiers', []))}")
    print(f"profile  : {pack['ranking_profile']}"
          f"   rerank={pack.get('rerank', {}).get('applied')}")
    print(f"budget   : {pack['budget']['used']}/{pack['budget']['effective']} tokens"
          f"  ({pack['budget']['reason']})")
    print(f"timings  : {pack['timings_ms']}")
    if pack.get("degraded"):
        print("WARNING  : embedder unavailable — lexical arm only")

    print("\nRETURNED")
    for sec in pack["sections"]:
        for it in pack["sections"][sec]:
            if sec == "contested":
                print(f"  [contested] {it.get('kind')}")
                for side in it.get("sides", []):
                    print(f"      vs {side['title'][:56]} ({side['trust']})")
                continue
            parts = it.get("score_parts") or {}
            top = sorted(parts.items(), key=lambda kv: -abs(kv[1]))[:3]
            print(f"  [{sec:11}] {it['score']:.4f}  {it['title'][:48]}")
            print(f"       trust={it['trust']:13} why={', '.join(f'{k}={v}' for k, v in top)}")
            if it.get("also_seen_in"):
                print(f"       collapsed {len(it['also_seen_in'])} near-duplicate(s)")

    print("\nDROPPED")
    if not pack["dropped"]:
        print("  (nothing dropped)")
    for d_ in pack["dropped"]:
        print(f"  {d_['score']:.4f}  {(d_.get('title') or '')[:48]}")
        print(f"       reason: {d_['reason']}")
    return 0


def cmd_inbox(args) -> int:
    """Show the review queue, or act on one item."""
    b = load_binding(repo_root())
    if args.promote or args.reject or args.resolve:
        ref = args.promote or args.reject or args.resolve
        action = ("promote" if args.promote else
                  "reject" if args.reject else "resolve")
        st, d = _req("POST", "/v1/inbox/review", {
            **scope_params(b), "ref": ref, "action": action,
            "to_tier": args.tier, "note": args.note})
        if st != 200:
            die(f"{action} failed ({st}): {d.get('detail', d)}")
        print(f"{action}: {d}")
        return 0

    st, d = _req("GET", "/v1/inbox?" + urllib.parse.urlencode(
        {**scope_params(b), "limit": args.limit}))
    if st != 200:
        die(f"inbox failed ({st}): {d}")

    print(f"backlog {d['backlog']}  oldest {d['oldest_days']}d  -> {d['health']}")
    if not d["items"]:
        print("nothing awaiting review")
        return 0
    for it in d["items"]:
        print(f"\n  [{it['kind']:9}] {it['age_days'] or 0:>3}d  {it['ref'][:8]}  "
              f"{(it['title'] or '')[:52]}")
        if it.get("digest"):
            print(f"      {it['digest'][:96]}")
        if it.get("why"):
            print(f"      flagged: {str(it['why'])[:88]}")
    print("\nmemory inbox --promote <ref> --tier observed|verified")
    print("  memory inbox --reject  <ref> --note 'why'")
    return 0


def cmd_curation(args) -> int:
    """Curation health and the state of the ADR-0015 kill switch.

    Deliberately leads with whether extraction is ON or OFF and why. An operator
    looking at a silent extractor needs to distinguish "disabled by policy" from
    "broken", and that is the first question, not a detail further down.
    """
    b = load_binding(repo_root())
    st, d = _req("GET", "/v1/curation?" + urllib.parse.urlencode(scope_params(b)))
    if st != 200:
        die(f"curation failed ({st}): {d}")

    on = d["extraction_allowed"]
    print(f"extraction: {'ENABLED' if on else 'DISABLED'}  ({d['extraction_reason']})")
    print(f"inbox depth {d['inbox_depth']}  oldest {d['oldest_days']}d"
          f"  sampled {d['sampled_on'] or 'never'}")

    a = d["acceptance"]
    print(f"acceptance:  {a['promoted']} accepted / {a['rejected']} rejected"
          f"  over {a['days']}d  -> {a['band']}")
    if a["pending"]:
        print(f"             {a['pending']} extracted proposal(s) still undecided")

    t = d["thresholds"]
    print(f"thresholds:  alert {t['alert']}  disable {t['disable']}"
          f" sustained {t['sustained_days']}d"
          f"  band {t['accept_band'][0]:.0%}-{t['accept_band'][1]:.0%}")

    for al in d["alerts"]:
        print(f"  ALERT  {al}")
    if not d["alerts"]:
        print("  no alerts")
    return 0


def cmd_doctor(args) -> int:
    """Diagnose, in the order things actually break."""
    root_ok = bool(git("rev-parse", "--show-toplevel"))
    print(f"{'ok ' if root_ok else 'FAIL'} git repository")
    if not root_ok:
        print("     -> run this inside a git repo")
        return 1
    root = repo_root()

    remote = remote_url()
    print(f"{'ok ' if remote else 'warn'} git remote: {remote or '(none)'}")

    st, ready = _req("GET", "/readyz", timeout=15)
    print(f"{'ok ' if st == 200 else 'FAIL'} api reachable at {API}")
    for name, c in (ready.get("checks") or {}).items():
        # /readyz check shapes are not uniform: most report `ok`, the isolation
        # self-test reports `pass`. Reading only `ok` made doctor print
        # "FAIL isolation" on a perfectly healthy stack — which is the worst
        # possible false positive, because it sends someone hunting a data-leak
        # that is not there, and trains them to ignore the one line that matters
        # when it is.
        passed = c.get("ok", c.get("pass"))
        mark = "ok " if passed else ("warn" if c.get("degraded") else "FAIL")
        detail = c.get("error", "")
        print(f"  {mark} {name}{': ' + str(detail)[:60] if detail else ''}")

    bfile = binding_path(root)
    label = bfile.relative_to(root).as_posix() if bfile else GITHUB_BINDING_FILE
    print(f"{'ok ' if bfile else 'FAIL'} binding file {label}")
    if bfile is None:
        print("     -> run: memory init --github")
        return 1
    b = json.loads(bfile.read_text(encoding="utf-8"))

    if remote:
        st, r = _req("GET", "/v1/projects/resolve?"
                     + urllib.parse.urlencode({"repo_url": remote}))
        if st == 200 and r.get("project_id") == b["project_id"]:
            print("ok  remote resolves to the bound project")
        elif st == 409:
            print(f"FAIL ambiguous binding: {r.get('detail', '')[:100]}")
        elif st == 404:
            print("warn remote is not registered server-side (run: memory init)")
        else:
            print(f"warn remote resolves to a DIFFERENT project ({r.get('project_id')})")

    memdir = root / ".memory"
    n = len([p for p in memdir.rglob("*") if p.is_file()]) if memdir.is_dir() else 0
    github_native = b.get("source_provider") == "github"
    if github_native:
        print(f"ok  GitHub-native evidence: {b.get('evidence_repo_url') or '(missing)'}")
    else:
        print(f"{'ok ' if n else 'warn'} .memory/ contains {n} file(s)")

    q = urllib.parse.urlencode({**scope_params(b), "q": "constraints", "limit": 1})
    st, d = _req("GET", f"/v1/search?{q}")
    if st != 200:
        print(f"FAIL retrieval returned HTTP {st}")
        return 1
    print(f"ok  retrieval responds ({d.get('count', 0)} hit(s) for a smoke query)")
    if d.get("count", 0) == 0 and n and not github_native:
        print("     -> files exist locally but nothing is indexed yet.")
        print("        the scheduler polls every 60s; or POST /v1/ingest to force it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles still default to cp1252, which cannot encode the em-dashes
    # and the `⚠` marker the pack uses for contested claims. Without this the CLI
    # either mangles them or dies with UnicodeEncodeError partway through a
    # listing — and a review tool that crashes while printing the queue is one
    # nobody uses. `errors="replace"` keeps output flowing on terminals that
    # genuinely cannot render a glyph.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    p = argparse.ArgumentParser(prog="memory", description="Project memory CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="register this repository (legacy scaffold by default)")
    i.add_argument("--org", help="organisation slug (default: 'default')")
    i.add_argument("--project", help="project slug (default: repo directory name)")
    i.add_argument("--github", action="store_true",
                   help="bind GitHub source/evidence repositories without .memory/")
    i.add_argument("--evidence-repo", help="paired private GitHub evidence repository")
    i.add_argument("--installation-id", type=int, help="GitHub App installation id")
    i.add_argument("--branch", help="source default branch (default: main)")
    i.set_defaults(fn=cmd_init)

    s = sub.add_parser("status", help="what this repo is bound to")
    s.set_defaults(fn=cmd_status)

    q = sub.add_parser("search", help="search this project's memory")
    q.add_argument("query", nargs="+")
    q.add_argument("--limit", type=int, default=5)
    q.set_defaults(fn=cmd_search)

    w = sub.add_parser("why", help="ask a rationale question")
    w.add_argument("query", nargs="+")
    w.add_argument("--limit", type=int, default=5)
    w.set_defaults(fn=cmd_why)

    d = sub.add_parser("doctor", help="diagnose a setup that is not working")
    d.set_defaults(fn=cmd_doctor)

    i2 = sub.add_parser("inbox", help="review quarantined memories and conflicts")
    i2.add_argument("--limit", type=int, default=20)
    i2.add_argument("--promote", metavar="REF")
    i2.add_argument("--reject", metavar="REF")
    i2.add_argument("--resolve", metavar="REF")
    i2.add_argument("--tier", default="observed", choices=["observed", "verified"])
    i2.add_argument("--note", default="")
    i2.set_defaults(fn=cmd_inbox)

    c = sub.add_parser("curation",
                       help="curation health and the LLM-extraction kill switch")
    c.set_defaults(fn=cmd_curation)

    e = sub.add_parser("explain", help="why did retrieval return (or drop) this?")
    e.add_argument("query", nargs="*", help="a task to build and explain a pack for")
    e.add_argument("--ref", help="explain one memory's provenance instead")
    e.add_argument("--pack", help="explain a past pack by id")
    e.add_argument("--budget", type=int, default=4000)
    e.set_defaults(fn=cmd_explain)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
