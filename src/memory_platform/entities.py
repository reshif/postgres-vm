"""Entity extraction and mention linking — the graph arm's fuel.

00-MASTER-BLUEPRINT.md §5.2 defines a graph arm that walks `entity_mentions` out
to depth 2. Measured against the golden set it contributed **0%** of expected
hits, for the simple reason that `mem.entities` and `mem.entity_mentions` were
empty: nothing ever wrote to them. An arm with no data is not a weak arm, it is
an absent one, and it was quietly costing a fifth of the fusion budget.

DETERMINISTIC, NOT LLM. ADR-0015 caps extraction at rule-based until a human
curator exists, and entities feed retrieval directly — an LLM inventing an entity
called "the deployment policy" would create a graph hub that pulls unrelated
memories together forever. So extraction here is a dictionary plus conservative
patterns, and everything it produces is `observed` at best.

WHY A DICTIONARY AND NOT NER. A general NER model finds people and places; what
matters in a codebase is that `pgvector`, `PgBouncer` and `procrastinate` are the
same three things every time they appear, under every spelling. Canonicalisation
is the whole job — "Postgres", "PostgreSQL" and "postgresql" must resolve to one
node or the graph has three disconnected copies of the most important entity in
the project.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

log = logging.getLogger("memory.entities")

# canonical name -> (kind, aliases). Aliases are matched case-insensitively as
# whole words. Keep this small and high-precision: a false entity is worse than a
# missing one, because it links memories that have nothing to do with each other.
DICTIONARY: dict[str, tuple[str, tuple[str, ...]]] = {
    "PostgreSQL":    ("technology", ("postgres", "postgresql", "psql", "pg")),
    # Technologies this project argues ABOUT, not just ones it uses. A rejected
    # alternative is exactly what a "why did we not use X" query names, and
    # conflict detection pairs memories by shared entity — so an alternative
    # missing from the dictionary means two memories disagreeing about it are
    # never even compared.
    "Redis":         ("technology", ("redis",)),
    "Celery":        ("technology", ("celery",)),
    "Kafka":         ("technology", ("kafka",)),
    "RabbitMQ":      ("technology", ("rabbitmq", "rabbit mq")),
    "Qdrant":        ("technology", ("qdrant",)),
    "Weaviate":      ("technology", ("weaviate",)),
    "Elasticsearch": ("technology", ("elasticsearch", "elastic search")),
    "Memcached":     ("technology", ("memcached",)),
    "Neo4j":         ("technology", ("neo4j",)),
    "SQLite":        ("technology", ("sqlite",)),
    "pgvector":      ("technology", ("pgvector", "halfvec", "hnsw")),
    "PgBouncer":     ("technology", ("pgbouncer",)),
    "Procrastinate": ("technology", ("procrastinate",)),
    "Ollama":        ("technology", ("ollama",)),
    "TEI":           ("technology", ("text-embeddings-inference", "tei")),
    "bge-m3":        ("technology", ("bge-m3", "bge m3")),
    "Alembic":       ("technology", ("alembic",)),
    "FastAPI":       ("technology", ("fastapi",)),
    "Docker":        ("technology", ("docker", "docker compose", "compose")),
    "OpenTelemetry": ("technology", ("opentelemetry", "otel")),
    "Prometheus":    ("technology", ("prometheus",)),
    "Grafana":       ("technology", ("grafana",)),
    "Tempo":         ("technology", ("tempo",)),
    "MCP":           ("system", ("mcp", "model context protocol")),
    "RLS":           ("system", ("rls", "row level security", "row-level security")),
    "RRF":           ("system", ("rrf", "reciprocal rank fusion")),
    "Plane A":       ("system", ("plane a",)),
    "Plane B":       ("system", ("plane b",)),
    "context engine": ("module", ("context engine", "context pack")),
    "api":           ("service", ("api service", "the api")),
    "worker":        ("service", ("worker",)),
    "scheduler":     ("service", ("scheduler",)),
    "reranker":      ("service", ("reranker", "cross-encoder")),
}

# Code-shaped entities discovered from text rather than listed. Conservative on
# purpose: a bare CamelCase word is too common to treat as an entity, so only
# qualified forms (dotted paths, file paths, SQL objects) qualify.
CODE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("module", re.compile(r"\b(?:mem|memory_platform)\.[a-z_]+(?:\.[a-z_]+)*\b")),
    ("module", re.compile(r"\b[a-z_]+/[a-z_]+\.(?:py|sql|ya?ml|toml|sh)\b")),
    ("module", re.compile(r"\b[a-z_]{3,}\.(?:py|sql|ya?ml|toml|sh)\b")),
]

MAX_ENTITIES_PER_MEMORY = 25


def _alias_regex(alias: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", re.I)


_COMPILED = {
    canon: (kind, [_alias_regex(a) for a in aliases])
    for canon, (kind, aliases) in DICTIONARY.items()
}


def extract(*texts: str) -> list[tuple[str, str, float]]:
    """Return (canonical_name, kind, weight) for entities present in the text.

    Weight is mention frequency normalised into 0..1 and is what the graph arm
    orders by — an entity mentioned once in passing should not pull as hard as
    the subject of the document.
    """
    blob = "\n".join(t for t in texts if t)
    if not blob.strip():
        return []

    counts: dict[tuple[str, str], int] = {}
    for canon, (kind, patterns) in _COMPILED.items():
        n = sum(len(p.findall(blob)) for p in patterns)
        if n:
            counts[(canon, kind)] = n

    for kind, pat in CODE_PATTERNS:
        for m in pat.findall(blob):
            key = (m.lower(), kind)
            counts[key] = counts.get(key, 0) + 1

    if not counts:
        return []
    top = max(counts.values())
    out = [(name, kind, round(min(1.0, n / top), 4))
           for (name, kind), n in counts.items()]
    out.sort(key=lambda r: (-r[2], r[0]))
    return out[:MAX_ENTITIES_PER_MEMORY]


def link_memory(
    conn: Connection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    memory_id: Any,
    title: str,
    content: str,
) -> list[str]:
    """Upsert entities found in a memory and link them via entity_mentions.

    Idempotent: re-running on the same memory converges rather than duplicating,
    because entities are unique on (tenant, project, kind, canonical_name) and
    mentions on (memory, entity).
    """
    found = extract(title, content)
    if not found:
        return []

    linked: list[str] = []
    for name, kind, weight in found:
        entity_id = conn.execute(
            text("INSERT INTO mem.entities "
                 "  (tenant_id, project_id, kind, canonical_name, tier) "
                 "VALUES (:t, :p, :k, :n, 'observed') "
                 "ON CONFLICT (tenant_id, project_id, kind, canonical_name) "
                 "  DO UPDATE SET canonical_name = EXCLUDED.canonical_name "
                 "RETURNING id"),
            {"t": str(tenant_id), "p": str(project_id), "k": kind, "n": name},
        ).scalar_one()

        conn.execute(
            text("INSERT INTO mem.entity_mentions "
                 "  (tenant_id, memory_id, entity_id, weight) "
                 "VALUES (:t, :m, :e, :w) "
                 "ON CONFLICT (memory_id, entity_id) "
                 "  DO UPDATE SET weight = EXCLUDED.weight"),
            {"t": str(tenant_id), "m": str(memory_id), "e": str(entity_id), "w": weight},
        )
        linked.append(name)

    # Aliases make the dictionary's canonicalisation visible to anything that
    # queries the graph directly, rather than living only in this module.
    for name, kind, _ in found:
        for alias in DICTIONARY.get(name, ("", ()))[1]:
            conn.execute(
                text("INSERT INTO mem.entity_aliases (tenant_id, entity_id, alias) "
                     "SELECT :t, e.id, :a FROM mem.entities e "
                     " WHERE e.tenant_id = :t AND e.project_id = :p "
                     "   AND e.kind = :k AND e.canonical_name = :n "
                     "ON CONFLICT DO NOTHING"),
                {"t": str(tenant_id), "p": str(project_id), "k": kind,
                 "n": name, "a": alias},
            )
    return linked


def resolve_query_entities(
    conn: Connection, query: str, *, tenant_id: UUID, project_id: UUID,
) -> list[str]:
    """Entity ids named by a query, for the graph arm's starting set.

    Matches the same dictionary the writer used. Resolving a query with different
    rules than the corpus was indexed with is how a graph arm ends up looking
    broken when it is merely misaligned.
    """
    names = [n for n, _, _ in extract(query)]
    if not names:
        return []
    rows = conn.execute(
        text("SELECT id FROM mem.entities "
             " WHERE tenant_id = :t AND (project_id = :p OR project_id IS NULL) "
             "   AND canonical_name = ANY(:names)"),
        {"t": str(tenant_id), "p": str(project_id), "names": names},
    ).scalars().all()
    return [str(r) for r in rows]


def backfill(conn: Connection, *, tenant_id: UUID, project_id: UUID) -> dict[str, int]:
    """Link entities for memories that have none yet.

    Needed because entity extraction arrived after the corpus did; without it the
    graph arm stays empty for everything written before this module existed.
    """
    rows = conn.execute(
        text("SELECT m.id, m.title, m.content FROM mem.memories m "
             " WHERE m.tenant_id = :t AND m.project_id = :p "
             "   AND upper(m.valid_at) IS NULL "
             "   AND NOT EXISTS (SELECT 1 FROM mem.entity_mentions em "
             "                    WHERE em.memory_id = m.id)"),
        {"t": str(tenant_id), "p": str(project_id)},
    ).mappings().all()

    linked = 0
    for r in rows:
        names = link_memory(conn, tenant_id=tenant_id, project_id=project_id,
                            memory_id=r["id"], title=r["title"], content=r["content"])
        linked += len(names)

    # Relationships need their own backfill. link_relations runs only on the
    # CREATE path of write_memory, so a corpus ingested before relationship
    # extraction existed deduplicates on every later run and never gets edges —
    # which is exactly how the graph arm ended up being credited with a gain it
    # could not have produced. Backfilled over ALL memories, not just ones
    # missing mentions, because a memory can have entities and still no edges.
    all_rows = conn.execute(
        text("SELECT id, title, content, source_type FROM mem.memories "
             " WHERE tenant_id = :t AND project_id = :p AND upper(valid_at) IS NULL"),
        {"t": str(tenant_id), "p": str(project_id)},
    ).mappings().all()
    edges = proposed = 0
    for r in all_rows:
        got = link_relations(conn, tenant_id=tenant_id, project_id=project_id,
                             memory_id=r["id"], title=r["title"],
                             content=r["content"], source_type=r["source_type"])
        edges += got["edges"]
        proposed += got["proposed"]
    return {"memories": len(rows), "mentions": linked,
            "edges": edges, "proposed": proposed}


# ---------------------------------------------------------------- relationships
#
# The graph arm walks `relationships` out to depth 2. With that table empty the
# recursive CTE terminates at depth 0, so the arm degenerates to "memories that
# mention the entity the query named" — useful, but not a graph.
#
# ADR-0002/§444: "Edges are only created from tier >= 2 sources. Inferred edges
# live in a proposed_relationships table and appear in the review inbox." So
# extraction from a `git` or `ci` memory writes a real edge; anything less
# trusted proposes one instead. That split is the whole reason two tables exist.

# Surface patterns -> relation type. Deliberately narrow: an edge asserted
# between the wrong two entities is worse than a missing edge, because the graph
# arm then pulls unrelated memories together on every query that touches either.
RELATION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("depends_on", re.compile(
        r"\b(depends on|requires|needs|relies on|is backed by|fronted by)\b", re.I)),
    ("uses", re.compile(r"\b(uses|using|via|through|built on|runs on)\b", re.I)),
    ("part_of", re.compile(r"\b(part of|belongs to|lives in|inside)\b", re.I)),
    ("implements", re.compile(r"\b(implements|provides|serves|exposes)\b", re.I)),
    ("caused_by", re.compile(r"\b(caused by|due to|because of|triggered by)\b", re.I)),
    ("solved_by", re.compile(r"\b(fixed by|solved by|resolved by|mitigated by)\b", re.I)),
    ("contradicts", re.compile(r"\b(contradicts|conflicts with|instead of|rather than)\b", re.I)),
    ("deployed_to", re.compile(r"\b(deployed to|runs in|hosted on)\b", re.I)),
]

# Sources trusted enough to assert an edge rather than propose one (§444).
EDGE_SOURCES = {"git", "human", "ci", "deploy", "test"}


def extract_relations(text_: str) -> list[tuple[str, str, str]]:
    """Return (source_entity, relation, target_entity) found in one sentence.

    Sentence-scoped on purpose: two entities in the same *document* are not
    related, they are merely co-mentioned. Requiring them either side of a
    relation phrase in the same sentence is what keeps this from asserting an
    edge between every pair of technologies a long ADR happens to name.
    """
    out: list[tuple[str, str, str]] = []
    # Split on clause boundaries, not just sentence ends. A semicolon separates
    # two independent statements: reading
    # "PgBouncer is fronted by nothing; Procrastinate uses PostgreSQL"
    # as one clause produced the edge PgBouncer -depends_on-> PostgreSQL, which
    # the text does not say anywhere.
    for clause in re.split(r"(?<=[.!?;:])\s+|\n", text_ or ""):
        names = [n for n, _, _ in extract(clause)]
        if len(names) < 2:
            continue
        for rel, pat in RELATION_PATTERNS:
            m = pat.search(clause)
            if not m:
                continue
            # NEAREST entity either side of the relation phrase, not merely the
            # first one found. "Grafana and Prometheus are separate, the worker
            # uses Procrastinate" must not yield Grafana -uses-> Procrastinate.
            left = _nearest(clause[:m.start()], names, from_end=True)
            right = _nearest(clause[m.end():], names, from_end=False)
            if left and right and left != right:
                out.append((left, rel, right))
            break
    return out


def _nearest(fragment: str, names: list[str], *, from_end: bool) -> str | None:
    """The entity closest to the boundary of `fragment`.

    from_end=True looks for the LAST mention (the entity just before the relation
    phrase); from_end=False looks for the FIRST (just after it).
    """
    hits: list[tuple[int, str]] = []
    for n in names:
        for pat in _COMPILED.get(n, ("", []))[1]:
            found = list(pat.finditer(fragment))
            if found:
                hits.append((found[-1].end() if from_end else found[0].start(), n))
                break
    if not hits:
        return None
    return max(hits)[1] if from_end else min(hits)[1]


MEASURED_PROSE_YIELD = """
Measured on this project's own 33-document corpus: prose relation extraction
found ZERO edges, and only 4 of 56 clauses in a representative ADR contain two
entities at all. Where they do, the connector is punctuation or a preposition
("PostgreSQL + pgvector", "PostgreSQL with pgvector") rather than a relation
verb. Matching those would mean treating "with" as evidence of a relationship,
which manufactures edges between every pair of technologies a sentence names.

