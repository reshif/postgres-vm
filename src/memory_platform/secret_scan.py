"""Secret scanning on the ingest path.

05-BUILD-PLAN Phase 1: "Secret scanning on the ingest path (hard reject, never
silent redaction)."

Hard reject is the whole point, so it is worth being explicit about why, because
the alternative looks friendlier: redaction produces a memory that an agent will
later quote as authoritative project knowledge, with a hole in it that nobody
knows about, while the real secret stays in git history where the redaction did
nothing. Rejecting keeps the operator's attention on the actual problem — a
credential is in the repository and must be rotated.

False positives therefore block a merge, which is the correct trade for this
system: a blocked ADR costs someone five minutes, a leaked key costs a rotation.
Patterns are anchored on issuer-specific prefixes rather than entropy heuristics
so that cost stays rare. ALLOW_MARKERS lets a document discuss a credential
format on purpose.

Module is deliberately named secret_scan, not secrets: the standard library owns
that name and a package-local shadow is the kind of import bug that surfaces
somewhere unrelated at 3am.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    kind: str
    line: int
    excerpt: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.kind} ({self.excerpt})"


# Issuer-prefixed patterns. Each one is a credential shape that essentially
# cannot occur by accident in prose.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Stripe secret key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    # Credentials embedded in a connection string. Placeholders are excluded
    # below so documentation examples do not trip it.
    ("credential in URL", re.compile(
        r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:([^\s/@]{6,})@", re.IGNORECASE)),
    ("AWS secret access key", re.compile(
        r"(?i)\baws_secret_access_key\b\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})\b")),
]

# Obvious non-secrets that match the shapes above. Kept small on purpose: a long
# allowlist is how a scanner quietly stops scanning.
PLACEHOLDERS = re.compile(
    r"(?i)\b(change[-_]?me|example|placeholder|redacted|your[-_]?\w+|"
    r"xxx+|\.{3,}|<[^>]+>|\$\{[^}]+\}|dummy|fake|sample|test[-_]?key)\b"
)

# An explicit, greppable opt-out for documents that discuss credential formats.
ALLOW_MARKERS = ("memory:allow-secret", "pragma: allowlist secret")


def scan(content: str) -> list[Finding]:
    """Return every credential-shaped match. Empty list means clean."""
    if any(m in content for m in ALLOW_MARKERS):
        return []

    findings: list[Finding] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        for kind, pat in PATTERNS:
            for m in pat.finditer(line):
                hit = m.group(m.lastindex or 0)
                if PLACEHOLDERS.search(hit) or PLACEHOLDERS.search(m.group(0)):
                    continue
                findings.append(Finding(kind, lineno, _mask(hit)))
    return findings


def _mask(s: str) -> str:
    """Never echo a live credential into logs, an API response, or an alert —
    that just copies it somewhere new."""
    s = s.strip()
    return f"{s[:4]}…{s[-2:]}" if len(s) > 12 else "…"


class SecretDetected(ValueError):
    """Raised instead of ingesting. Carries the findings for the operator."""

    def __init__(self, path: str, findings: list[Finding]) -> None:
        self.path = path
        self.findings = findings
        detail = "; ".join(str(f) for f in findings)
        super().__init__(
            f"REJECTED {path}: {len(findings)} possible credential(s) — {detail}. "
            "Nothing was ingested and nothing was redacted. Rotate the credential, "
            "remove it from the file AND from git history, then re-run ingestion. "
            f"If this is a documented example, add a '{ALLOW_MARKERS[0]}' marker."
        )
