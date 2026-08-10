"""Review and add golden cases exported by the Retrieval Debugger.

The console export intentionally contains no asserted answer. A curator selects
the reviewed entries in ``case.expect`` and then uses this tool to validate or
append the case. Keeping that decision in a file review prevents a retrieved but
wrong answer from becoming self-reinforcing benchmark data.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLDEN = ROOT / "eval" / "golden_set.json"
HASH = re.compile(r"^[0-9a-f]{12}$")
CASE_ID = re.compile(r"^g\d+$")


class CaseError(ValueError):
    """A case is syntactically valid JSON but unsuitable for the benchmark."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CaseError(f"{path} must contain a JSON object")
    return data


def validate_case(case: dict[str, Any]) -> None:
    case_id = case.get("id")
    if case_id is not None and (not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id)):
        raise CaseError("case id must match g<digits>")
    if not isinstance(case.get("query"), str) or not case["query"].strip():
        raise CaseError("case query must be a non-empty string")

    expected = case.get("expect")
    if not isinstance(expected, list) or not expected:
        raise CaseError("case.expect must contain at least one reviewed memory")
    expected_keys: set[str] = set()
    for item in expected:
        if not isinstance(item, dict):
            raise CaseError("each expected item must be an object")
        key, digest, grade = item.get("key"), item.get("hash"), item.get("grade", 3)
        if not isinstance(key, str) or not key:
            raise CaseError("expected item is missing its stable key")
        if key in expected_keys:
            raise CaseError(f"expected key is repeated: {key}")
        expected_keys.add(key)
        if not isinstance(digest, str) or not HASH.fullmatch(digest):
            raise CaseError(f"expected hash for {key} must be 12 lowercase hex characters")
        if not isinstance(grade, int) or grade not in (1, 2, 3):
            raise CaseError(f"expected grade for {key} must be 1, 2 or 3")

    forbidden = case.get("forbid", [])
    if not isinstance(forbidden, list) or not all(isinstance(key, str) and key for key in forbidden):
        raise CaseError("case.forbid must be a list of stable keys")
    overlap = expected_keys & set(forbidden)
    if overlap:
        raise CaseError(f"a key cannot be expected and forbidden: {sorted(overlap)[0]}")


def validate_golden(golden: dict[str, Any]) -> None:
    cases = golden.get("cases")
    if not isinstance(cases, list) or not cases:
        raise CaseError("golden_set.json must contain a non-empty cases list")
    ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise CaseError("every golden case must be an object")
        validate_case(case)
        case_id = case["id"]
        if case_id in ids:
            raise CaseError(f"duplicate case id: {case_id}")
        ids.add(case_id)


def case_from_export(export: dict[str, Any], case_id: str) -> dict[str, Any]:
    if export.get("version") != 1:
        raise CaseError("export version must be 1")
    raw_case = export.get("case")
    if not isinstance(raw_case, dict):
        raise CaseError("export is missing its case object")
    case = {
        "id": case_id,
        "query": raw_case.get("query"),
        "expect": raw_case.get("expect"),
        "forbid": raw_case.get("forbid", []),
    }
    validate_case(case)
    return case


def add_case(golden: dict[str, Any], export: dict[str, Any], case_id: str) -> dict[str, Any]:
    validate_golden(golden)
    if any(case["id"] == case_id for case in golden["cases"]):
        raise CaseError(f"case id already exists: {case_id}")
    case = case_from_export(export, case_id)
    updated = {**golden, "cases": [*golden["cases"], case]}
    validate_golden(updated)
    return updated


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Validate and add reviewed golden cases")
    sub = p.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate", help="validate a golden-set JSON file")
    check.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    add = sub.add_parser("add", help="preview or append a reviewed console export")
    add.add_argument("export", type=Path)
    add.add_argument("--id", required=True, dest="case_id")
    add.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    add.add_argument("--write", action="store_true", help="write the appended golden set")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        golden = load_json(args.golden)
        if args.command == "validate":
            validate_golden(golden)
            print(f"valid: {len(golden['cases'])} cases, snapshot {golden.get('snapshot', '?')}")
            return 0

        updated = add_case(golden, load_json(args.export), args.case_id)
        case = updated["cases"][-1]
        if not args.write:
            print(json.dumps(case, indent=2))
            print("dry run only; pass --write after reviewing this case")
            return 0
        args.golden.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
        print(f"added {case['id']} to {args.golden} ({len(updated['cases'])} cases)")
        return 0
    except CaseError as exc:
        print(f"invalid golden case: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
