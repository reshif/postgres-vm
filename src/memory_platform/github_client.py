"""Small, bounded GitHub App client for immutable Git evidence reads.

The worker uses this client to fetch an exact tree and blob content by SHA. It
does not clone a repository, run arbitrary Git commands, or consume a mounted
host checkout. That is the practical boundary between a GitHub-native pipeline
and the legacy local-ingestion model.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import PurePosixPath
import re
from typing import Any, Iterable
from urllib.parse import quote

import httpx
import jwt

from .github_evidence import normalize_repository_url


class GitHubApiError(RuntimeError):
    """The provider declined a bounded, authenticated evidence read."""


_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")


@dataclass(frozen=True)
class GitBlob:
    repository: str
    revision: str
    path: str
    git_sha: str
    content: str
    content_sha256: str
    byte_size: int


def repository_slug(repository_url: str) -> str:
    """Convert a canonical GitHub remote into the API's owner/repository form."""
    normalized = normalize_repository_url(repository_url)
    if not normalized.startswith("github.com/"):
        raise GitHubApiError("only github.com repositories are supported")
    parts = normalized.split("/")
    if len(parts) != 3 or not all(parts):
        raise GitHubApiError("repository must name exactly one GitHub owner and repository")
    return f"{parts[1]}/{parts[2]}"


def _private_key(value: str) -> str:
    """Accept PEM content from a secret manager, including escaped newlines."""
    key = (value or "").strip()
    if key.startswith("-----BEGIN"):
        return key.replace("\\n", "\n")
    raise GitHubApiError("MEMORY_GITHUB_PRIVATE_KEY must contain a PEM private key")


def _safe_path(path: str) -> str:
    candidate = PurePosixPath(path)
    if not path or candidate.is_absolute() or ".." in candidate.parts:
        raise GitHubApiError("GitHub tree returned an unsafe path")
    return candidate.as_posix()


def _revision(value: str) -> str:
    if not _SHA.fullmatch(value or ""):
        raise GitHubApiError("an immutable 7-64 character hexadecimal revision is required")
    return value.lower()


