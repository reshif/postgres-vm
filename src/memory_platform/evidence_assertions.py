"""Structured assertions from the reviewed Git sidecar evidence repository."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .github_client import GitBlob
from .github_evidence import normalize_repository_url
from .ingest import parse_frontmatter


class AssertionFormatError(ValueError):
    """A sidecar assertion file is not a safe, reviewable assertion."""


_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{2,119}$")
_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_STATES = frozenset({"accepted", "contested", "retracted"})


@dataclass(frozen=True)
class EvidenceReference:
    repository_url: str
    revision: str
    path: str

    @property
    def external_id(self) -> str:
        return f"git_blob:{normalize_repository_url(self.repository_url)}@{self.revision}:{self.path}"


@dataclass(frozen=True)
class AssertionDocument:
    assertion_key: str
    subject: str
    predicate: str
    object_value: str
    state: str
    confidence: float
    evidence: tuple[EvidenceReference, ...]


@dataclass(frozen=True)
class PreparedAssertion:
    """A fully fetched sidecar assertion, ready for an atomic DB write."""
    document: AssertionDocument
    source_blob: GitBlob
    supporting_blobs: tuple[GitBlob, ...]


def _bounded(meta: dict[str, Any], key: str, *, limit: int = 500) -> str:
    value = meta.get(key)
    if not isinstance(value, str) or not (cleaned := " ".join(value.split())):
        raise AssertionFormatError(f"assertion frontmatter requires {key}")
    if len(cleaned) > limit:
        raise AssertionFormatError(f"assertion field {key} exceeds {limit} characters")
    return cleaned


def _reference(value: str) -> EvidenceReference:
    """Parse github://github.com/org/repo@sha:path without fetching anything."""
    if not value.startswith("github://"):
        raise AssertionFormatError("evidence references must use github:// URLs")
    raw = value.removeprefix("github://")
    if "@" not in raw or ":" not in raw:
        raise AssertionFormatError("evidence reference must include @revision:path")
    repository, revision_path = raw.split("@", 1)
    revision, path = revision_path.split(":", 1)
    normalized = normalize_repository_url("https://" + repository)
    candidate = PurePosixPath(path)
    if (not normalized.startswith("github.com/") or not _SHA.fullmatch(revision)
            or not path or candidate.is_absolute() or ".." in candidate.parts):
        raise AssertionFormatError("evidence reference is not a safe immutable GitHub blob")
    return EvidenceReference(
        repository_url="https://" + normalized,
        revision=revision.lower(),
        path=candidate.as_posix(),
    )


def parse_assertion(path: str, raw: str) -> AssertionDocument:
    """Validate the narrow sidecar schema before it reaches PostgreSQL."""
    if not path.startswith("assertions/") or not path.endswith(".md"):
        raise AssertionFormatError("assertions must be Markdown files under assertions/")
    meta, _body = parse_frontmatter(raw)
    key = _bounded(meta, "id", limit=120)
    if not _KEY.fullmatch(key):
        raise AssertionFormatError("assertion id must be a stable lower-case key")
    state = _bounded(meta, "state", limit=30).lower()
    if state not in _STATES:
        raise AssertionFormatError("assertion state must be accepted, contested, or retracted")
    try:
        confidence = float(meta.get("confidence", "1.0"))
    except (TypeError, ValueError) as exc:
        raise AssertionFormatError("assertion confidence must be a number") from exc
    if not 0 <= confidence <= 1:
        raise AssertionFormatError("assertion confidence must be between 0 and 1")
    raw_evidence = meta.get("evidence", [])
    values = raw_evidence if isinstance(raw_evidence, list) else [raw_evidence]
    references = tuple(_reference(value) for value in values if isinstance(value, str) and value)
    if not references:
        raise AssertionFormatError("accepted assertions require at least one immutable evidence reference")
    if len(references) > 20:
        raise AssertionFormatError("an assertion may cite at most 20 evidence blobs")
    if len(set(references)) != len(references):
        raise AssertionFormatError("assertion repeats an evidence reference")
    return AssertionDocument(
        assertion_key=key,
        subject=_bounded(meta, "subject"), predicate=_bounded(meta, "predicate"),
        object_value=_bounded(meta, "object"), state=state, confidence=confidence,
        evidence=references,
    )


