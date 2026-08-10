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

import json
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

# ---------------------------------------------------------------------------
# AUTHORED ENTITIES, loaded from the database rather than hard-coded here.
#
# The dictionary above is deliberately hand-maintained and high-precision, but it
# cannot grow: a term is invisible to the graph until an engineer edits this file
# and redeploys. Meanwhile `.memory/glossary.md` is already ingested at
# `authoritative` tier and ingest.py describes it as "entities and their
# canonical names" — it was feeding searchable TEXT and nothing else. `Context
# pack`, `Trust tier` and `Digest` were defined there and were not graph nodes.
#
# This is the bridge. Terms authored in the glossary become entities through the
# path the two-plane model already sanctions: written in git, reviewed in a diff,
# ingested with a commit sha. No LLM, no proposal queue — an authored definition
# is authoritative by construction (ADR-0002).
#
# Cached per (tenant, project) because extract() is called once per memory on the
# ingest path and recompiling a regex set per call would make a tree walk
# quadratic in glossary size.
_AUTHORED: dict[tuple[str, str], dict[str, tuple[str, list[re.Pattern[str]]]]] = {}


def load_authored(conn: Connection, *, tenant_id: UUID, project_id: UUID,
                  refresh: bool = False) -> int:
    """Compile authored entities from mem.entities into the matcher.

    Returns how many were loaded. Safe to call on every ingest pass; the compiled
    form is cached until `refresh=True`, which the glossary writer passes after it
    has upserted new terms.
    """
    key = (str(tenant_id), str(project_id))
    if not refresh and key in _AUTHORED:
        return len(_AUTHORED[key])

    rows = conn.execute(
        text("SELECT e.canonical_name, e.kind, "
             "       COALESCE(array_agg(a.alias) FILTER (WHERE a.alias IS NOT NULL), "
             "                '{}') AS aliases "
             "  FROM mem.entities e "
             "  LEFT JOIN mem.entity_aliases a ON a.entity_id = e.id "
             " WHERE e.tenant_id = :t AND e.project_id = :p "
             "   AND e.attributes ->> 'source' = 'glossary' "
             " GROUP BY e.canonical_name, e.kind"),
        {"t": str(tenant_id), "p": str(project_id)},
    ).mappings().all()

    compiled: dict[str, tuple[str, list[re.Pattern[str]]]] = {}
    for r in rows:
        # The canonical name is itself a matchable alias; an author who writes
        # "**Context pack** — ..." expects the phrase "context pack" to match.
        names = {r["canonical_name"], *(r["aliases"] or [])}
        compiled[r["canonical_name"]] = (
            r["kind"], [_alias_regex(a) for a in names if a])
    _AUTHORED[key] = compiled
    return len(compiled)


def _matchers(scope: tuple[str, str] | None):
    """The hard-coded dictionary plus any authored entities for this scope.

    The dictionary wins on a name collision: it carries curated aliases like
    `pg` for PostgreSQL that a prose glossary will not, and silently replacing
    them with a looser authored entry would lower precision without anyone
    noticing.
    """
    if scope is None or scope not in _AUTHORED:
        return _COMPILED
    return {**_AUTHORED[scope], **_COMPILED}


def extract(*texts: str, scope: tuple[str, str] | None = None) -> list[tuple[str, str, float]]:
    """Return (canonical_name, kind, weight) for entities present in the text.

    Weight is mention frequency normalised into 0..1 and is what the graph arm
    orders by — an entity mentioned once in passing should not pull as hard as
    the subject of the document.
    """
    blob = "\n".join(t for t in texts if t)
    if not blob.strip():
        return []

    counts: dict[tuple[str, str], int] = {}
    for canon, (kind, patterns) in _matchers(scope).items():
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


