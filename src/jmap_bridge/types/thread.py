"""Thread/get, Thread/changes (RFC 8621 SS3.

Threads are not stored anywhere: a message's threadId is derived purely
from its References/In-Reply-To/Message-Id headers (see
`email_map.derive_thread_id_from_headers`), so answering Thread/get means
scanning every selectable mailbox's headers and grouping by that derived
id. This is O(total messages in the account) per call - acceptable for
MVP-sized mailboxes, but a real scaling limitation worth revisiting (e.g.
restricting the scan or building a cheap index) before this sees a large
account.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from jmap_bridge.backends.imap.client import ImapError
from jmap_bridge.backends.imap.email_map import derive_thread_id_from_headers
from jmap_bridge.backends.imap.modseq_state import (
    MailboxCursor,
    encode_email_id,
    encode_mail_state,
)
from jmap_bridge.context import RequestContext
from jmap_bridge.dispatch import method
from jmap_bridge.errors import CannotCalculateChanges, InvalidArguments, ServerFail
from jmap_bridge.types.mailbox import _list_selectable_mailboxes


async def _scan_threads(conn) -> tuple[dict[str, list[str]], dict[str, MailboxCursor]]:
    entries = await _list_selectable_mailboxes(conn)
    emails_by_thread: dict[str, list[str]] = defaultdict(list)
    cursors: dict[str, MailboxCursor] = {}
    for entry in entries:
        status = await conn.select(entry.name, readonly=True)
        cursors[entry.name] = MailboxCursor(
            uidvalidity=status.uidvalidity, highestmodseq=status.highestmodseq or 0,
            uidnext=status.uidnext, exists=status.exists,
        )
        uids = await conn.search("ALL")
        if not uids:
            continue
        fetched = await conn.fetch(uids, ["RFC822.HEADER"])
        for uid, data in fetched.items():
            headers = data.get(b"RFC822.HEADER")
            if headers is None:
                continue
            email_id = encode_email_id(entry.name, status.uidvalidity, uid)
            thread_id = derive_thread_id_from_headers(
                headers, fallback=f"{entry.name}:{status.uidvalidity}:{uid}"
            )
            emails_by_thread[thread_id].append(email_id)
    return emails_by_thread, cursors


@method("Thread/get")
async def thread_get(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    ids = args.get("ids")
    if ids is None:
        raise InvalidArguments("ids is required")

    try:
        async with ctx.imap() as conn:
            emails_by_thread, cursors = await _scan_threads(conn)
    except ImapError as exc:
        raise ServerFail(str(exc)) from exc

    found = []
    not_found = []
    for thread_id in ids:
        if thread_id in emails_by_thread:
            found.append({"id": thread_id, "emailIds": emails_by_thread[thread_id]})
        else:
            not_found.append(thread_id)

    return {
        "accountId": account_id,
        "state": encode_mail_state(cursors),
        "list": found,
        "notFound": not_found,
    }


@method("Thread/changes")
async def thread_changes(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    """A thread's membership changes whenever the underlying Email set
    changes, so this inherits Email/changes' limitation exactly (see
    types/email.py: destroyed-detection needs QRESYNC VANISHED, not yet
    implemented) - always cannotCalculateChanges for now.
    """
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    if not args.get("sinceState"):
        raise InvalidArguments("sinceState is required")
    raise CannotCalculateChanges(
        "Thread/changes depends on Email/changes, which requires QRESYNC VANISHED "
        "support not yet implemented - client should fall back to a full resync"
    )
