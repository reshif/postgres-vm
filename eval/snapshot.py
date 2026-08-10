"""Re-freeze the pinned eval corpus from the live .memory/ tree.

    python eval/snapshot.py

Run this deliberately. It invalidates comparison with every result recorded
against the previous snapshot, which is the entire reason the corpus is pinned.
"""
import hashlib
import json
import pathlib
import re
import shutil

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC, DST = ROOT / ".memory", ROOT / "eval" / "corpus" / ".memory"
LOCAL_FILES = {pathlib.Path("binding.json")}


def corpus_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Return curated files only; local CLI state is not benchmark content."""
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.relative_to(root) not in LOCAL_FILES
    )


def snapshot_id(root: pathlib.Path) -> str:
    files = corpus_files(root)
    payload = "|".join(
        f"{path.relative_to(root).as_posix()}:"
        f"{hashlib.sha256(path.read_bytes()).hexdigest()[:16]}"
        for path in files
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def update_metadata(snapshot: str, file_count: int) -> None:
    """Record the new corpus identity without changing case labels or hashes."""
    readme = ROOT / "eval" / "corpus" / "SNAPSHOT.md"
    text = readme.read_text("utf-8")
    text = re.sub(r"(?m)^\*\*Snapshot id:\*\* `[^`]+`$",
                  f"**Snapshot id:** `{snapshot}`", text)
    text = re.sub(r"(?m)^\*\*Files:\*\* \d+$",
                  f"**Files:** {file_count}", text)
    readme.write_text(text, encoding="utf-8")

    golden = ROOT / "eval" / "golden_set.json"
    cases = json.loads(golden.read_text("utf-8"))
    cases["snapshot"] = snapshot
    golden.write_text(json.dumps(cases, indent=2) + "\n", encoding="utf-8")


def freeze() -> tuple[str, int]:
    """Copy the curated Plane A files into the pinned benchmark corpus."""
    if DST.exists():
        shutil.rmtree(DST)
    DST.mkdir(parents=True)
    for source in corpus_files(SRC):
        target = DST / source.relative_to(SRC)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    fingerprint = snapshot_id(DST)
    file_count = len(corpus_files(DST))
    update_metadata(fingerprint, file_count)
    return fingerprint, file_count

if __name__ == "__main__":
    fp, count = freeze()
    print(f"snapshot {fp}  ({count} files)")
    print("review case drift, then commit the corpus, metadata and labels together")