# `**Term** — definition`, which is the convention `.memory/glossary.md` already
# uses. Parsing what authors already write means the glossary becomes a graph
# source with no change to how anyone writes it — and a format nobody has to
# learn is a format that stays accurate.
_GLOSSARY_TERM = re.compile(
    r"^\*\*(?P<name>[^*\n]{2,60})\*\*\s*(?:—|--|-|:)\s*(?P<definition>.+)$",
    re.M,
)

# Domain vocabulary, not installed software. `concept` is the kind that fixes the
# eval's worst failures — questions asked in plain language ("what stops project
# B reading project A's decisions?") against answers written in technical
# language. The concept is the bridge between the two.
DEFAULT_GLOSSARY_KIND = "concept"

# Entity kinds this system understands.
#
# `mem.entities.kind` is a text column, not an enum, so this list is advisory
# rather than enforced — deliberately, because a domain adds nouns faster than a
# migration can be written, and an unknown kind should degrade to "unclassified"
# rather than reject the write.
#
# Everything present before this list was something an engineer INSTALLS
# (technology, module, system, service). Nothing was something the business
# MEANS. That gap is the measured cause of the eval's worst cases: a question
# phrased in domain language cannot reach an answer written in technical
# language, because no node connects them.
ENTITY_KINDS: dict[str, str] = {
    # Technical — what was already here.
    "technology":  "Installed or depended-upon software",
    "module":      "A component inside this codebase",
    "system":      "A named subsystem or protocol",
    "service":     "A deployable process",

    # Domain — the vocabulary bridge.
    "concept":     "A domain idea the team names: project isolation, bi-temporality",
    "incident":    "A named failure. The subject of caused_by / solved_by, and "
                   "what capability C2 (recurrence) is asked about",
    "person":      "A human. 'Who decided this' is the most common follow-up to "
                   "any decision, and today it is unanswerable",
    "team":        "A group that owns something",
    "environment": "staging, production. Knowledge true in one place and not another",
    "requirement": "A rule or policy a constraint traces back to",
}


# Headings authors actually write, mapped to kinds. English plurals do not
# reduce mechanically — "people" is not "persons" and stripping an "s" from
# "technologies" gives "technologie" — so the natural forms are listed rather
# than derived. An unrecognised heading is not an error; it leaves the kind at
# the default, because a glossary should not fail to ingest over a section title.
_HEADING_KIND: dict[str, str] = {
    "concept": "concept", "concepts": "concept",
    "incident": "incident", "incidents": "incident",
    "failure": "incident", "failures": "incident", "outages": "incident",
    "person": "person", "people": "person", "persons": "person",
    "team": "team", "teams": "team",
    "environment": "environment", "environments": "environment",
    "requirement": "requirement", "requirements": "requirement",
    "policy": "requirement", "policies": "requirement",
    "technology": "technology", "technologies": "technology",
    "module": "module", "modules": "module",
    "system": "system", "systems": "system",
    "service": "service", "services": "service",
}


def _kind_for_heading(heading: str) -> str:
    return _HEADING_KIND.get(heading.strip().lower(), DEFAULT_GLOSSARY_KIND)


