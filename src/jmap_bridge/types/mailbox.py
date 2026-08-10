"""Mailbox/get, Mailbox/query, Mailbox/set, Mailbox/changes (RFC 8621 SS2).

Every handler here opens a pooled IMAP connection (`ctx.imap()`), lists
mailboxes live, and computes JMAP state from a fresh STATUS sweep - there
is no local mailbox cache. See modseq_state.py for the state-vector design.
"""

from __future__ import annotations

from typing import Any

from jmap_bridge.backends.imap.client import ImapError, MailboxStatus
from jmap_bridge.backends.imap.mailbox_map import (
    ImapMailboxEntry,
    decode_mailbox_id,
    encode_mailbox_id,
    imap_list_to_jmap_mailboxes,
)
from jmap_bridge.backends.imap.modseq_state import (
    CannotCalculateChanges as StateCannotCalculateChanges,
)
from jmap_bridge.backends.imap.modseq_state import (
    MailboxCursor,
    decode_mail_state,
    diff_mailbox_state,
    encode_mail_state,
)
from jmap_bridge.context import RequestContext
from jmap_bridge.dispatch import method
from jmap_bridge.errors import CannotCalculateChanges, InvalidArguments, ServerFail
from jmap_bridge.state import InvalidStateToken


async def _list_selectable_mailboxes(conn) -> list[ImapMailboxEntry]:
    raw = await conn.list_mailboxes()
    entries = [ImapMailboxEntry(flags=f, delimiter=d, name=n) for f, d, n in raw]
    return [e for e in entries if not any(fl.upper() == "\\NOSELECT" for fl in e.flags)]


async def _status_map(conn, entries: list[ImapMailboxEntry]) -> dict[str, MailboxStatus]:
    """Authoritative per-mailbox cursor via SELECT, not STATUS.

    Confirmed by live testing against Dovecot: STATUS on a mailbox that
    isn't the connection's currently-selected one can be served from
    Dovecot's `mailbox_list_index` cache, which does not update
    synchronously with writes from *other* connections - a connection
    that only ever issues LIST+STATUS could see a frozen snapshot of
    UIDVALIDITY/HIGHESTMODSEQ/MESSAGES indefinitely, exactly the kind of
    silent staleness the whole state-vector design (modseq_state.py) is
    built to avoid. SELECT always opens a live, authoritative view, so it
    doesn't have this failure mode - confirmed fresh in the same tests.
    The cost is identical (one round trip per mailbox either way); the
    only thing SELECT can't give us is an unseen *count* (see
    `_status_and_unseen_map`, used only where that's actually needed).
    """
    statuses = {}
    for entry in entries:
        statuses[entry.name] = await conn.select(entry.name, readonly=True)
    return statuses


async def _status_and_unseen_map(
    conn, entries: list[ImapMailboxEntry]
) -> tuple[dict[str, MailboxStatus], dict[str, int]]:
    """Like `_status_map`, but also returns a genuinely live unread count
    per mailbox - via SEARCH UNSEEN on the connection immediately after
    SELECTing it, not STATUS UNSEEN.

    This used to be STATUS-based and documented as an acceptable
    "occasionally stale" trade-off; live testing showed the staleness
    isn't occasional; it's the identical caching failure mode `_status_map`
    was already rewritten to avoid (STATUS on a mailbox not authoritatively
    synced by this connection can be served from a cache that doesn't see
    other connections' writes). SEARCH on a connection immediately after
    its own SELECT is guaranteed fresh the same way SELECT's own response
    fields are - confirmed by the same live testing.
    """
    statuses = {}
    unseen_counts = {}
    for entry in entries:
        statuses[entry.name] = await conn.select(entry.name, readonly=True)
        unseen_counts[entry.name] = len(await conn.search("UNSEEN"))
    return statuses, unseen_counts


def _cursors_from_statuses(statuses: dict[str, MailboxStatus]) -> dict[str, MailboxCursor]:
    return {
        name: MailboxCursor(
            uidvalidity=s.uidvalidity, highestmodseq=s.highestmodseq or 0,
            uidnext=s.uidnext, exists=s.exists,
        )
        for name, s in statuses.items()
    }


