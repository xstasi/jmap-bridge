"""IMAP mailbox <-> JMAP Mailbox (RFC 8621 SS2) mapping.

JMAP Mailbox ids are opaque strings the client round-trips; since we keep
no id-mapping table, the id is a deterministic encoding of the IMAP
mailbox name itself (mirrors the Email id approach in modseq_state.py).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

_SPECIAL_USE_TO_ROLE = {
    "\\ARCHIVE": "archive",
    "\\DRAFTS": "drafts",
    "\\JUNK": "junk",
    "\\SENT": "sent",
    "\\TRASH": "trash",
    "\\IMPORTANT": "important",
    "\\FLAGGED": "flagged",
    "\\ALL": "all",
}

_ROLE_SORT_ORDER = {
    "inbox": 1,
    "drafts": 10,
    "sent": 20,
    "important": 30,
    "archive": 40,
    "junk": 50,
    "trash": 60,
}
_DEFAULT_SORT_ORDER = 1000


def encode_mailbox_id(mailbox_name: str) -> str:
    raw = mailbox_name.encode("utf-8")
    return "M" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_mailbox_id(mailbox_id: str) -> str:
    if not mailbox_id.startswith("M"):
        raise ValueError(f"not a Mailbox id: {mailbox_id!r}")
    padded = mailbox_id[1:]
    padded += "=" * (-len(padded) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8")
    except Exception as exc:
        raise ValueError(f"malformed Mailbox id {mailbox_id!r}: {exc}") from exc


def _role_for(name: str, special_use_flags: frozenset[str]) -> str | None:
    if name.upper() == "INBOX":
        return "inbox"
    for flag in special_use_flags:
        role = _SPECIAL_USE_TO_ROLE.get(flag.upper())
        if role:
            return role
    return None


@dataclass(frozen=True, slots=True)
class ImapMailboxEntry:
    flags: frozenset[str]
    delimiter: str
    name: str


def imap_list_to_jmap_mailboxes(entries: list[ImapMailboxEntry]) -> list[dict]:
    """Map an IMAP LIST response into JMAP Mailbox objects, without the
    optional count properties (totalEmails/unreadEmails/...) which need a
    separate STATUS call per mailbox — callers add those on demand.

    Selectable-only: entries flagged \\Noselect (placeholder path
    components with no actual mailbox behind them) are skipped, matching
    JMAP's model where every Mailbox is a real, selectable mailbox.
    """
    by_name = {e.name for e in entries}
    mailboxes = []
    for entry in entries:
        if any(f.upper() == "\\NOSELECT" for f in entry.flags):
            continue
        segments = entry.name.split(entry.delimiter) if entry.delimiter else [entry.name]
        display_name = segments[-1]
        parent_name = entry.delimiter.join(segments[:-1]) if len(segments) > 1 else None
        parent_id = (
            encode_mailbox_id(parent_name) if parent_name and parent_name in by_name else None
        )
        role = _role_for(entry.name, entry.flags)
        mailboxes.append(
            {
                "id": encode_mailbox_id(entry.name),
                "name": display_name,
                "parentId": parent_id,
                "role": role,
                "sortOrder": _ROLE_SORT_ORDER.get(role, _DEFAULT_SORT_ORDER),
                "isSubscribed": True,
                "myRights": {
                    "mayReadItems": True,
                    "mayAddItems": True,
                    "mayRemoveItems": True,
                    "maySetSeen": True,
                    "maySetKeywords": True,
                    "mayCreateChild": True,
                    "mayRename": role is None,
                    "mayDelete": role is None,
                    "maySubmit": True,
                },
            }
        )
    return mailboxes
