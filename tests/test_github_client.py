"""GitHub App exact-SHA fetch contract, using a local HTTP transport only."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import sys

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, "/app/src")
from memory_platform.github_client import (  # noqa: E402
    GitHubApiError, GitHubAppClient, GitHubPatClient, repository_slug,
)

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def main() -> None:
    pem = private_key()
    calls: list[httpx.Request] = []
    content = b"---\nid: storage\n---\nPostgreSQL is the primary store.\n"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/app/installations/77/access_tokens":
            return httpx.Response(201, json={"token": "installation-token"})
        if request.url.path == "/repos/example/service/git/trees/aaaaaaaa":
            return httpx.Response(200, json={"tree": [
                {"path": "assertions/storage.md", "type": "blob", "sha": "blob-a", "size": len(content)},
                {"path": "src/main.py", "type": "blob", "sha": "blob-b", "size": 12},
                {"path": "assertions/image.png", "type": "blob", "sha": "blob-c", "size": 10},
            ], "truncated": False})
        if request.url.path == "/repos/example/service/git/blobs/blob-a":
            return httpx.Response(200, json={
                "encoding": "base64", "content": base64.b64encode(content).decode(),
            })
        if request.url.path == "/repos/example/service/contents/assertions/storage.md":
            return httpx.Response(200, json={
                "sha": "blob-file", "encoding": "base64",
                "content": base64.b64encode(content).decode(),
            })
        return httpx.Response(404, json={"message": "not found"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = GitHubAppClient(
        app_id="123", private_key=pem, api_url="https://github.test",
        http=http, now=datetime(2026, 8, 12, tzinfo=timezone.utc))

    print("\n1. App authentication")
    token = client.app_jwt()
    claims = jwt.decode(token, options={"verify_signature": False})
    check("App JWT is issued for the configured App id", claims.get("iss") == "123")
    check("SSH and HTTPS remotes resolve to owner/repository",
          repository_slug("git@github.com:example/service.git") == "example/service")
    try:
        repository_slug("https://gitlab.com/example/service")
        non_github = False
    except GitHubApiError:
        non_github = True
    check("non-GitHub remotes are refused", non_github)

    print("\n2. Exact revision reads")
    blobs = client.list_blobs(
        repository_url="https://github.com/example/service", revision="aaaaaaaa",
        installation_id=77, prefix="assertions", max_files=5)
    check("tree filtering selects only allowed evidence files", blobs == [
        {"path": "assertions/storage.md", "sha": "blob-a", "size": len(content)}], str(blobs))
    blob = client.get_text_blob(
        repository_url="https://github.com/example/service", revision="aaaaaaaa",
        path=blobs[0]["path"], git_sha=blobs[0]["sha"], installation_id=77)
    check("blob preserves requested source revision", blob.revision == "aaaaaaaa")
    check("blob is content-addressed", blob.content_sha256 != "" and blob.byte_size == len(content))
    file_at_sha = client.get_text_file(
        repository_url="https://github.com/example/service", revision="aaaaaaaa",
        path="assertions/storage.md", installation_id=77)
    check("path read pins the requested commit SHA", file_at_sha.revision == "aaaaaaaa" and
          file_at_sha.git_sha == "blob-file")
    check("installation token is requested once then cached",
          sum(1 for c in calls if c.url.path.endswith("access_tokens")) == 1)
    auth_headers = [c.headers.get("authorization") for c in calls if c.url.path.startswith("/repos/")]
    check("repository calls use the installation token", auth_headers ==
          ["Bearer installation-token", "Bearer installation-token", "Bearer installation-token"],
          str(auth_headers))

    print("\n3. Fine-grained PAT reads")
    pat_calls: list[httpx.Request] = []

    def pat_handler(request: httpx.Request) -> httpx.Response:
        pat_calls.append(request)
        if request.url.path == "/repos/example/service":
            return httpx.Response(200, json={"full_name": "example/service"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "octo-user"})
        if request.url.path == "/repos/example/service/git/trees/aaaaaaaa":
            return httpx.Response(200, json={"tree": [
                {"path": "assertions/storage.md", "type": "blob", "sha": "blob-a", "size": len(content)},
            ], "truncated": False})
        return httpx.Response(404, json={"message": "not found"})

    pat_http = httpx.Client(transport=httpx.MockTransport(pat_handler))
    pat = GitHubPatClient(token="github_pat_abcdefghijklmnopqrstuvwxyz", api_url="https://github.test", http=pat_http)
    login, scopes = pat.validate_repository("https://github.com/example/service")
    pat_blobs = pat.list_blobs(repository_url="https://github.com/example/service", revision="aaaaaaaa",
                               installation_id=0, prefix="assertions", max_files=5)
    check("PAT validation proves access to the bound repository", login == "octo-user" and scopes == ())
    check("PAT client preserves the exact-SHA tree bounds", pat_blobs == [
        {"path": "assertions/storage.md", "sha": "blob-a", "size": len(content)}])
    check("PAT is sent only as an Authorization header", all(
        request.headers.get("authorization") == "Bearer github_pat_abcdefghijklmnopqrstuvwxyz"
        for request in pat_calls), str([request.headers.get("authorization") for request in pat_calls]))
    pat.close()

    print("\n4. Bounds and unsafe input")
    try:
        client.get_text_blob(
            repository_url="https://github.com/example/service", revision="aaaaaaaa",
            path="../outside.md", git_sha="blob-a", installation_id=77)
        unsafe = False
    except GitHubApiError:
        unsafe = True
    check("unsafe paths are rejected", unsafe)
    try:
        client.get_text_blob(
            repository_url="https://github.com/example/service", revision="aaaaaaaa",
            path="assertions/storage.md", git_sha="blob-a", installation_id=77, max_bytes=4)
        oversize = False
    except GitHubApiError:
        oversize = True
    check("oversized blobs are rejected before persistence", oversize)

    http.close()
    failed = [name for ok, name in results if not ok]
    print(f"\n{'=' * 62}\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