def prepare_sidecar_sync(
    client: Any,
    *,
    source_repository: str,
    evidence_repository: str,
    revision: str,
    installation_id: int,
    max_blob_bytes: int,
    max_files: int,
) -> list[PreparedAssertion]:
    """Fetch and validate an entire sidecar revision before any database write."""
    source = normalize_repository_url(source_repository)
    if not source.startswith("github.com/"):
        raise AssertionFormatError("project source repository must be GitHub")
    entries = client.list_blobs(
        repository_url=evidence_repository, revision=revision,
        installation_id=installation_id, prefix="assertions", max_files=max_files,
        allowed_suffixes=(".md",))
    prepared: list[tuple[AssertionDocument, GitBlob]] = []
    for entry in entries:
        blob = client.get_text_blob(
            repository_url=evidence_repository, revision=revision, path=entry["path"],
            git_sha=entry["sha"], installation_id=installation_id,
            max_bytes=max_blob_bytes)
        prepared.append((parse_assertion(blob.path, blob.content), blob))

    references: dict[str, EvidenceReference] = {}
    for document, _source_blob in prepared:
        for reference in document.evidence:
            if normalize_repository_url(reference.repository_url) != source:
                raise AssertionFormatError(
                    "sidecar assertions may only cite the bound project source repository")
            references[reference.external_id] = reference
    if len(references) > max_files:
        raise AssertionFormatError("sidecar references exceed the configured sync limit")

    fetched: dict[str, GitBlob] = {}
    for external_id, reference in references.items():
        fetched[external_id] = client.get_text_file(
            repository_url=reference.repository_url, revision=reference.revision,
            path=reference.path, installation_id=installation_id,
            max_bytes=max_blob_bytes)
    return [PreparedAssertion(
        document=document,
        source_blob=source_blob,
        supporting_blobs=tuple(fetched[reference.external_id] for reference in document.evidence),
    ) for document, source_blob in prepared]


