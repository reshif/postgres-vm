"""Unit coverage for the explicit no-evidence retrieval boundary.

The ranker must order every candidate set, including an unrelated one. This
test proves the evidence gate does not mistake vector rank, trust, or recency
for proof that project memory answers the caller's question.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/app/src")
from memory_platform import memories  # noqa: E402


results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def candidate(**values: object) -> dict:
    return {
        "id": values.pop("id", "memory"),
        "title": values.pop("title", ""),
        "digest": values.pop("digest", ""),
        "identifiers": values.pop("identifiers", ""),
        "r_vec": values.pop("r_vec", 1),
        "r_lex": values.pop("r_lex", None),
        "r_ident": values.pop("r_ident", None),
        "r_graph": values.pop("r_graph", None),
        "cross_score": values.pop("cross_score", None),
        **values,
    }


def main() -> None:
    absent, outcome = memories.select_evidence(
        "what is the Zorblax archive retention rule",
        [candidate(title="Run the retrieval evaluation",
                   digest="The golden set measures recall and latency.",
                   cross_score=0.0002)],
    )
    check("vector-only neighbour is not treated as evidence",
          absent == [] and outcome["status"] == "no_relevant_evidence", str(outcome))

    direct, outcome = memories.select_evidence(
        "how do I add a database migration",
        [candidate(id="procedure", title="Add a database migration",
                   digest="Create a migration revision and execute the SQL.", r_lex=1)],
    )
    check("direct query terms are retained as evidence",
          [item["id"] for item in direct] == ["procedure"]
          and "direct_terms" in direct[0]["evidence"]["signals"]
          and outcome["status"] == "supported", str(outcome))

    compound, outcome = memories.select_evidence(
        "why did we choose postgres for vectors and how do I deploy the api",
        [
            candidate(id="storage", title="Postgres vector storage decision"),
            candidate(id="deploy", title="API deployment procedure"),
        ],
    )
    check("compound questions retain evidence for each supported clause",
          {item["id"] for item in compound} == {"storage", "deploy"}
          and outcome["status"] == "supported", str(outcome))

    partial_compound, outcome = memories.select_evidence(
        "why did we choose postgres for vectors and how do I deploy the api",
        [candidate(id="storage", title="Postgres vector storage decision")],
    )
    check("compound questions report partial support instead of no evidence",
          [item["id"] for item in partial_compound] == ["storage"]
          and outcome["status"] == "partial_support"
          and outcome["missing_clauses"] == ["how do I deploy the api"], str(outcome))

    partial, outcome = memories.select_evidence(
        "which unrecorded Zorblax archive policy applies to pgvector",
        [candidate(id="glossary", title="Glossary of platform entities",
                   digest="The pgvector policy is documented for storage.", r_lex=1)],
    )
    check("partial generic overlap does not validate a longer claim",
          partial == [] and outcome["status"] == "no_relevant_evidence", str(outcome))

    semantic, outcome = memories.select_evidence(
        "unusual wording for the storage architecture",
        [candidate(id="semantic", title="Two-plane design",
                   digest="Repository ledger and operational index.", cross_score=0.2)],
    )
    check("raw cross-encoder score alone does not establish evidence",
          semantic == [] and outcome["status"] == "no_relevant_evidence", str(outcome))

    corroborated, outcome = memories.select_evidence(
        "how do I add a database migration",
        [candidate(id="corroborated", title="Add a database migration",
                   digest="Create the revision and execute SQL.", r_lex=1, cross_score=0.2)],
    )
    check("reranker corroborates direct project evidence",
          [item["id"] for item in corroborated] == ["corroborated"]
          and "reranker" in corroborated[0]["evidence"]["signals"]
          and outcome["status"] == "supported", str(outcome))

    linked, outcome = memories.select_evidence(
        "what is the Zorblax retention rule for pgvector",
        [candidate(id="linked", title="pgvector storage decision", r_graph=1)],
    )
    check("an entity link alone does not validate an unrelated claim",
          linked == [] and outcome["status"] == "no_relevant_evidence", str(outcome))

    graph, outcome = memories.select_evidence(
        "what depends on Redis",
        [candidate(id="graph", title="Cache invalidation decision", r_graph=1)],
    )
    check("query-resolved graph support is evidence",
          [item["id"] for item in graph] == ["graph"]
          and "graph" in graph[0]["evidence"]["signals"], str(outcome))

    failed = [name for ok, name in results if not ok]
    print(f"\n{'=' * 62}\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
