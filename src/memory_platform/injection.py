"""Indirect prompt-injection heuristic.

00-MASTER-BLUEPRINT.md §33: "An auto-ingesting memory system is a persistence
layer for indirect prompt injection. One poisoned README becomes permanent
instruction for every future agent."

04-EVALUATION.md Suite 5 asks for a heuristic that flags content written to steer
an agent, so it is "ingested at tier 0/1, flagged, never retrieved as guidance".

FLAG AND QUARANTINE — DO NOT REJECT. This is the opposite policy to
secret_scan.py, deliberately:

  * A credential in a repository is a fact about the world that must be fixed at
    the source, so refusing the file is the only honest response.
  * Injected instructions are CONTENT. Refusing to ingest a poisoned README does
    not make the README go away, and it destroys the audit trail of an attack in
    progress. Quarantining it keeps the evidence, keeps it out of every context
    pack, and puts it at the top of the review inbox.

TRUST-DEPENDENT SEVERITY. The same sentence means different things depending on
where it came from:

  * From `git`/`human` — a reviewed Plane A file. A human approved this in a pull
    request, and human review IS the control for Plane A (ADR-0002). It is
    flagged and logged loudly, but not downgraded: silently quarantining reviewed
    content would mean an ADR that legitimately DISCUSSES prompt injection (this
    project has several) disappears from its own memory.
  * From anything else — unreviewed. Capped at `untrusted` and quarantined,
    which is exactly Suite 5's expected defence.

The heuristic is deliberately shallow. A determined attacker will phrase around
it; that is why it is one layer among several (tier caps, quarantine, the pack's
no-instructions boundary, and human review) rather than the defence.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("memory.injection")

# Explicit opt-out for documents that legitimately discuss these techniques —
# ADRs, runbooks and this project's own test fixtures.
ALLOW_MARKERS = ("memory:allow-injection-example", "pragma: allowlist injection")


@dataclass(frozen=True)
class Signal:
    kind: str
    line: int
    excerpt: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.kind} ({self.excerpt!r})"


PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("instruction-override", re.compile(
        r"\b(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+)?"
        r"(previous|prior|earlier|above|preceding)\s+"
        r"(instruction|prompt|rule|direction|context)", re.I)),
    ("agent-directive", re.compile(
        r"\b(ai\s+agents?|assistants?|language\s+models?|llms?|claude|gpt|copilot)\s*[:,]\s*"
        r"(you\s+)?(must|should|always|never|do not|don't)\b", re.I)),
    ("system-prompt-claim", re.compile(
        r"\b(system\s+prompt|you\s+are\s+now|new\s+instructions?|updated\s+instructions?"
        r"|from\s+now\s+on\s+you)\b", re.I)),
    ("security-downgrade", re.compile(
        r"\b(disable|skip|bypass|turn\s+off|ignore)\s+"
        r"(tls|ssl|certificate|cert\s+verification|auth\w*|security|validation"
        r"|sandbox|safety|review)\b", re.I)),
    ("exfiltration", re.compile(
        r"\b(send|post|upload|exfiltrate|forward|leak)\s+"
        r"(the\s+)?(secret|token|credential|key|env|password|\.env)\w*\s+"
        r"(to|at|into)\b", re.I)),
    ("autonomy-escalation", re.compile(
        r"\b(without\s+(asking|confirmation|human|review|approval)"
        r"|do\s+not\s+(ask|confirm|tell)\s+the\s+(user|human|operator)"
        r"|auto[- ]?approve)\b", re.I)),
]

# Keyword stuffing: a memory crafted to rank first for every query. Suite 5 lists
# it as an attack in its own right.
STUFFING_MIN_WORDS = 40
STUFFING_RATIO = 0.28


def detect_stuffing(content: str) -> Signal | None:
    """Flag abnormally repetitive content.

    A document where one token is >28% of the text is not prose. It is either
    generated padding or an attempt to dominate the lexical arm — and the lexical
    arm ranks on term frequency, so it would work.
    """
    words = re.findall(r"[a-z0-9_]{3,}", (content or "").lower())
    if len(words) < STUFFING_MIN_WORDS:
        return None
    counts: dict[str, int] = {}
    for w in words:
        counts[w] = counts.get(w, 0) + 1
    token, n = max(counts.items(), key=lambda kv: kv[1])
    ratio = n / len(words)
    if ratio >= STUFFING_RATIO:
        return Signal("keyword-stuffing", 0, f"{token!r} is {ratio:.0%} of {len(words)} words")
    return None


def scan(content: str) -> list[Signal]:
    """Return injection signals. Empty list means nothing detected."""
    if any(m in (content or "") for m in ALLOW_MARKERS):
        return []

    signals: list[Signal] = []
    for lineno, line in enumerate((content or "").splitlines(), start=1):
        for kind, pat in PATTERNS:
            m = pat.search(line)
            if m:
                signals.append(Signal(kind, lineno, m.group(0)[:60]))
    stuffed = detect_stuffing(content)
    if stuffed:
        signals.append(stuffed)
    return signals


# Sources whose content a human reviewed before it landed (ADR-0002, Plane A).
REVIEWED_SOURCES = {"git", "human"}


def assess(content: str, source_type: str) -> dict:
    """Decide what to do about detected injection, given where it came from.

    Returns {signals, flagged, quarantine, tier_cap}. `tier_cap` of None means
    "do not downgrade".
    """
    signals = scan(content)
    if not signals:
        return {"signals": [], "flagged": False, "quarantine": False, "tier_cap": None}

    reviewed = (source_type or "").lower() in REVIEWED_SOURCES
    detail = "; ".join(str(s) for s in signals[:4])

    if reviewed:
        # Loud, but not downgraded. A reviewed file carrying these phrases is
        # either a document about prompt injection or a compromised review, and
        # the second is a people problem that silently hiding the file does not
        # solve.
        log.warning("injection signals in REVIEWED content (%s): %s — flagged, "
                    "tier unchanged because a human approved this in review",
                    source_type, detail)
        return {"signals": [str(s) for s in signals], "flagged": True,
                "quarantine": False, "tier_cap": None}

    log.error("injection signals in UNREVIEWED content (%s): %s — quarantined at "
              "tier untrusted", source_type, detail)
    return {"signals": [str(s) for s in signals], "flagged": True,
            "quarantine": True, "tier_cap": "untrusted"}
