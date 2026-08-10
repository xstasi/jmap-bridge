"""Opaque JMAP state-string codec.

JMAP `state` strings (RFC 8620 SS5) are opaque to the client: it stores
whatever we hand back and echoes it as `sinceState` on the next `changes`
call. Because we keep no local storage, every backend cursor we need to
resume from (IMAP per-mailbox modseq vectors, WebDAV sync-tokens, ...) must
be encoded *inside* the state string itself, so the string is the only
place that information lives.

This module owns the generic envelope (base64url JSON, versioned, tagged
with a `kind` discriminant). Backend-specific payload shapes live in
`backends/imap/modseq_state.py` and the future CalDAV/CardDAV sync_state
modules.
"""

from __future__ import annotations

import base64
import json
from typing import Any


class InvalidStateToken(ValueError):
    """Raised when a client-supplied state string can't be decoded, or was
    encoded for a different `kind` than the caller expected. Callers should
    treat this the same as `cannotCalculateChanges` (RFC 8620 SS5.2) rather
    than crashing: the client sent us garbage or a state from a different
    account/server, so we can't compute a diff from it.
    """


def encode_state(kind: str, payload: dict[str, Any]) -> str:
    """Encode `payload` as an opaque JMAP state string tagged with `kind`.

    `sort_keys=True` makes the encoding deterministic: two equal payloads
    always produce the same string, so states can be compared by equality
    (JMAP allows this) without decoding.
    """
    envelope = {"v": 1, "kind": kind, "payload": payload}
    raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_state(token: str, expected_kind: str) -> dict[str, Any]:
    """Decode a JMAP state string previously produced by `encode_state`.

    Raises `InvalidStateToken` for anything malformed, version-mismatched,
    or tagged with a different `kind` — never returns a partial/best-effort
    result, since a silently-wrong diff is worse than an honest failure.
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        envelope = json.loads(raw)
    except Exception as exc:
        raise InvalidStateToken(f"malformed state token: {exc}") from exc

    if not isinstance(envelope, dict):
        raise InvalidStateToken("state token did not decode to an object")
    if envelope.get("v") != 1:
        raise InvalidStateToken(f"unsupported state token version: {envelope.get('v')!r}")
    if envelope.get("kind") != expected_kind:
        raise InvalidStateToken(
            f"state token kind mismatch: expected {expected_kind!r}, got {envelope.get('kind')!r}"
        )
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise InvalidStateToken("state token payload is not an object")
    return payload
