"""Thread/get, Thread/changes (RFC 8621 SS3).

Threads are not stored anywhere: a message's threadId is derived purely
from its References/In-Reply-To/Message-Id headers (see
`email_map.derive_thread_id_from_headers`) and is fully reversible (see
`decode_thread_id`), so answering Thread/get means decoding each
requested id back to the original header value it was built from and
running a targeted IMAP SEARCH per mailbox for messages that could
reference it - not scanning every message in the account. Confirmed live
this was the real fix needed: the previous full-account scan fetched
~5000 messages' headers for one call on a real, mail-heavy account, the
dominant cost even after scoping the per-message fetch to just the three
needed headers (`_THREAD_FETCH_ITEM`, still used here for the same
reason - full RFC822.HEADER carries real messages' often-bulky
spam-filter headers along for nothing).

HEADER SEARCH does substring matching (RFC 3501), so it can only narrow
candidates, never confirm them - every candidate's *actual* threadId is
still derived from its own headers and checked before being counted, so
a coincidental substring match can't produce a wrong grouping.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from jmap_bridge.backends.imap.client import ImapError
from jmap_bridge.backends.imap.email_map import decode_thread_id, derive_thread_id_from_headers
from jmap_bridge.backends.imap.modseq_state import encode_email_id
from jmap_bridge.context import RequestContext
from jmap_bridge.dispatch import method
from jmap_bridge.errors import CannotCalculateChanges, InvalidArguments, ServerFail
from jmap_bridge.types.email import _account_mail_state
from jmap_bridge.types.mailbox import _list_selectable_mailboxes

_THREAD_HEADER_FIELDS = "MESSAGE-ID REFERENCES IN-REPLY-TO"
_THREAD_FETCH_ITEM = f"BODY.PEEK[HEADER.FIELDS ({_THREAD_HEADER_FIELDS})]"
_THREAD_FETCH_KEY = f"BODY[HEADER.FIELDS ({_THREAD_HEADER_FIELDS})]".encode()


def _build_or_criteria(basis_values: list[str]) -> list:
    """Nested-OR IMAP SEARCH criteria matching any message whose
    Message-Id, References, or In-Reply-To header contains any of
    `basis_values` - confirmed live against Dovecot. IMAP's OR operator
    is strictly binary (RFC 3501), so N clauses need N-1 nested ORs.
    """
    clauses = []
    for value in basis_values:
        clauses.append(["HEADER", "Message-Id", value])
        clauses.append(["HEADER", "References", value])
        clauses.append(["HEADER", "In-Reply-To", value])
    criteria = clauses[0]
    for clause in clauses[1:]:
        criteria = ["OR"] + criteria + clause
    return criteria


async def _resolve_header_based_threads(conn, basis_values: list[str]) -> dict[str, list[str]]:
    """Targeted search across every selectable mailbox for messages that
    could belong to any thread rooted at one of `basis_values`, grouped
    by each match's own actually-derived threadId (which, for a genuine
    match, is always exactly the threadId `basis_values` was decoded
    from - a coincidental HEADER substring match would derive some other
    threadId instead and simply not be requested by the caller).
    """
    entries = await _list_selectable_mailboxes(conn)
    emails_by_thread: dict[str, list[str]] = defaultdict(list)
    criteria = _build_or_criteria(basis_values)
    for entry in entries:
        try:
            status = await conn.select(entry.name, readonly=True)
            uids = await conn.search(criteria)
        except ImapError:
            continue
        if not uids:
            continue
        fetched = await conn.fetch(uids, [_THREAD_FETCH_ITEM])
        for uid, data in fetched.items():
            headers = data.get(_THREAD_FETCH_KEY)
            if headers is None:
                continue
            email_id = encode_email_id(entry.name, status.uidvalidity, uid)
            thread_id = derive_thread_id_from_headers(
                headers, fallback=f"{entry.name}:{status.uidvalidity}:{uid}"
            )
            emails_by_thread[thread_id].append(email_id)
    return emails_by_thread


async def _resolve_location_thread(
    conn, mailbox: str, uidvalidity: int, uid: int, expected_thread_id: str
) -> list[str] | None:
    """A "TL"-kind threadId (see decode_thread_id) names exactly one
    message directly - no search needed, just confirm it's still there
    (same UIDVALIDITY) and still derives to the id that was asked for.
    """
    try:
        status = await conn.select(mailbox, readonly=True)
    except ImapError:
        return None
    if status.uidvalidity != uidvalidity:
        return None
    fetched = await conn.fetch([uid], [_THREAD_FETCH_ITEM])
    data = fetched.get(uid)
    if data is None:
        return None
    headers = data.get(_THREAD_FETCH_KEY)
    if headers is None:
        return None
    actual_id = derive_thread_id_from_headers(headers, fallback=f"{mailbox}:{uidvalidity}:{uid}")
    if actual_id != expected_thread_id:
        return None
    return [encode_email_id(mailbox, uidvalidity, uid)]


@method("Thread/get")
async def thread_get(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    ids = args.get("ids")
    if ids is None:
        raise InvalidArguments("ids is required")

    header_basis: dict[str, str] = {}
    location_basis: dict[str, tuple[str, int, int]] = {}
    for thread_id in ids:
        decoded = decode_thread_id(thread_id)
        if decoded is None:
            continue
        kind, basis = decoded
        if kind == "H":
            header_basis[thread_id] = basis
        else:
            try:
                mailbox, uidvalidity_s, uid_s = basis.split(":", 2)
                location_basis[thread_id] = (mailbox, int(uidvalidity_s), int(uid_s))
            except ValueError:
                continue

    found: dict[str, list[str]] = {}

    try:
        async with ctx.imap() as conn:
            if header_basis:
                by_thread = await _resolve_header_based_threads(
                    conn, list(set(header_basis.values()))
                )
                for thread_id in header_basis:
                    if thread_id in by_thread:
                        found[thread_id] = by_thread[thread_id]

            for thread_id, (mailbox, uidvalidity, uid) in location_basis.items():
                email_ids = await _resolve_location_thread(
                    conn, mailbox, uidvalidity, uid, thread_id
                )
                if email_ids is not None:
                    found[thread_id] = email_ids

            state = await _account_mail_state(ctx, conn)
    except ImapError as exc:
        raise ServerFail(str(exc)) from exc

    result_list = [{"id": tid, "emailIds": found[tid]} for tid in ids if tid in found]
    not_found = [tid for tid in ids if tid not in found]

    return {
        "accountId": account_id,
        "state": state,
        "list": result_list,
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
