"""Pinned eval snapshots exclude local CLI binding state."""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path


def load_snapshot_module():
    spec = importlib.util.spec_from_file_location("eval_snapshot", "/repo/eval/snapshot.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    snapshot = load_snapshot_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        decision = root / "decisions" / "ADR-0001.md"
        decision.parent.mkdir()
        decision.write_text("# Decision\n", encoding="utf-8")

        baseline = snapshot.snapshot_id(root)
        (root / "binding.json").write_text('{"project_id":"local"}', encoding="utf-8")
        files = snapshot.corpus_files(root)
        assert files == [decision], files
        assert snapshot.snapshot_id(root) == baseline

    print("1/1 passed")


if __name__ == "__main__":
    main()