class GitHubAppClient:
    """Authenticated reads restricted to explicitly requested immutable objects."""

    def __init__(
        self,
        *,
        app_id: str,
        private_key: str,
        api_url: str = "https://api.github.com",
        http: httpx.Client | None = None,
        now: datetime | None = None,
    ) -> None:
        if not str(app_id).isdigit():
            raise GitHubApiError("MEMORY_GITHUB_APP_ID must be a numeric GitHub App id")
        self.app_id = str(app_id)
        self.private_key = _private_key(private_key)
        self.api_url = api_url.rstrip("/")
        self.http = http or httpx.Client(timeout=30.0)
        self._owns_http = http is None
        self.now = now
        self._installation_tokens: dict[int, str] = {}

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> "GitHubAppClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def app_jwt(self) -> str:
        now = self.now or datetime.now(timezone.utc)
        issued = int(now.timestamp()) - 30
        return jwt.encode(
            {"iat": issued, "exp": issued + 9 * 60, "iss": self.app_id},
            self.private_key,
            algorithm="RS256",
        )

    def installation_token(self, installation_id: int) -> str:
        if installation_id <= 0:
            raise GitHubApiError("GitHub installation id must be positive")
        cached = self._installation_tokens.get(installation_id)
        if cached:
            return cached
        response = self.http.post(
            f"{self.api_url}/app/installations/{installation_id}/access_tokens",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.app_jwt()}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if response.status_code != 201:
            raise GitHubApiError(
                f"GitHub installation token request failed ({response.status_code})")
        value = response.json().get("token")
        if not isinstance(value, str) or not value:
            raise GitHubApiError("GitHub installation token response was invalid")
        self._installation_tokens[installation_id] = value
        return value

    def _get(self, path: str, *, installation_id: int) -> dict[str, Any]:
        response = self.http.get(
            f"{self.api_url}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.installation_token(installation_id)}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if response.status_code != 200:
            raise GitHubApiError(f"GitHub API read failed ({response.status_code}) for {path}")
        body = response.json()
        if not isinstance(body, dict):
            raise GitHubApiError(f"GitHub API returned a non-object for {path}")
        return body

    def list_blobs(
        self,
        *,
        repository_url: str,
        revision: str,
        installation_id: int,
        prefix: str = "",
        max_files: int = 250,
        allowed_suffixes: Iterable[str] = (".md", ".txt", ".json"),
    ) -> list[dict[str, Any]]:
        """List a bounded set of safe text candidates at exactly ``revision``."""
        revision = _revision(revision)
        if max_files < 1:
            raise GitHubApiError("max_files must be positive")
        repository = repository_slug(repository_url)
        tree = self._get(
            f"/repos/{repository}/git/trees/{revision}?recursive=1",
            installation_id=installation_id,
        )
        if tree.get("truncated") is True:
            raise GitHubApiError("GitHub tree is truncated; narrow the evidence path")
        entries = tree.get("tree")
        if not isinstance(entries, list):
            raise GitHubApiError("GitHub tree response has no tree entries")
        safe_prefix = _safe_path(prefix).rstrip("/") if prefix else ""
        suffixes = tuple(allowed_suffixes)
        selected: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "blob":
                continue
            path = _safe_path(str(entry.get("path") or ""))
            if safe_prefix and not path.startswith(safe_prefix + "/"):
                continue
            if suffixes and not path.endswith(suffixes):
                continue
            sha = entry.get("sha")
            size = entry.get("size")
            if not isinstance(sha, str) or not isinstance(size, int) or size < 0:
                continue
            selected.append({"path": path, "sha": sha, "size": size})
            if len(selected) > max_files:
                raise GitHubApiError("GitHub evidence tree exceeds the configured file limit")
        return selected

    def get_text_blob(
        self,
        *,
        repository_url: str,
        revision: str,
        path: str,
        git_sha: str,
        installation_id: int,
        max_bytes: int = 262_144,
    ) -> GitBlob:
        """Fetch one Git blob by object SHA and reject non-text or oversized input."""
        if max_bytes < 1:
            raise GitHubApiError("max_bytes must be positive")
        revision = _revision(revision)
        safe_path = _safe_path(path)
        repository = repository_slug(repository_url)
        blob = self._get(
            f"/repos/{repository}/git/blobs/{git_sha}", installation_id=installation_id)
        if blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
            raise GitHubApiError("GitHub blob was not base64 text content")
        try:
            raw = base64.b64decode(blob["content"], validate=True)
        except (ValueError, TypeError) as exc:
            raise GitHubApiError("GitHub blob content was not valid base64") from exc
        if len(raw) > max_bytes:
            raise GitHubApiError("GitHub blob exceeds the configured byte limit")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitHubApiError("GitHub blob is not UTF-8 text") from exc
        return GitBlob(
            repository=repository_url,
            revision=revision,
            path=safe_path,
            git_sha=git_sha,
            content=content,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            byte_size=len(raw),
        )

    def get_text_file(
        self,
        *,
        repository_url: str,
        revision: str,
        path: str,
        installation_id: int,
        max_bytes: int = 262_144,
    ) -> GitBlob:
        """Fetch one safe path at an immutable revision, never a branch name."""
        revision = _revision(revision)
        if max_bytes < 1:
            raise GitHubApiError("max_bytes must be positive")
        safe_path = _safe_path(path)
        repository = repository_slug(repository_url)
        record = self._get(
            f"/repos/{repository}/contents/{quote(safe_path, safe='/')}?ref={revision}",
            installation_id=installation_id)
        git_sha = record.get("sha")
        if not isinstance(git_sha, str) or not git_sha:
            raise GitHubApiError("GitHub file response had no blob SHA")
        if record.get("encoding") != "base64" or not isinstance(record.get("content"), str):
            raise GitHubApiError("GitHub file was not base64 text content")
        try:
            raw = base64.b64decode(record["content"], validate=True)
        except (ValueError, TypeError) as exc:
            raise GitHubApiError("GitHub file content was not valid base64") from exc
        if len(raw) > max_bytes:
            raise GitHubApiError("GitHub file exceeds the configured byte limit")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitHubApiError("GitHub file is not UTF-8 text") from exc
        return GitBlob(
            repository=repository_url, revision=revision, path=safe_path, git_sha=git_sha,
            content=content, content_sha256=hashlib.sha256(raw).hexdigest(),
            byte_size=len(raw),
        )
