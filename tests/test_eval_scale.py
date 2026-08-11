"""The scale corpus is deterministic and cannot silently shrink."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_module():
    spec = importlib.util.spec_from_file_location("seed_scale", "/repo/eval/seed_scale.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    scale = load_module()
    documents = scale.documents()
    assert len(documents) == 150
    assert len({document.key for document in documents}) == len(documents)
    assert all(document.key.startswith("scale:") for document in documents)
    assert all("synthetic evaluation distractor" in document.content for document in documents)
    assert {document.key.split(":", 1)[1].split("-", 1)[0] for document in documents} == {
        topic for topic, _ in scale.TOPICS}
    run_eval = importlib.util.spec_from_file_location("run_eval", "/repo/eval/run_eval.py")
    assert run_eval and run_eval.loader
    module = importlib.util.module_from_spec(run_eval)
    sys.modules[run_eval.name] = module
    run_eval.loader.exec_module(module)
    assert (module.SCALE_TENANT, module.SCALE_PROJECT, module.SCALE_PRINCIPAL) != (
        module.TENANT, module.PROJECT, module.PRINCIPAL)
    evaluator_source = Path("/repo/eval/run_eval.py").read_text("utf-8")
    assert "status=run_status" in evaluator_source
    print("7/7 passed")


if __name__ == "__main__":
    main()