def parse_glossary(body: str, meta: dict | None = None) -> list[dict[str, Any]]:
    """Entities defined in a glossary document.

    Two sources, in increasing order of precision:

      * the `**Term** — definition` prose convention, which needs no authoring
        change and covers the whole existing file;
      * an optional `entities:` frontmatter block, for terms that need a specific
        kind or aliases prose cannot express:

            entities:
              - name: RRF
                kind: technology
                aliases: [reciprocal rank fusion]

    Frontmatter wins where both describe the same term, because it is the more
    deliberate statement.
    """
    found: dict[str, dict[str, Any]] = {}

    # A markdown heading sets the kind for the terms beneath it:
    #
    #     ## Incidents
    #     **PgBouncer auth outage** — the pooler refused every connection ...
    #
    # Grouping by heading is how people already write glossaries, so a kind can
    # be declared without frontmatter and without learning a syntax. Singular or
    # plural, case-insensitive; an unrecognised heading simply leaves the kind at
    # the default rather than inventing one.
    section_kind = DEFAULT_GLOSSARY_KIND
    for line in (body or "").splitlines():
        heading = re.match(r"^#{1,6}\s+(?P<h>.+?)\s*$", line)
        if heading:
            section_kind = _kind_for_heading(heading.group("h"))
            continue

        m = _GLOSSARY_TERM.match(line)
        if not m:
            continue
        name = m.group("name").strip()
        if not name:
            continue
        found[name] = {
            "name": name,
            "kind": section_kind,
            "aliases": [],
            "definition": m.group("definition").strip()[:500],
        }

    for item in (meta or {}).get("entities") or []:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        existing = found.get(name, {})
        aliases = item.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        found[name] = {
            "name": name,
            "kind": str(item.get("kind") or existing.get("kind")
                        or DEFAULT_GLOSSARY_KIND),
            "aliases": [str(a).strip() for a in aliases if str(a).strip()],
            "definition": str(item.get("definition")
                              or existing.get("definition") or "")[:500],
        }

    return list(found.values())


