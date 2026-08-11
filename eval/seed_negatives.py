"""Seed Suite 1's negative fixtures and attach them to the golden set.

Why this exists is argued at length in negative_fixtures.json. The short version:
`forbidden@10` has a gate of 0 and is an absolute count, so it is a containment
tripwire — "did anything that must never be served reach a pack" — and every
golden case carried an empty forbidden set, which made a headline gate report 0
for the only reason that proves nothing.

Two subcommands, because the two halves have different requirements:

  seed   needs a database and runs wherever the application does (in the
         container). It writes the fixtures, VERIFIES each one actually landed
         contained, and prints the key -> content-hash labels.
  sync   needs no database. It reads those labels and writes forbidden_memory_ids
         into golden_set.json, which lives on a read-only mount inside the
         container and so has to be written from the host.

The verification in `seed` is the part worth keeping. The fixture file DECLARES
that a payload is untrusted; the database DECIDES. If the injection classifier
stops recognising one of these, the fixture silently becomes an active,
retrievable memory — a negative label that is no longer contained, quietly
turning the gate red with no indication of why. Seeding therefore asserts the
outcome rather than assuming it, and refuses to emit a label it could not
confirm.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

# Evaluation scripts are normally piped into the API container (`python -`),
# where `__file__` resolves to `/stdin` rather than this checkout. Prefer the
# mounted repository in that runtime, while retaining direct host execution for
# the `sync` command that intentionally writes a reviewed golden set.
ROOT = Path("/repo") if (Path("/repo") / "eval").is_dir() else Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "eval" / "negative_fixtures.json"
DEFAULT_GOLDEN = ROOT / "eval" / "golden_set.json"

# Contained means "retrieval will not serve it". Retired content is withdrawn
# knowledge (status superseded); untrusted content is an agent-directed payload
# held behind review (status quarantined). Both are excluded by the status filter
# in the retrieval SQL, which is exactly the filter these fixtures exist to guard.
CONTAINED = {"retired": {"superseded"}, "untrusted": {"quarantined"}}
SOURCE_TYPE = {"retired": "git", "untrusted": "agent"}
# Retired fixtures impersonate withdrawn decisions, which is what makes them
# compete with the ADRs; untrusted ones are unreviewed agent output.
MEMORY_TYPE = {"retired": "decision", "untrusted": "observation"}


def load_fixtures(path: Path = FIXTURES) -> list[dict[str, Any]]:
    data = json.loads(path.read_text("utf-8"))
    fixtures = data.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise SystemExit(f"{path} has no fixtures")
    for fixture in fixtures:
        if fixture.get("kind") not in CONTAINED:
            raise SystemExit(f"unknown fixture kind: {fixture.get('kind')}")
        for field in ("key", "title", "content", "forbidden_for", "why"):
            if not fixture.get(field):
                raise SystemExit(f"fixture {fixture.get('key')} is missing {field}")
    return fixtures


def seed_into(conn: Any, tenant: UUID, project: UUID, principal: UUID, *,
              quiet: bool = False) -> dict[str, str]:
    """Write the fixtures on an already-scoped connection and confirm containment.

    Takes a connection rather than opening one so the eval can seed negatives in
    the same transaction it builds the rest of the corpus in — a negative fixture
    that only exists when someone remembers to run a separate script is not part
    of the benchmark.
    """
    from sqlalchemy import text

    from memory_platform import memories

    labels: dict[str, str] = {}
    failures: list[str] = []

    for fixture in load_fixtures():
        kind = fixture["kind"]
        result = memories.write_memory(
            conn,
            tenant_id=tenant, project_id=project, principal_id=principal,
            mtype=MEMORY_TYPE[kind], title=fixture["title"], content=fixture["content"],
            source_type=SOURCE_TYPE[kind],
            memory_key=fixture["key"],
            metadata={"eval_fixture": "suite1_negative", "kind": kind},
            # Retired fixtures are authoritative Plane A content that the project
            # has withdrawn — the same path a superseded ADR takes. Untrusted
            # fixtures must NOT be handed a lifecycle: the whole point is that the
            # trust and injection rules contain them without being told to.
            lifecycle="superseded" if kind == "retired" else None,
        )
        status, digest = conn.execute(
            text("SELECT status::text, left(content_hash, 12) "
                 "  FROM mem.memories WHERE id = :i"),
            {"i": str(result["id"])}).one()

        if status not in CONTAINED[kind]:
            failures.append(
                f"{fixture['key']}: expected {kind} fixture to be "
                f"{'/'.join(sorted(CONTAINED[kind]))}, database says {status}")
            continue
        labels[fixture["key"]] = digest
        if not quiet:
            print(f"  {status:12} {fixture['key']}  {digest}", file=sys.stderr)

    if failures:
        print("\nnegative fixtures were not contained:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print("\nThese are the labels forbidden@k is measured against. One that is "
              "not contained is retrievable, so the gate would go red without "
              "anything having regressed in retrieval. Fix containment first.",
              file=sys.stderr)
        raise SystemExit(2)
    return labels


def seed(tenant: UUID, project: UUID, principal: UUID) -> dict[str, str]:
    sys.path.insert(0, "/app/src")
    from memory_platform import db

    with db.scoped(tenant, principal, project) as conn:
        return seed_into(conn, tenant, project, principal)


def sync(labels: dict[str, str], golden_path: Path) -> tuple[int, int]:
    golden = json.loads(golden_path.read_text("utf-8"))
    fixtures = load_fixtures()
    by_case: dict[str, list[dict[str, str]]] = {}
    for fixture in fixtures:
        key = fixture["key"]
        if key not in labels:
            raise SystemExit(f"no seeded hash for {key}; run `seed` first")
        for case_id in fixture["forbidden_for"]:
            by_case.setdefault(case_id, []).append({"key": key, "hash": labels[key]})

    known = {case["id"] for case in golden["cases"]}
    unknown = sorted(set(by_case) - known)
    if unknown:
        raise SystemExit(f"fixtures reference unknown cases: {', '.join(unknown)}")

    touched = 0
    for case in golden["cases"]:
        negatives = by_case.get(case["id"])
        if not negatives:
            continue
        # Rewritten rather than appended so re-seeding after a content edit
        # replaces the stale hash instead of accumulating both.
        case["forbidden_memory_ids"] = negatives
        touched += 1

    golden_path.write_text(json.dumps(golden, indent=1, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    return touched, sum(len(v) for v in by_case.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    seeder = sub.add_parser("seed", help="write fixtures and emit labels (needs a database)")
    seeder.add_argument("--tenant", required=True)
    seeder.add_argument("--project", required=True)
    seeder.add_argument("--principal", required=True)
    seeder.add_argument("--out", type=Path, help="write labels here as well as stdout")

    syncer = sub.add_parser("sync", help="attach seeded labels to the golden set")
    syncer.add_argument("--labels", type=Path, required=True)
    syncer.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)

    args = parser.parse_args(argv)

    if args.command == "seed":
        labels = seed(UUID(args.tenant), UUID(args.project), UUID(args.principal))
        payload = json.dumps(labels, indent=1)
        if args.out:
            args.out.write_text(payload, encoding="utf-8")
        print("###LABELS###")
        print(payload)
        return 0

    labels = json.loads(args.labels.read_text("utf-8"))
    touched, total = sync(labels, args.golden)
    print(f"attached {total} negative labels to {touched} cases in {args.golden}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
