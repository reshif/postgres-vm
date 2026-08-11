"""Deterministic observed-noise corpus for retrieval scale experiments.

The reviewed project corpus is intentionally small.  A ranking evaluation that
never has more candidates than its internal retrieval window cannot reveal how
the system behaves when irrelevant, active project records compete for space.
These records are synthetic and stay in the dedicated evaluation tenant; they
are not product knowledge or Suite 1 acceptance evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memory_platform import memories


@dataclass(frozen=True)
class ScaleDocument:
    key: str
    title: str
    content: str


# Deliberately outside this repository's operational vocabulary.  The fixtures
# are active observed records so they contend in ordinary retrieval, but must
# never become a convenient way to make project-answer accuracy look better.
TOPICS: tuple[tuple[str, str], ...] = (
    ("astronomy", "observatory spectra telescope galaxies orbital photometry"),
    ("botany", "greenhouse seedlings irrigation pollination herbarium soils"),
    ("ceramics", "kiln glaze clay firing pottery studio"),
    ("coastal", "shoreline tides estuary seabirds saltmarsh navigation"),
    ("cuisine", "sourdough pastry fermentation ingredients kitchen"),
    ("geology", "bedrock minerals sediment volcanic strata fieldwork"),
    ("history", "archival manuscript dynasty chronicle museum restoration"),
    ("music", "orchestra harmony rehearsal notation recital acoustics"),
    ("rural", "orchard harvest irrigation village market weather"),
    ("sports", "athletics training sprint recovery stadium coaching"),
    ("textiles", "weaving loom fibres dye pattern garment workshop"),
    ("wildlife", "wetland habitat migration rangers biodiversity survey"),
    ("marine", "reef plankton currents vessel sampling oceanography"),
    ("theatre", "stage costume rehearsal audience lighting playwright"),
    ("transport", "tram timetable station passengers route conductor"),
)
RECORDS_PER_TOPIC = 10


def documents() -> list[ScaleDocument]:
    """Return a stable 150-record corpus with unique source identities."""
    rows: list[ScaleDocument] = []
    for topic, vocabulary in TOPICS:
        for sequence in range(1, RECORDS_PER_TOPIC + 1):
            key = f"scale:{topic}-{sequence:02d}"
            rows.append(ScaleDocument(
                key=key,
                title=f"{topic.title()} field note {sequence:02d}",
                content=(
                    f"Field note {sequence:02d} from the {topic} programme. "
                    f"Observed {vocabulary}. "
                    f"This record is a synthetic evaluation distractor with batch "
                    f"token SCALE_{topic.upper()}_{sequence:02d}."
                ),
            ))
    return rows


def seed_into(
    conn: Connection, tenant_id: UUID, project_id: UUID, principal_id: UUID,
) -> dict[str, int]:
    """Insert missing records only, avoiding repeat embedding work between runs."""
    rows = documents()
    keys = [row.key for row in rows]
    existing = set(conn.execute(text(
        "SELECT memory_key FROM mem.memories "
        "WHERE tenant_id = :tenant AND memory_key = ANY(:keys)"
    ), {"tenant": str(tenant_id), "keys": keys}).scalars())

    created = 0
    for row in rows:
        if row.key in existing:
            continue
        memories.write_memory(
            conn, tenant_id=tenant_id, project_id=project_id,
            principal_id=principal_id, mtype="observation", title=row.title,
            content=row.content, source_type="capture", memory_key=row.key,
            source_uri="eval://synthetic-scale/v1",
            metadata={"fixture": "synthetic-scale", "topic": row.key.split(":", 1)[1]},
        )
        created += 1
    return {"created": created, "unchanged": len(rows) - created, "total": len(rows)}