So prose extraction stays narrow and mostly silent, and DECLARED relations carry
the weight. That is consistent with ADR-0002: Plane A is authored and reviewed,
so an author stating `relates: [uses pgvector]` in frontmatter is both more
precise than inference and already covered by pull-request review. Inferred edges
from an LLM remain a Phase 5 concern and land in proposed_relationships.
"""


def declared_relations(meta: dict) -> list[tuple[str, str]]:
    """Relations an author declared in `.memory/` frontmatter.

        relates:
          - uses pgvector
          - depends_on PgBouncer

    Returns (relation, target_entity). The subject is the document's own
    entities, resolved by the caller.
    """
    raw = meta.get("relates") or []
    if isinstance(raw, str):
        raw = [raw]
    out: list[tuple[str, str]] = []
    valid = {r for r, _ in RELATION_PATTERNS} | {
        "supersedes", "implements", "part_of", "owns", "produces",
        "documented_in", "supports", "mitigates", "deployed_to"}
    for item in raw:
        parts = str(item).strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() in valid:
            out.append((parts[0].lower(), parts[1].strip()))
    return out


def link_relations(
    conn: Connection, *, tenant_id: UUID, project_id: UUID,
    memory_id: Any, title: str, content: str, source_type: str,
    metadata: dict | None = None,
) -> dict[str, int]:
    """Create edges (trusted sources) or proposals (everything else).

    Declared relations first, inferred ones second. See MEASURED_PROSE_YIELD for
    why inference alone is not enough on real technical prose.
    """
    rels = extract_relations(f"{title}. {content}")

    # Declared: the document's most-weighted entity is the subject.
    own = [n for n, _, _ in extract(title, content)]
    if own:
        for rel, target in declared_relations(metadata or {}):
            rels.append((own[0], rel, target))
    if not rels:
        return {"edges": 0, "proposed": 0}

    trusted = (source_type or "").lower() in EDGE_SOURCES
    table = "relationships" if trusted else "proposed_relationships"
    edges = 0
    for src, rel, dst in rels:
        ids = conn.execute(
            text("SELECT canonical_name, id FROM mem.entities "
                 " WHERE tenant_id = :t AND project_id = :p "
                 "   AND canonical_name = ANY(:names)"),
            {"t": str(tenant_id), "p": str(project_id), "names": [src, dst]},
        ).mappings().all()
        by_name = {r["canonical_name"]: r["id"] for r in ids}
        if src not in by_name or dst not in by_name:
            continue
        conn.execute(
            text(f"INSERT INTO mem.{table} "
                 "  (tenant_id, project_id, source_id, target_id, relation, tier, "
                 "   confidence, evidence_memory_id) "
                 "VALUES (:t, :p, :s, :d, CAST(:r AS mem.relation_type), "
                 "        CAST(:tier AS mem.trust_tier), :c, :m) "
                 "ON CONFLICT DO NOTHING"),
            {"t": str(tenant_id), "p": str(project_id),
             "s": str(by_name[src]), "d": str(by_name[dst]), "r": rel,
             "tier": "observed" if trusted else "inferred",
             "c": 0.7 if trusted else 0.4, "m": str(memory_id)},
        )
        edges += 1
    return ({"edges": edges, "proposed": 0} if trusted
            else {"edges": 0, "proposed": edges})
