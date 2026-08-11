"""MRTR — the confirmation step for actions that must not happen silently.

02-MCP-CONTRACT.md:

    MRTR for confirmations. Scope promotion, retraction, ADR creation and
    cross-project grants return `InputRequiredResult` with `inputRequests`; the
    client retries with `inputResponses`. Correlate across retries with your own
    identifier in `requestState`.

WHICH ACTIONS, AND WHY THESE. Not "destructive" ones — the platform has no
destructive tool. The list is the actions whose effect is on TRUST rather than on
data: retraction removes something an agent may already be relying on, an ADR
enters the authoritative plane, a grant lets one project read another, and scope
promotion raises a claim's standing. Each is the kind of thing an agent can be
argued into by content it read (Suite 5), and a confirmation is what puts a human
between the argument and the effect.

WHY requestState IS SIGNED. The obvious implementation mints a random token,
remembers it, and accepts the retry that quotes it. That correlates the retry
with the REQUEST but not with the OPERATION, and the two come apart: an agent
that obtains a confirmation for retracting memory A can replay the same token to
retract memory B, because nothing in the token says which memory was approved.
The human confirmed one sentence and authorised another.

So the token is an HMAC over the operation itself — tool, op, and the arguments
that determine the effect. A confirmation is therefore valid for exactly the
action it was shown, and for nothing else. It also carries an expiry, because an
approval from an hour ago is not evidence about now, and being stateless it needs
no store to revoke against.

The secret is the one already used to sign the platform's own tokens. If none is
configured, confirmations are still REQUIRED — they simply cannot be replayed
across process restarts, which is the safe direction to fail.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from .config import settings

log = logging.getLogger("memory.mrtr")

# How long a confirmation stays usable. Long enough for a person to read the
# question and answer it; short enough that an approval cannot be banked.
CONFIRMATION_TTL_S = 600

CONFIRM_KEY = "confirm"


def _secret() -> bytes:
    cfg = settings()
    for attr in ("jwt_secret", "api_token", "secret_key"):
        value = getattr(cfg, attr, "") or ""
        if value:
            return str(value).encode()
    # No configured secret: derive a per-process one. Confirmations still work
    # within a process and simply do not survive a restart, which fails closed.
    global _EPHEMERAL
    try:
        return _EPHEMERAL
    except NameError:
        import secrets as _secrets

        _EPHEMERAL = _secrets.token_bytes(32)
        log.warning("no signing secret configured; MRTR confirmations will not "
                    "survive a restart of this process")
        return _EPHEMERAL


def _canonical(tool: str, args: dict[str, Any]) -> str:
    """The operation, reduced to what determines its effect.

    Only the fields that change WHAT HAPPENS are included. Including everything
    would make a confirmation fail because the client re-sent a different
    token_budget; including too little is how a token for one memory authorises
    another.
    """
    material = {
        "tool": tool,
        "op": args.get("op", "assert"),
        "type": args.get("type", ""),
        "ref": args.get("ref", "") or args.get("memory_id", ""),
        "title": args.get("title", ""),
        "content_sha": hashlib.sha256(
            (args.get("content") or "").encode()).hexdigest()[:16],
        "target_project": args.get("target_project", ""),
    }
    return json.dumps(material, sort_keys=True, separators=(",", ":"))


def requires_confirmation(tool: str, args: dict[str, Any]) -> str | None:
    """Return the reason this call needs a human, or None."""
    if tool != "memory_write":
        return None
    op = (args.get("op") or "assert").lower()
    mtype = (args.get("type") or "").lower()
    if op == "retract":
        return ("Retraction removes a memory from every future context pack. "
                "Nothing is deleted — the record is archived with an audit "
                "entry — but agents relying on it will stop seeing it.")
    if op == "supersede":
        return ("Supersession replaces what the project currently believes. The "
                "previous version stays answerable through an as-of query.")
    if mtype == "decision":
        return ("A decision is authoritative knowledge and belongs in git "
                "(ADR-0002). Confirming opens a pull request against "
                ".memory/decisions/ rather than writing a row.")
    return None


def issue(tool: str, args: dict[str, Any], reason: str) -> dict[str, Any]:
    """Build the InputRequiredResult the client must answer."""
    expires = int(time.time()) + CONFIRMATION_TTL_S
    payload = f"{expires}.{_canonical(tool, args)}"
    digest = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    state = f"{expires}.{base64.urlsafe_b64encode(digest).decode().rstrip('=')}"

    return {
        # 02-MCP-CONTRACT.md §216: every result carries a resultType of
        # "complete" or "input_required". Set EXPLICITLY here because
        # mcp_server._result defaults it to "complete" — which would have
        # announced this confirmation prompt as a finished result, so a strict
        # client would take the answer and never ask the human. The inputRequests
        # below would have been decoration.
        "resultType": "input_required",
        # The extension's shape: not an error. An error tells the agent it did
        # something wrong and invites a retry with different arguments; this is a
        # question with a resumable answer.
        "isError": False,
        "requestState": state,
        "inputRequests": [{
            "id": CONFIRM_KEY,
            "type": "boolean",
            "title": "Confirm this action",
            "description": reason,
            "required": True,
        }],
        "content": [{"type": "text", "text": (
            f"Confirmation required.\n\n{reason}\n\n"
            "Retry this call with the same arguments, the requestState above, "
            f"and inputResponses {{\"{CONFIRM_KEY}\": true}}."
        )}],
    }


def verify(tool: str, args: dict[str, Any], request_state: str | None,
           responses: dict[str, Any] | None) -> tuple[bool, str]:
    """Check a confirmation. Returns (ok, reason_if_not)."""
    if not request_state:
        return False, "no requestState was returned with the confirmation"
    answer = (responses or {}).get(CONFIRM_KEY)
    if answer is not True:
        return False, "the action was not confirmed"

    try:
        expires_raw, provided = request_state.split(".", 1)
        expires = int(expires_raw)
    except (ValueError, AttributeError):
        return False, "requestState is malformed"
    if expires < int(time.time()):
        return False, "the confirmation has expired; ask again"

    payload = f"{expires}.{_canonical(tool, args)}"
    digest = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    # compare_digest, not ==: a timing-variable comparison on a MAC is the
    # standard way this check is defeated.
    if not hmac.compare_digest(expected, provided):
        # The common cause is not an attack — it is the arguments having changed
        # between the question and the answer. Which is exactly the case that
        # must fail.
        return False, ("this confirmation was issued for a different action; "
                       "confirm the action you are performing")
    return True, ""