def record_git_blob(
    conn: Connection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    repository_url: str,
    revision: str,
    path: str,
    content_sha256: str,
    byte_size: int,
    observed_at: Any,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Record immutable Git blob provenance without copying its content into the DB."""
    normalized = normalize_repository_url(repository_url)
    safe_path = PurePosixPath(path)
    if (not normalized.startswith("github.com/") or not _SHA.fullmatch(revision)
            or safe_path.is_absolute() or ".." in safe_path.parts):
        raise AssertionFormatError("cannot record an unsafe Git blob artifact")
    external_id = f"git_blob:{normalized}@{revision.lower()}:{safe_path.as_posix()}"
    row = conn.execute(text(
        "INSERT INTO mem.evidence_artifacts "
        "(tenant_id, project_id, provider, kind, external_id, source_repository, "
        " source_revision, location, content_sha256, byte_size, observed_at, metadata) "
        "VALUES (:tenant, :project, 'github', 'git_blob', :external, :repository, "
        "        :revision, :location, :digest, :size, :observed, CAST(:metadata AS jsonb)) "
        "ON CONFLICT (project_id, provider, kind, external_id) DO NOTHING "
        "RETURNING id"
    ), {
        "tenant": str(tenant_id), "project": str(project_id), "external": external_id,
        "repository": "https://" + normalized, "revision": revision.lower(),
        "location": safe_path.as_posix(), "digest": content_sha256, "size": byte_size,
        "observed": observed_at, "metadata": json.dumps(metadata or {}),
    }).scalar_one_or_none()
    if row is None:
        row = conn.execute(text(
            "SELECT id FROM mem.evidence_artifacts "
            " WHERE project_id = :project AND provider = 'github' "
            "   AND kind = 'git_blob' AND external_id = :external"),
            {"project": str(project_id), "external": external_id}).scalar_one()
    return UUID(str(row))


def resolve_evidence_artifacts(
    conn: Connection, *, document: AssertionDocument
) -> list[UUID]:
    """Require every cited immutable blob to already exist in this project scope."""
    ids: list[UUID] = []
    for reference in document.evidence:
        row = conn.execute(text(
            "SELECT id FROM mem.evidence_artifacts "
            " WHERE provider = 'github' AND kind = 'git_blob' AND external_id = :external"),
            {"external": reference.external_id}).scalar_one_or_none()
        if row is None:
            raise AssertionFormatError(
                f"assertion evidence has not been ingested: {reference.external_id}")
        ids.append(UUID(str(row)))
    return ids


def write_assertion(
    conn: Connection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    document: AssertionDocument,
    source_repository: str,
    source_path: str,
    source_revision: str,
    source_artifact_id: UUID,
    supporting_artifact_ids: list[UUID],
    accepted_by: UUID | None = None,
) -> dict[str, Any]:
    """Persist a reviewed assertion and atomically supersede its prior revision."""
    if not _SHA.fullmatch(source_revision):
        raise AssertionFormatError("assertion source revision must be an immutable Git SHA")
    if not supporting_artifact_ids:
        raise AssertionFormatError("an assertion requires supporting evidence")

    artifacts = [source_artifact_id, *supporting_artifact_ids]
    count = conn.execute(text(
        "SELECT count(*) FROM mem.evidence_artifacts WHERE id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": "{" + ",".join(str(value) for value in artifacts) + "}"}).scalar_one()
    if count != len(set(artifacts)):
        raise AssertionFormatError("assertion references an artifact outside the current project scope")

    row = conn.execute(text(
        "INSERT INTO mem.evidence_assertions "
        "(tenant_id, project_id, assertion_key, subject, predicate, object_value, attributes, "
        " state, confidence, source_repository, source_path, source_revision, accepted_at, accepted_by) "
        "VALUES (:tenant, :project, :key, :subject, :predicate, :object, '{}'::jsonb, "
        "        :state, :confidence, :repository, :path, :revision, "
        "        CASE WHEN :state = 'accepted' THEN now() ELSE NULL END, :accepted_by) "
        "ON CONFLICT (project_id, assertion_key, source_revision) DO UPDATE SET "
        " subject = EXCLUDED.subject, predicate = EXCLUDED.predicate, object_value = EXCLUDED.object_value, "
        " state = EXCLUDED.state, confidence = EXCLUDED.confidence, updated_at = now() "
        "RETURNING id"
    ), {
        "tenant": str(tenant_id), "project": str(project_id), "key": document.assertion_key,
        "subject": document.subject, "predicate": document.predicate,
        "object": document.object_value, "state": document.state,
        "confidence": document.confidence,
        "repository": "https://" + normalize_repository_url(source_repository),
        "path": source_path, "revision": source_revision.lower(),
        "accepted_by": str(accepted_by) if accepted_by else None,
    }).scalar_one()
    assertion_id = UUID(str(row))
    conn.execute(text(
        "UPDATE mem.evidence_assertions SET state = 'superseded', superseded_by = :new, updated_at = now() "
        " WHERE tenant_id = :tenant AND project_id = :project AND assertion_key = :key "
        "   AND id <> :new AND state IN ('accepted', 'contested')"),
        {"new": str(assertion_id), "tenant": str(tenant_id), "project": str(project_id),
         "key": document.assertion_key})
    links = [(source_artifact_id, "derived_from"),
             *((artifact_id, "supports") for artifact_id in supporting_artifact_ids)]
    for artifact_id, role in links:
        conn.execute(text(
            "INSERT INTO mem.assertion_evidence "
            "(assertion_id, artifact_id, tenant_id, project_id, role) "
            "VALUES (:assertion, :artifact, :tenant, :project, :role) "
            "ON CONFLICT DO NOTHING"),
            {"assertion": str(assertion_id), "artifact": str(artifact_id),
             "tenant": str(tenant_id), "project": str(project_id), "role": role})
    return {"id": str(assertion_id), "state": document.state,
            "evidence_count": len(supporting_artifact_ids)}


def persist_sidecar_sync(
    conn: Connection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    evidence_repository: str,
    evidence_revision: str,
    observed_at: Any,
    prepared: list[PreparedAssertion],
    accepted_by: UUID | None,
) -> dict[str, int]:
    """Persist a prepared revision and retract assertions deleted from that tree."""
    imported = 0
    for item in prepared:
        source_artifact = record_git_blob(
            conn, tenant_id=tenant_id, project_id=project_id,
            repository_url=evidence_repository, revision=evidence_revision,
            path=item.source_blob.path, content_sha256=item.source_blob.content_sha256,
            byte_size=item.source_blob.byte_size, observed_at=observed_at,
            metadata={"git_blob": item.source_blob.git_sha, "role": "assertion_source"})
        supports = [record_git_blob(
            conn, tenant_id=tenant_id, project_id=project_id,
            repository_url=blob.repository, revision=blob.revision, path=blob.path,
            content_sha256=blob.content_sha256, byte_size=blob.byte_size,
            observed_at=observed_at,
            metadata={"git_blob": blob.git_sha, "role": "assertion_support"})
            for blob in item.supporting_blobs]
        write_assertion(
            conn, tenant_id=tenant_id, project_id=project_id, document=item.document,
            source_repository=evidence_repository, source_path=item.source_blob.path,
            source_revision=evidence_revision, source_artifact_id=source_artifact,
            supporting_artifact_ids=supports, accepted_by=accepted_by)
        imported += 1
    present_paths = {item.source_blob.path for item in prepared}
    rows = conn.execute(text(
        "SELECT id, source_path FROM mem.evidence_assertions "
        " WHERE tenant_id = :tenant AND project_id = :project "
        "   AND source_repository = :repository AND state IN ('accepted', 'contested')"),
        {"tenant": str(tenant_id), "project": str(project_id),
         "repository": "https://" + normalize_repository_url(evidence_repository)}).mappings().all()
    removed = [str(row["id"]) for row in rows if row["source_path"] not in present_paths]
    if removed:
        conn.execute(text(
            "UPDATE mem.evidence_assertions "
            "   SET state = 'retracted', updated_at = now() "
            " WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": "{" + ",".join(removed) + "}"})
    return {"assertions": imported, "supporting_blobs": sum(
        len(item.supporting_blobs) for item in prepared), "retracted": len(removed)}
