"""Mail state-token payload: an account-wide vector of per-mailbox IMAP
CONDSTORE/QRESYNC cursors, plus the deterministic Email id codec.

Why a vector and not a single scalar: JMAP `Email`/`Mailbox` state is
account-wide, but IMAP's CONDSTORE `HIGHESTMODSEQ` is per-mailbox. With no
local database to hand out a single incrementing counter (as the reference
jmap-perl server does), the only zero-storage account-wide cursor is the
full set of per-mailbox `(UIDVALIDITY, HIGHESTMODSEQ)` pairs, encoded into
the opaque state string itself.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from jmap_bridge.state import InvalidStateToken, decode_state, encode_state

STATE_KIND = "mailstate"


class CannotCalculateChanges(Exception):
    """Raised when a `changes`/`queryChanges` diff can't be honestly
    computed from the given `sinceState` — e.g. a mailbox's UIDVALIDITY
    rotated (its entire UID space is a new identity space), or the backend
    lacks CONDSTORE/QRESYNC. Handlers should catch this and return the
    RFC 8620 `cannotCalculateChanges` error rather than guessing.
    """


@dataclass(frozen=True, slots=True)
class MailboxCursor:
    uidvalidity: int
    highestmodseq: int


@dataclass(frozen=True, slots=True)
class MailboxChanges:
    created: list[str]
    updated: list[str]
    destroyed: list[str]


def encode_mail_state(cursors: dict[str, MailboxCursor]) -> str:
    payload = {
        "mailboxes": {
            name: {"uv": c.uidvalidity, "ms": c.highestmodseq}
            for name, c in cursors.items()
        }
    }
    return encode_state(STATE_KIND, payload)


def decode_mail_state(token: str) -> dict[str, MailboxCursor]:
    payload = decode_state(token, STATE_KIND)
    mailboxes = payload.get("mailboxes")
    if not isinstance(mailboxes, dict):
        raise InvalidStateToken("mail state payload missing 'mailboxes'")
    result: dict[str, MailboxCursor] = {}
    try:
        for name, cursor in mailboxes.items():
            result[name] = MailboxCursor(
                uidvalidity=int(cursor["uv"]), highestmodseq=int(cursor["ms"])
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidStateToken(f"malformed mailbox cursor: {exc}") from exc
    return result


def diff_mailbox_state(
    old: dict[str, MailboxCursor], new: dict[str, MailboxCursor]
) -> MailboxChanges:
    """Compute Mailbox/changes created/updated/destroyed from two cursor
    vectors. Raises CannotCalculateChanges if any mailbox present in both
    has a rotated UIDVALIDITY — that mailbox's UID space is a different
    identity space now, so no honest diff exists; the caller must force a
    full client resync instead of fabricating one.
    """
    created = [name for name in new if name not in old]
    destroyed = [name for name in old if name not in new]
    updated = []
    for name in new.keys() & old.keys():
        if new[name].uidvalidity != old[name].uidvalidity:
            raise CannotCalculateChanges(
                f"mailbox {name!r} UIDVALIDITY rotated "
                f"({old[name].uidvalidity} -> {new[name].uidvalidity})"
            )
        if new[name].highestmodseq != old[name].highestmodseq:
            updated.append(name)
    return MailboxChanges(created=created, updated=updated, destroyed=destroyed)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def encode_email_id(mailbox: str, uidvalidity: int, uid: int) -> str:
    """Deterministic JMAP Email id derived from (mailbox, uidvalidity, uid)
    — no id-mapping table needed, decode_email_id is a pure inverse. JMAP
    ids must match ^[A-Za-z0-9_-]+$ (RFC 8620 SS1.2), which base64url satisfies.
    """
    raw = json.dumps([mailbox, uidvalidity, uid], separators=(",", ":")).encode("utf-8")
    return "E" + _b64url_encode(raw)


def decode_email_id(email_id: str) -> tuple[str, int, int]:
    """Inverse of encode_email_id. Raises ValueError on malformed input —
    callers (Email/get, Email/set) should map that to a `notFound` result,
    not propagate it as a server error, since it just means the client
    handed back an id that isn't (or is no longer) one of ours.
    """
    if not email_id.startswith("E"):
        raise ValueError(f"not a mail-backed Email id: {email_id!r}")
    try:
        mailbox, uidvalidity, uid = json.loads(_b64url_decode(email_id[1:]))
        return str(mailbox), int(uidvalidity), int(uid)
    except Exception as exc:
        raise ValueError(f"malformed Email id {email_id!r}: {exc}") from exc