@method("Mailbox/get")
async def mailbox_get(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    ids = args.get("ids")
    properties = args.get("properties")

    try:
        async with ctx.imap() as conn:
            entries = await _list_selectable_mailboxes(conn)
            statuses, unseen_counts = await _status_and_unseen_map(conn, entries)
    except ImapError as exc:
        raise ServerFail(str(exc)) from exc

    all_mailboxes = imap_list_to_jmap_mailboxes(entries)
    name_by_id = {encode_mailbox_id(e.name): e.name for e in entries}
    for mailbox in all_mailboxes:
        name = name_by_id[mailbox["id"]]
        status = statuses[name]
        mailbox["totalEmails"] = status.exists
        mailbox["unreadEmails"] = unseen_counts[name]
        # Thread counts require grouping every message in the mailbox by
        # Message-Id/References chain - too expensive to compute on every
        # Mailbox/get. Deferred past MVP; report unknown rather than
        # faking a value equal to the email counts.
        mailbox["totalThreads"] = None
        mailbox["unreadThreads"] = None
    state = encode_mail_state(_cursors_from_statuses(statuses))

    by_id = {m["id"]: m for m in all_mailboxes}
    if ids is None:
        selected = list(all_mailboxes)
        not_found: list[str] = []
    else:
        selected = [by_id[i] for i in ids if i in by_id]
        not_found = []
        for i in ids:
            if i in by_id:
                continue
            # Might be an id from before a rename we still have a
            # redirect on file for (id_redirect.py) - resolve and retry,
            # reporting the object back under the *originally requested*
            # id so a client that cached it never sees it change.
            resolved = ctx.id_redirects.resolve(ctx.id_redirect_key, i)
            if resolved in by_id:
                selected.append({**by_id[resolved], "id": i})
            else:
                not_found.append(i)

    if properties is not None:
        selected = [
            {"id": m["id"], **{p: m[p] for p in properties if p in m}} for m in selected
        ]

    return {"accountId": account_id, "state": state, "list": selected, "notFound": not_found}


@method("Mailbox/changes")
async def mailbox_changes(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    since_state = args.get("sinceState")
    if not since_state:
        raise InvalidArguments("sinceState is required")

    try:
        old_cursors = decode_mail_state(since_state)
    except InvalidStateToken as exc:
        raise CannotCalculateChanges(str(exc)) from exc

    try:
        async with ctx.imap() as conn:
            entries = await _list_selectable_mailboxes(conn)
            new_cursors = _cursors_from_statuses(await _status_map(conn, entries))
    except ImapError as exc:
        raise ServerFail(str(exc)) from exc

    try:
        diff = diff_mailbox_state(old_cursors, new_cursors)
    except StateCannotCalculateChanges as exc:
        raise CannotCalculateChanges(str(exc)) from exc

    return {
        "accountId": account_id,
        "oldState": since_state,
        "newState": encode_mail_state(new_cursors),
        "hasMoreChanges": False,
        "created": [encode_mailbox_id(n) for n in diff.created],
        "updated": [encode_mailbox_id(n) for n in diff.updated],
        "destroyed": [encode_mailbox_id(n) for n in diff.destroyed],
    }


@method("Mailbox/query")
async def mailbox_query(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    filter_ = args.get("filter") or {}

    try:
        async with ctx.imap() as conn:
            entries = await _list_selectable_mailboxes(conn)
            cursors = _cursors_from_statuses(await _status_map(conn, entries))
    except ImapError as exc:
        raise ServerFail(str(exc)) from exc

    mailboxes = imap_list_to_jmap_mailboxes(entries)

    parent_id = filter_.get("parentId", "__unset__")
    if parent_id != "__unset__":
        mailboxes = [m for m in mailboxes if m["parentId"] == parent_id]
    if "role" in filter_:
        mailboxes = [m for m in mailboxes if m["role"] == filter_["role"]]

    ids = [m["id"] for m in mailboxes]
    return {
        "accountId": account_id,
        "queryState": encode_mail_state(cursors),
        "canCalculateChanges": False,
        "position": 0,
        "ids": ids,
        "total": len(ids),
    }


@method("Mailbox/set")
async def mailbox_set(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    create = args.get("create") or {}
    update = args.get("update") or {}
    destroy = args.get("destroy") or []

    created: dict[str, dict] = {}
    not_created: dict[str, dict] = {}
    updated: dict[str, dict | None] = {}
    not_updated: dict[str, dict] = {}
    destroyed: list[str] = []
    not_destroyed: dict[str, dict] = {}

    try:
        async with ctx.imap() as conn:
            # Real IMAP servers can use any hierarchy delimiter (RFC 3501
            # SS7.2.2) - Dovecot and others commonly use "." rather than
            # "/". Look up the server's actual delimiter (any existing
            # entry's is representative; a server has one delimiter
            # tree-wide) instead of assuming "/", which would build a
            # malformed path on a differently-delimited server.
            entries = await _list_selectable_mailboxes(conn)
            entry_by_name = {e.name: e for e in entries}
            server_delimiter = entries[0].delimiter if entries else "/"

            for creation_id, props in create.items():
                name = props.get("name")
                if not name:
                    not_created[creation_id] = InvalidArguments(
                        "name is required", arguments=["name"]
                    ).to_response()
                    continue
                parent_id = props.get("parentId")
                full_name = name
                if parent_id:
                    try:
                        parent_name = decode_mailbox_id(parent_id)
                    except ValueError:
                        not_created[creation_id] = InvalidArguments(
                            "invalid parentId", arguments=["parentId"]
                        ).to_response()
                        continue
                    delimiter = entry_by_name[parent_name].delimiter if parent_name in entry_by_name else server_delimiter
                    full_name = f"{parent_name}{delimiter}{name}"
                try:
                    await conn.create_folder(full_name)
                except ImapError as exc:
                    not_created[creation_id] = ServerFail(str(exc)).to_response()
                    continue
                created[creation_id] = {"id": encode_mailbox_id(full_name)}
                entry_by_name[full_name] = ImapMailboxEntry(
                    flags=frozenset(), delimiter=server_delimiter, name=full_name
                )

            for mailbox_id, props in update.items():
                # Might be an id from before an earlier rename we still
                # have a redirect on file for (id_redirect.py) - resolve
                # to the mailbox's current id before decoding.
                resolved_id = ctx.id_redirects.resolve(ctx.id_redirect_key, mailbox_id)
                try:
                    old_name = decode_mailbox_id(resolved_id)
                except ValueError:
                    not_updated[mailbox_id] = InvalidArguments("invalid id").to_response()
                    continue
                new_display_name = props.get("name")
                if new_display_name:
                    delimiter = entry_by_name[old_name].delimiter if old_name in entry_by_name else server_delimiter
                    segments = old_name.split(delimiter)
                    segments[-1] = new_display_name
                    new_name = delimiter.join(segments)
                    try:
                        await conn.rename_folder(old_name, new_name)
                    except ImapError as exc:
                        not_updated[mailbox_id] = ServerFail(str(exc)).to_response()
                        continue
                    # RFC 8620 SS5.3: `updated` must be keyed by the id the
                    # client passed in the update, not a newly-derived one.
                    # Renaming the IMAP folder does change what this
                    # id-encoding scheme produces for it going forward, so
                    # record a redirect (see id_redirect.py) rather than
                    # letting `mailbox_id` go permanently stale - a real
                    # client (confirmed: Bulwark webmail) assumes per spec
                    # that a Mailbox's id never changes across a rename.
                    ctx.id_redirects.record(
                        ctx.id_redirect_key, mailbox_id, encode_mailbox_id(new_name)
                    )
                    updated[mailbox_id] = None
                else:
                    updated[mailbox_id] = None

            for mailbox_id in destroy:
                resolved_id = ctx.id_redirects.resolve(ctx.id_redirect_key, mailbox_id)
                try:
                    name = decode_mailbox_id(resolved_id)
                except ValueError:
                    not_destroyed[mailbox_id] = InvalidArguments("invalid id").to_response()
                    continue
                try:
                    await conn.delete_folder(name)
                except ImapError as exc:
                    not_destroyed[mailbox_id] = ServerFail(str(exc)).to_response()
                    continue
                destroyed.append(mailbox_id)

            entries = await _list_selectable_mailboxes(conn)
            cursors = _cursors_from_statuses(await _status_map(conn, entries))
    except ImapError as exc:
        raise ServerFail(str(exc)) from exc

    return {
        "accountId": account_id,
        "oldState": None,
        "newState": encode_mail_state(cursors),
        "created": created,
        "updated": updated,
        "destroyed": destroyed,
        "notCreated": not_created,
        "notUpdated": not_updated,
        "notDestroyed": not_destroyed,
    }
