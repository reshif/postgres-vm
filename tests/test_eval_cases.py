"""Golden-case additions require reviewed, stable labels."""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path


def load_cases_module():
    spec = importlib.util.spec_from_file_location("eval_cases", "/repo/eval/cases.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    cases = load_cases_module()
    golden = {
        "version": 1,
        "snapshot": "snapshot-a",
        "cases": [{
            "id": "g01", "query": "why", "forbid": [],
            "expect": [{"key": ".memory:decisions/ADR-0001.md",
                        "hash": "0123456789ab", "grade": 3}],
        }],
    }
    export = {
        "version": 1,
        "case": {
            "query": "what is the queue?", "forbid": [],
            "expect": [{"key": ".memory:decisions/ADR-0010.md",
                        "hash": "abcdef012345", "grade": 3}],
            "forbidden_memory_ids": [{
                "key": ".memory:decisions/ADR-0001.md", "hash": "0123456789ab",
            }],
        },
    }

    updated = cases.add_case(golden, export, "g02")
    assert len(updated["cases"]) == 2
    assert updated["cases"][-1]["id"] == "g02"
    assert cases.forbidden_labels(updated["cases"][-1]) == [{
        "key": ".memory:decisions/ADR-0001.md", "hash": "0123456789ab",
    }]

    try:
        cases.add_case(golden, export, "g01")
    except cases.CaseError:
        pass
    else:
        raise AssertionError("duplicate ids must be rejected")

    invalid = {**export, "case": {**export["case"], "expect": []}}
    try:
        cases.case_from_export(invalid, "g03")
    except cases.CaseError:
        pass
    else:
        raise AssertionError("unreviewed exports must be rejected")

    invalid_negative = {**export, "case": {**export["case"], "forbidden_memory_ids": [{
        "key": ".memory:decisions/ADR-0010.md", "hash": "abcdef012345",
    }]}}
    try:
        cases.case_from_export(invalid_negative, "g03")
    except cases.CaseError:
        pass
    else:
        raise AssertionError("an expected memory cannot also be forbidden")

    print("5/5 passed")


if __name__ == "__main__":
    main()