def upsert_glossary_entities(
    conn: Connection, *, tenant_id: UUID, project_id: UUID,
    body: str, metadata: dict | None = None, source_version: str | None = None,
) -> dict[str, int]:
    """Turn a glossary document into graph entities.

    Written at `authoritative` tier, and that is the point: these came through
    Plane A — authored in the repository, reviewed in a diff, ingested with a
    commit sha (ADR-0002). They do not pass through the proposal queue, because
    a human already reviewed them in the only place this system counts as review.

    Idempotent. Re-ingesting an unchanged glossary changes nothing; renaming a
    term adds the new one and leaves the old, which is deliberate — silently
    deleting an entity would orphan every mention and edge that referenced it.
    """
    terms = parse_glossary(body, metadata)
    if not terms:
        return {"entities": 0, "aliases": 0}

    entities = aliases = 0
    for t in terms:
        eid = conn.execute(
            text("INSERT INTO mem.entities "
                 "  (tenant_id, project_id, kind, canonical_name, tier, attributes) "
                 "VALUES (:t, :p, :k, :n, 'authoritative', CAST(:a AS jsonb)) "
                 "ON CONFLICT (tenant_id, project_id, kind, canonical_name) "
                 "DO UPDATE SET attributes = mem.entities.attributes || EXCLUDED.attributes, "
                 "              tier = 'authoritative' "
                 "RETURNING id"),
            {"t": str(tenant_id), "p": str(project_id), "k": t["kind"],
             "n": t["name"],
             "a": json.dumps({"source": "glossary",
                              "definition": t["definition"],
                              "source_version": source_version})},
        ).scalar_one()
        entities += 1

        for alias in t["aliases"]:
            conn.execute(
                text("INSERT INTO mem.entity_aliases (entity_id, tenant_id, alias) "
                     "VALUES (:e, :t, :a) ON CONFLICT DO NOTHING"),
                {"e": str(eid), "t": str(tenant_id), "a": alias})
            aliases += 1

    # The matcher is cached; without this the terms just written stay invisible
    # until the next process restart, which is exactly the kind of "works after a
    # redeploy" behaviour that wastes an afternoon.
    load_authored(conn, tenant_id=tenant_id, project_id=project_id, refresh=True)
    log.info("glossary: %d entities, %d aliases", entities, aliases)
    return {"entities": entities, "aliases": aliases}


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
    found = extract(title, content,
                    scope=(str(tenant_id), str(project_id)))
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
    names = [n for n, _, _ in extract(
        query, scope=(str(tenant_id), str(project_id)))]
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
# ORDER IS SIGNIFICANT — most specific first, `uses` last.
#
# Every one of the 51 edge proposals in the database was `uses`, and it was not
# because prose only expresses that relation. The matcher takes the FIRST
# pattern that matches a clause, and `uses` was second in the list with a very
# broad alternation (`using|via|through`). So:
#
#   "The queue failure was fixed by Procrastinate connecting through PostgreSQL"
#
# matched `uses` on "through" and stopped, never reaching `solved_by`. Thirteen
# of fourteen relation types were unreachable in practice for any sentence that
# happened to contain a common preposition.
#
# `uses` is the catch-all and now sorts last. The relations that carry reasoning
# value — why something broke, what fixed it, what it argues against — get first
# refusal on the clause.
RELATION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Causal and evidential: capability C2 (recurrence) is built on these.
    ("caused_by", re.compile(
        r"\b(caused by|due to|because of|triggered by|as a result of|"
        r"stems from|comes from)\b", re.I)),
    ("solved_by", re.compile(
        r"\b(fixed by|solved by|resolved by|mitigated by|worked around by|"
        r"addressed by|corrected by)\b", re.I)),
    ("mitigates", re.compile(
        r"\b(mitigates|guards against|protects against|prevents|defends against)\b", re.I)),
    ("contradicts", re.compile(
        r"\b(contradicts|conflicts with|instead of|rather than|"
        r"incompatible with|in place of|as opposed to)\b", re.I)),
    ("supersedes", re.compile(
        r"\b(supersedes|replaces|replaced by|deprecates|obsoletes)\b", re.I)),
    ("supports", re.compile(
        r"\b(supports|corroborates|confirms|backs up|is evidence for)\b", re.I)),

    # Structural.
    ("depends_on", re.compile(
        r"\b(depends on|requires|needs|relies on|is backed by|fronted by)\b", re.I)),
    ("part_of", re.compile(r"\b(part of|belongs to|lives in|inside|contained in)\b", re.I)),
    ("implements", re.compile(r"\b(implements|provides|serves|exposes|satisfies)\b", re.I)),
    ("deployed_to", re.compile(r"\b(deployed to|runs in|hosted on|deployed on)\b", re.I)),
    ("produces", re.compile(r"\b(produces|emits|writes|generates|outputs)\b", re.I)),
    ("documented_in", re.compile(r"\b(documented in|described in|specified in|recorded in)\b", re.I)),
    ("owns", re.compile(r"\b(owns|owned by|maintained by|responsible for)\b", re.I)),

    # LAST. The broadest alternation, and the least informative relation — it
    # must never pre-empt a more specific one.
    ("uses", re.compile(r"\b(uses|using|via|through|built on|runs on)\b", re.I)),
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
            # Matched the phrase but could not resolve an entity either side —
            # usually because the subject is not in the dictionary ("the outage
            # was caused by PgBouncer"). Keep trying the remaining patterns
            # instead of giving up on the clause: breaking here was half of why
            # only one relation type ever appeared, since a failed specific
            # match consumed the clause and blocked the general one behind it.
            continue
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
    #
    # `own[0]` is a guess at what the document is ABOUT, and when the guess lands
    # on the same entity the author named as the target you get a self-loop. That
    # is how `RRF uses RRF` and `RLS uses RLS` reached the accepted relationships
    # table: ADRs that discuss one technology and declare a relation to it.
    #
    # A self-edge is never information. It also pollutes the graph arm, which
    # expands from a seed entity to its neighbours — a loop makes an entity its
    # own neighbour and inflates its apparent connectivity.
    own = [n for n, _, _ in extract(
        title, content, scope=(str(tenant_id), str(project_id)))]
    if own:
        for rel, target in declared_relations(metadata or {}):
            if own[0] != target:
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
        # Belt and braces. The declared path is guarded above, but this is the
        # single point every edge passes through, and the database constraint
        # added in migration 0017 would otherwise turn a future extraction bug
        # into a failed write on the ingest path rather than a skipped edge.
        if by_name[src] == by_name[dst]:
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
