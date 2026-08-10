"""Email/get, Email/query, Email/set, Email/import, Email/changes
(RFC 8621 SS4). Every Email id is a deterministic encoding of
`(mailbox, uidvalidity, uid)` (modseq_state.py) - decoding an id tells us
exactly what to IMAP-fetch, with no lookup table for the common (never-
moved) case. A moved message's old id is kept resolvable via an
in-memory redirect for the rest of this process's uptime - see
id_redirect.py and _apply_email_update's mailboxIds-move handling.

Multi-mailbox Email/set (mailboxIds with >1 entry) is implemented via real
IMAP COPY per the plan's confirmed decision: each mailbox gets its own
physical message with an independent UID, so "the same JMAP Email in two
mailboxes" doesn't survive as one identity across a flag change made
through only one of the two ids - this is a known, accepted gap between
the two protocols' models, not a bug.

Deferred (both scoped out deliberately, not oversights):
- `inMailboxOtherThan` filter condition: would need aggregating a search
  across multiple mailboxes at once, a materially bigger feature than the
  rest of Email/query's filter support - raises `unsupportedFilter`
  (`_translate_condition`) rather than silently ignoring it.
- `Email/queryChanges`: Email/query always reports `canCalculateChanges:
  False`, so a spec-compliant client (confirmed against aerc's source)
  never caches folder contents and never calls queryChanges in the first
  place - the honest signaling already prevents breakage, so implementing
  it hasn't been prioritized.
- `Email/copy`: not implemented at all (see session.py's module docstring
  for why - low priority given this bridge's single-account-per-session
  model).
"""

from __future__ import annotations

import email
import email.policy
import email.utils
from collections import defaultdict
from email.message import EmailMessage
from typing import Any

from jmap_bridge.backends.imap.client import ImapError
from jmap_bridge.backends.imap.email_map import (
    build_jmap_email,
    decode_blob_id,
    extract_blob_part,
    keywords_to_flags,
)
from jmap_bridge.backends.imap.mailbox_map import decode_mailbox_id, encode_mailbox_id
from jmap_bridge.backends.imap.modseq_state import (
    CannotCalculateChanges as StateCannotCalculateChanges,
)
from jmap_bridge.backends.imap.modseq_state import (
    decode_email_id,
    decode_mail_state,
    encode_email_id,
    encode_mail_state,
    verify_no_missing_destroys,
)
from jmap_bridge.context import RequestContext
from jmap_bridge.dispatch import method
from jmap_bridge.errors import (
    CannotCalculateChanges,
    InvalidArguments,
    InvalidProperties,
    MethodError,
    ServerFail,
    UnsupportedFilter,
    UnsupportedSort,
)
from jmap_bridge.state import InvalidStateToken
from jmap_bridge.types.mailbox import _cursors_from_statuses, _list_selectable_mailboxes, _status_map


async def _account_mail_state(conn) -> str:
    entries = await _list_selectable_mailboxes(conn)
    return encode_mail_state(_cursors_from_statuses(await _status_map(conn, entries)))


async def _fetch_one_email(conn, mailbox: str, uidvalidity: int, uid: int, *, report_id: str) -> dict | None:
    """Fetch and build the JMAP Email at one exact physical location, or
    None if it's not there. `report_id` is embedded as the result's `id`
    - which can differ from what this location encodes to, when called
    via a redirect (see _fetch_emails_by_id) for an id that's since moved.
    """
    try:
        status = await conn.select(mailbox, readonly=True)
    except ImapError:
        return None
    if status.uidvalidity != uidvalidity:
        return None
    fetched = await conn.fetch([uid], ["RFC822", "FLAGS", "INTERNALDATE"])
    data = fetched.get(uid)
    if data is None or b"RFC822" not in data:
        return None
    raw_flags = data.get(b"FLAGS", ())
    flags = frozenset(f.decode() if isinstance(f, bytes) else f for f in raw_flags)
    return build_jmap_email(
        raw_message=data[b"RFC822"],
        email_id=report_id,
        mailbox_ids={encode_mailbox_id(mailbox): True},
        flags=flags,
        internaldate=data.get(b"INTERNALDATE"),
        mailbox=mailbox,
        uidvalidity=status.uidvalidity,
        uid=uid,
    )


async def _fetch_emails_by_id(
    ctx: RequestContext, conn, ids: list[str]
) -> tuple[dict[str, dict], list[str]]:
    groups: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    not_found: list[str] = []
    for eid in ids:
        try:
            mailbox, uidvalidity, uid = decode_email_id(eid)
        except ValueError:
            not_found.append(eid)
            continue
        groups[mailbox].append((eid, uidvalidity, uid))

    found: dict[str, dict] = {}
    for mailbox, items in groups.items():
        try:
            status = await conn.select(mailbox, readonly=True)
        except ImapError:
            not_found.extend(i[0] for i in items)
            continue
        valid_items = [i for i in items if i[1] == status.uidvalidity]
        not_found.extend(i[0] for i in items if i[1] != status.uidvalidity)
        if not valid_items:
            continue
        uid_to_id = {i[2]: i[0] for i in valid_items}
        fetched = await conn.fetch([i[2] for i in valid_items], ["RFC822", "FLAGS", "INTERNALDATE"])
        for uid, data in fetched.items():
            eid = uid_to_id.get(uid)
            if eid is None or b"RFC822" not in data:
                continue
            raw_flags = data.get(b"FLAGS", ())
            flags = frozenset(f.decode() if isinstance(f, bytes) else f for f in raw_flags)
            found[eid] = build_jmap_email(
                raw_message=data[b"RFC822"],
                email_id=eid,
                mailbox_ids={encode_mailbox_id(mailbox): True},
                flags=flags,
                internaldate=data.get(b"INTERNALDATE"),
                mailbox=mailbox,
                uidvalidity=status.uidvalidity,
                uid=uid,
            )
        not_found.extend(eid for eid in uid_to_id.values() if eid not in found)

    # Anything still missing might be an id from before a move we still
    # have a redirect on file for (id_redirect.py) - resolve and retry
    # those individually before giving up. This only runs for ids that
    # are actually missing (rare - the vast majority of ids never move),
    # so a fetch-per-id here doesn't cost the common case anything.
    still_missing, not_found = not_found, []
    for eid in still_missing:
        resolved = ctx.id_redirects.resolve(ctx.id_redirect_key, eid)
        email_obj = None
        if resolved != eid:
            try:
                r_mailbox, r_uidvalidity, r_uid = decode_email_id(resolved)
            except ValueError:
                r_mailbox = None
            if r_mailbox is not None:
                email_obj = await _fetch_one_email(
                    conn, r_mailbox, r_uidvalidity, r_uid, report_id=eid
                )
        if email_obj is not None:
            found[eid] = email_obj
        else:
            not_found.append(eid)

    return found, not_found


@method("Email/get")
async def email_get(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    ids = args.get("ids")
    if ids is None:
        raise InvalidArguments("ids is required (full-account enumeration is not supported)")
    properties = args.get("properties")

    try:
        async with ctx.imap() as conn:
            found, not_found = await _fetch_emails_by_id(ctx, conn, ids)
            state = await _account_mail_state(conn)
    except ImapError as exc:
        raise ServerFail(str(exc)) from exc

    result_list = [found[i] for i in ids if i in found]
    if properties is not None:
        prop_set = set(properties) | {"id"}
        result_list = [{k: v for k, v in email.items() if k in prop_set} for email in result_list]

    return {
        "accountId": account_id,
        "state": state,
        "list": result_list,
        "notFound": not_found,
    }


_STANDARD_SEARCH_FLAGS = {
    "\\Seen": "SEEN",
    "\\Flagged": "FLAGGED",
    "\\Answered": "ANSWERED",
    "\\Draft": "DRAFT",
    "\\Deleted": "DELETED",
}

_SORT_PROPERTY_TO_IMAP = {
    "receivedAt": "ARRIVAL",
    "subject": "SUBJECT",
    "size": "SIZE",
    "from": "FROM",
    "to": "TO",
    "cc": "CC",
}

# inMailbox/inMailboxOtherThan aren't real IMAP SEARCH keys - IMAP search
# is inherently scoped to whatever mailbox is SELECTed, so those two
# properties decide *which mailbox to select*, not a search criterion
# (see _find_in_mailbox). They're accepted here (not "unsupported") but
# translate to nothing.
_SUPPORTED_FILTER_KEYS = {
    "inMailbox", "hasKeyword", "notKeyword", "subject", "text", "from", "to", "cc", "bcc",
    "before", "after", "body", "header",
}


def _keyword_search_term(keyword: str, *, negate: bool) -> list[str]:
    flag = keywords_to_flags({keyword: True})[0]
    standard = _STANDARD_SEARCH_FLAGS.get(flag)
    if standard:
        return ["NOT", standard] if negate else [standard]
    return ["UNKEYWORD", flag] if negate else ["KEYWORD", flag]


def _imap_search_date(value: str, field: str) -> str:
    """JMAP UTCDate ("2024-01-15T10:30:00Z") -> IMAP SEARCH date
    ("15-Jan-2024"). IMAP SEARCH BEFORE/SINCE are date-only (RFC 3501
    SS6.4.4) - an inherent protocol limitation, not something we can
    work around, so `before`/`after` filtering has day, not
    second, precision against real IMAP backends.
    """
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidArguments(f"invalid {field} date: {value!r}") from exc
    return dt.strftime("%d-%b-%Y")


def _find_in_mailbox(filter_: dict) -> str | None:
    """Find the first `inMailbox` value anywhere in the filter tree. IMAP
    SEARCH only ever operates against one SELECTed mailbox, so this - not
    a translated search criterion - is what decides which mailbox we
    select before searching. Real clients (confirmed against aerc's
    source) put `inMailbox` on one FilterCondition alongside everything
    else inside a top-level AND FilterOperator, so this only needs to
    descend into AND nodes to find it; a client that put it inside an OR
    or a NOT would be asking for something IMAP fundamentally can't do in
    one command (search isn't scoped per-branch), so we don't chase that.
    """
    if filter_.get("inMailbox"):
        return filter_["inMailbox"]
    if filter_.get("operator") == "AND":
        for cond in filter_.get("conditions") or []:
            found = _find_in_mailbox(cond)
            if found:
                return found
    return None


def _strip_wildcards(value: str) -> str:
    """Some clients (confirmed: Bulwark webmail, `search-utils.ts`'s
    `toWildcardQuery`) append a trailing `*` to each word of a text
    search term, assuming prefix-match full-text-search syntax like the
    server they were built against (Stalwart) supports. IMAP SEARCH's
    TEXT/SUBJECT/etc. do plain substring matching and have no wildcard
    syntax - a literal `*` is just a character to search for, so passing
    "foo*" through unchanged searches for the substring "foo*" and won't
    match "foo" or "foobar" the way the client expects. Strip a trailing
    `*` off each whitespace-separated word instead.
    """
    return " ".join(word[:-1] if word.endswith("*") else word for word in value.split())


def _translate_condition(cond: dict) -> list:
    """Translate one leaf FilterCondition (RFC 8621 SS4.4.1) into IMAP
    SEARCH criteria - multiple properties set on the same condition are
    an implicit AND per spec, which is exactly what IMAP does with
    multiple juxtaposed criteria, so this just concatenates.
    `inMailbox`/`inMailboxOtherThan` are consumed by mailbox selection,
    not translated here (see _find_in_mailbox). Any property not in
    `_SUPPORTED_FILTER_KEYS` (minSize/maxSize/hasAttachment/thread-keyword
    conditions, inMailboxOtherThan) raises UnsupportedFilter - the honest
    response (RFC 8620 SS5.5), not silently ignoring part of the filter.
    """
    unsupported = set(cond) - _SUPPORTED_FILTER_KEYS
    if unsupported:
        raise UnsupportedFilter(f"unsupported filter properties: {sorted(unsupported)}")

    criteria: list = []
    if "hasKeyword" in cond:
        criteria.extend(_keyword_search_term(cond["hasKeyword"], negate=False))
    if "notKeyword" in cond:
        criteria.extend(_keyword_search_term(cond["notKeyword"], negate=True))
    if cond.get("subject"):
        criteria.extend(["SUBJECT", _strip_wildcards(cond["subject"])])
    if cond.get("text"):
        criteria.extend(["TEXT", _strip_wildcards(cond["text"])])
    if cond.get("body"):
        criteria.extend(["BODY", _strip_wildcards(cond["body"])])
    if cond.get("from"):
        criteria.extend(["FROM", _strip_wildcards(cond["from"])])
    if cond.get("to"):
        criteria.extend(["TO", _strip_wildcards(cond["to"])])
    if cond.get("cc"):
        criteria.extend(["CC", _strip_wildcards(cond["cc"])])
    if cond.get("bcc"):
        criteria.extend(["BCC", _strip_wildcards(cond["bcc"])])
    if cond.get("header"):
        header = cond["header"]
        name = header[0]
        value = header[1] if len(header) > 1 else ""
        criteria.extend(["HEADER", name, value])
    if cond.get("before"):
        criteria.extend(["BEFORE", _imap_search_date(cond["before"], "before")])
    if cond.get("after"):
        criteria.extend(["SINCE", _imap_search_date(cond["after"], "after")])
    return criteria


def _translate_operator(operator: str | None, conditions: list[dict]) -> list:
    if operator not in ("AND", "OR", "NOT"):
        raise UnsupportedFilter(f"unsupported filter operator: {operator!r}")
    if not conditions:
        raise InvalidArguments("filter operator requires at least one condition")
    sub_groups = [group for group in (_translate_filter(c) for c in conditions) if group]
    if not sub_groups:
        return []

    if operator == "AND":
        # IMAP's own default (juxtaposed criteria) is already an AND -
        # flatten one level instead of over-nesting.
        result: list = []
        for group in sub_groups:
            result.extend(group)
        return result
    if operator == "NOT":
        # JMAP NOT = none of the conditions match = AND of per-condition
        # negations (De Morgan's); IMAP's NOT only negates one operand.
        result = []
        for group in sub_groups:
            result.extend(["NOT", group])
        return result
    # OR: IMAP's OR (RFC 3501 SS6.4.4) is binary - right-fold for N>2.
    acc = sub_groups[-1]
    for group in reversed(sub_groups[:-1]):
        acc = ["OR", group, acc]
    return acc


def _translate_filter(filter_: dict) -> list:
    if "operator" in filter_ or "conditions" in filter_:
        return _translate_operator(filter_.get("operator"), filter_.get("conditions") or [])
    return _translate_condition(filter_)


def _build_search_criteria(filter_: dict) -> list:
    """Translate a full JMAP Filter (a FilterCondition, or a nested
    FilterOperator tree of them) into IMAP SEARCH criteria, so filtering
    happens server-side instead of requiring every message body to be
    fetched into the bridge first.
    """
    criteria = _translate_filter(filter_)
    return criteria or ["ALL"]


def _build_sort_criteria(sort: list[dict]) -> list[str]:
    criteria: list[str] = []
    for sort_key in sort:
        prop = sort_key.get("property", "receivedAt")
        imap_key = _SORT_PROPERTY_TO_IMAP.get(prop)
        if imap_key is None:
            raise UnsupportedSort(f"unsupported sort property: {prop!r}")
        if not sort_key.get("isAscending", True):
            criteria.append("REVERSE")
        criteria.append(imap_key)
    return criteria


async def _sorted_uids_fallback(conn, uids: list[int], sort: list[dict]) -> list[int]:
    """Used only if the server doesn't support the SORT extension. Fetches
    just the lightweight field each requested sort key needs (INTERNALDATE
    / ENVELOPE / RFC822.SIZE - never a full body) and sorts in Python.
    """
    if not uids:
        return uids
    needed = {sk.get("property", "receivedAt") for sk in sort}
    fetch_items = []
    if "receivedAt" in needed:
        fetch_items.append("INTERNALDATE")
    if "subject" in needed:
        fetch_items.append("ENVELOPE")
    if "size" in needed:
        fetch_items.append("RFC822.SIZE")
    data = await conn.fetch(uids, fetch_items or ["INTERNALDATE"])

    def value_for(uid: int, prop: str):
        row = data.get(uid, {})
        if prop == "receivedAt":
            return row.get(b"INTERNALDATE")
        if prop == "subject":
            envelope = row.get(b"ENVELOPE")
            subj = getattr(envelope, "subject", None) if envelope else None
            return subj.decode("utf-8", "replace") if isinstance(subj, bytes) else subj
        if prop == "size":
            return row.get(b"RFC822.SIZE")
        return None

    sorted_uids = list(uids)
    for sort_key in reversed(sort):
        prop = sort_key.get("property", "receivedAt")
        ascending = sort_key.get("isAscending", True)
        sorted_uids.sort(key=lambda u: (value_for(u, prop) is None, value_for(u, prop)), reverse=not ascending)
    return sorted_uids


@method("Email/query")
async def email_query(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    filter_ = args.get("filter") or {}
    in_mailbox = _find_in_mailbox(filter_)
    if not in_mailbox:
        raise InvalidArguments(
            "filter must include inMailbox somewhere (in the condition itself, or "
            "inside a top-level AND) - account-wide/cross-mailbox search "
            "(inMailboxOtherThan, or no mailbox scope at all) is not supported yet"
        )
    try:
        mailbox_name = decode_mailbox_id(in_mailbox)
    except ValueError as exc:
        raise InvalidArguments(f"invalid inMailbox id: {exc}") from exc

    search_criteria = _build_search_criteria(filter_)
    sort = args.get("sort") or [{"property": "receivedAt", "isAscending": False}]
    sort_criteria = _build_sort_criteria(sort)

    try:
        async with ctx.imap() as conn:
            status = await conn.select(mailbox_name, readonly=True)
            try:
                uids = await conn.sort(sort_criteria, search_criteria)
            except ImapError:
                # Server doesn't support SORT (RFC 5256 is an extension,
                # not universal) - fall back to SEARCH + a lightweight
                # per-key FETCH, never a full body, for the sort.
                uids = await conn.search(search_criteria)
                uids = await _sorted_uids_fallback(conn, uids, sort)
            query_state = await _account_mail_state(conn)
    except ImapError as exc:
        raise ServerFail(str(exc)) from exc

    result_ids = [encode_email_id(mailbox_name, status.uidvalidity, uid) for uid in uids]
    position = max(args.get("position", 0), 0)
    limit = args.get("limit")
    page = result_ids[position : position + limit] if limit is not None else result_ids[position:]

    return {
        "accountId": account_id,
        "queryState": query_state,
        "canCalculateChanges": False,
        "position": position,
        "ids": page,
        "total": len(result_ids),
    }


@method("Email/changes")
async def email_changes(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    """Detects created and updated messages without needing QRESYNC, by
    combining two things CONDSTORE alone already gives us: newly-arrived
    messages are UIDs >= the mailbox's previous UIDNEXT (they couldn't
    have existed at the old state, since UIDs only increase), and updated
    messages come from CONDSTORE's CHANGEDSINCE. What CONDSTORE can't
    give us is *destroyed* messages individually (that needs QRESYNC's
    VANISHED response, which `imapclient` doesn't parse - see
    backends/imap/client.py). So before trusting a diff,
    `verify_no_missing_destroys` checks the message counts reconcile as
    pure creates; only then is reporting `destroyed: []` actually true,
    not just unknown. If they don't reconcile (something was also
    deleted), this falls back to the honest `cannotCalculateChanges`
    rather than a diff that's silently missing destroyed ids.
    """
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    since_state = args.get("sinceState")
    if not since_state:
        raise InvalidArguments("sinceState is required")

    try:
        old_cursors = decode_mail_state(since_state)
    except InvalidStateToken as exc:
        raise CannotCalculateChanges(str(exc)) from exc

    created: list[str] = []
    updated: list[str] = []

    try:
        async with ctx.imap() as conn:
            entries = await _list_selectable_mailboxes(conn)
            new_cursors = _cursors_from_statuses(await _status_map(conn, entries))

            for name in old_cursors:
                if name not in new_cursors:
                    raise StateCannotCalculateChanges(f"mailbox {name!r} no longer exists")

            for name, new_cursor in new_cursors.items():
                old_cursor = old_cursors.get(name)
                if old_cursor is None:
                    # A mailbox that didn't exist at sinceState: its
                    # messages aren't meaningfully "Email changes"
                    # relative to a state that predates the mailbox
                    # entirely - Mailbox/changes already reports the
                    # mailbox itself as created.
                    continue
                if old_cursor.uidvalidity != new_cursor.uidvalidity:
                    raise StateCannotCalculateChanges(f"mailbox {name!r} UIDVALIDITY rotated")
                if old_cursor.highestmodseq == new_cursor.highestmodseq:
                    continue  # nothing changed in this mailbox

                await conn.select(name, readonly=True)

                new_uids: list[int] = []
                if new_cursor.uidnext > old_cursor.uidnext:
                    new_uids = await conn.search(["UID", f"{old_cursor.uidnext}:*"])
                new_uid_set = set(new_uids)

                changed_data = await conn.fetch_changed_since(
                    old_cursor.highestmodseq, data_items=["UID"]
                )
                changed_uids = [uid for uid in changed_data if uid not in new_uid_set]

                verify_no_missing_destroys(old_cursor, new_cursor, created_count=len(new_uids))

                created.extend(encode_email_id(name, new_cursor.uidvalidity, uid) for uid in new_uids)
                updated.extend(
                    encode_email_id(name, new_cursor.uidvalidity, uid) for uid in changed_uids
                )

            new_state = encode_mail_state(new_cursors)
    except StateCannotCalculateChanges as exc:
        raise CannotCalculateChanges(str(exc)) from exc
    except ImapError as exc:
        raise ServerFail(str(exc)) from exc

    return {
        "accountId": account_id,
        "oldState": since_state,
        "newState": new_state,
        "hasMoreChanges": False,
        "created": created,
        "updated": updated,
        "destroyed": [],
    }


def _format_addresses(addresses: list[dict] | None) -> str | None:
    if not addresses:
        return None
    formatted = [
        email.utils.formataddr((addr.get("name") or "", addr["email"]))
        for addr in addresses
        if addr.get("email")
    ]
    return ", ".join(formatted) if formatted else None


async def _find_uid_by_message_id(conn, raw_message: bytes) -> int | None:
    """Fallback for servers without UIDPLUS (RFC 4315): APPEND doesn't
    report the new UID, so search for it by Message-Id in the
    just-selected mailbox. Returns None if the header is missing or the
    search finds nothing/ambiguous results.
    """
    msg = email.message_from_bytes(raw_message, policy=email.policy.default)
    message_id = msg.get("Message-Id")
    if not message_id:
        return None
    uids = await conn.search(["HEADER", "MESSAGE-ID", message_id])
    return uids[-1] if uids else None


async def _append_message_and_build_email(
    conn, raw_message: bytes, target_names: list[str], flags: tuple[str, ...]
) -> dict:
    """Shared by Email/import and Email/set's create path: APPEND
    `raw_message` to the first of `target_names` (COPY into any others -
    see the multi-mailbox note in the module docstring), then re-fetch
    and build the resulting JMAP Email. Raises ServerFail if the new
    message can't be located or re-fetched afterward.
    """
    primary = target_names[0]
    status = await conn.select(primary, readonly=False)
    new_uid = await conn.append(primary, raw_message, flags)
    if new_uid is None:
        status = await conn.select(primary, readonly=True)
        new_uid = await _find_uid_by_message_id(conn, raw_message)
    if new_uid is None:
        raise ServerFail(
            "APPEND succeeded but the new message could not be located "
            "(server lacks UIDPLUS and has no matching Message-Id)"
        )
    for extra in target_names[1:]:
        await conn.copy([new_uid], extra)

    fetched = await conn.fetch([new_uid], ["RFC822", "FLAGS", "INTERNALDATE"])
    data = fetched.get(new_uid)
    if data is None or b"RFC822" not in data:
        raise ServerFail("message was appended but could not be re-fetched")
    raw_flags = data.get(b"FLAGS", ())
    return build_jmap_email(
        raw_message=data[b"RFC822"],
        email_id=encode_email_id(primary, status.uidvalidity, new_uid),
        mailbox_ids={encode_mailbox_id(primary): True},
        flags=frozenset(f.decode() if isinstance(f, bytes) else f for f in raw_flags),
        internaldate=data.get(b"INTERNALDATE"),
        mailbox=primary,
        uidvalidity=status.uidvalidity,
        uid=new_uid,
    )


async def _resolve_attachment_bytes(conn, ctx: RequestContext, blob_id: str) -> tuple[bytes, str] | None:
    """An Email/set create attachment's blobId is either a staged upload
    (blob_cache - the common case, a client POSTs bytes to /upload first)
    or a blobId minted from an existing backend message (e.g. forwarding
    an attachment someone already fetched via Email/get) - reuses the
    already-open `conn` for the latter rather than checking out a second
    pooled connection mid-request.
    """
    staged = ctx.blob_cache.get(blob_id)
    if staged is not None:
        return staged
    try:
        mailbox, uidvalidity, uid, part_index = decode_blob_id(blob_id)
    except ValueError:
        return None
    status = await conn.select(mailbox, readonly=True)
    if status.uidvalidity != uidvalidity:
        return None
    fetched = await conn.fetch([uid], ["RFC822"])
    raw = fetched.get(uid, {}).get(b"RFC822")
    if raw is None:
        return None
    return extract_blob_part(raw, part_index)


def _apply_raw_header_properties(msg: EmailMessage, props: dict) -> None:
    """RFC 8621 SS4.1.5's dynamic `header:Name` / `header:Name:form`
    properties, as used on Email/set create - e.g. Bulwark webmail sets
    `header:Disposition-Notification-To:asText` for MDN requests. Only
    the `asRaw` (bare `header:Name`) and `asText` forms are supported:
    both are treated as a plain unfolded string value set verbatim as the
    header, which is correct for simple unstructured headers (the actual
    use case) but not a full implementation of the other forms
    (asAddresses/asGroupedAddresses/asMessageIds/asDate/asURLs), which
    would need structured JSON->header encoding this doesn't attempt.
    """
    for key, value in props.items():
        if not key.startswith("header:") or not value:
            continue
        rest = key[len("header:") :]
        name, _, form = rest.partition(":")
        if form not in ("", "asRaw", "asText"):
            continue
        if name:
            msg[name] = str(value)


def _build_mime_message(props: dict, resolved_attachments: list[tuple[dict, bytes, str]]) -> bytes:
    """Build an RFC822 message from JMAP Email creation properties
    (RFC 8621 SS4.6): address headers, subject, a text and/or html body
    from bodyValues (referenced by textBody/htmlBody partIds), attachments,
    and `header:Name`/`header:Name:asText` raw headers. Deferred: custom
    Message-Id/Date (we always mint our own), inline `cid`-referenced
    body images beyond a flat attachment list, non-text header forms
    (asAddresses/asGroupedAddresses/asMessageIds/asDate/asURLs).
    """
    msg = EmailMessage(policy=email.policy.SMTP)

    for header, prop in (
        ("From", "from"), ("To", "to"), ("Cc", "cc"), ("Bcc", "bcc"), ("Reply-To", "replyTo"),
    ):
        value = _format_addresses(props.get(prop))
        if value:
            msg[header] = value
    if props.get("subject"):
        msg["Subject"] = props["subject"]
    msg["Date"] = email.utils.formatdate(localtime=False)
    msg["Message-Id"] = email.utils.make_msgid()
    _apply_raw_header_properties(msg, props)

    body_values = props.get("bodyValues") or {}

    def _body_text(body_list: list[dict] | None) -> str | None:
        if not body_list:
            return None
        part_id = body_list[0].get("partId")
        entry = body_values.get(part_id)
        return entry.get("value") if entry else None

    text_content = _body_text(props.get("textBody"))
    html_content = _body_text(props.get("htmlBody"))

    if text_content and html_content:
        msg.set_content(text_content)
        msg.add_alternative(html_content, subtype="html")
    elif html_content:
        msg.set_content(html_content, subtype="html")
    else:
        msg.set_content(text_content or "")

    for attachment_props, data, content_type in resolved_attachments:
        maintype, _, subtype = (content_type or "application/octet-stream").partition("/")
        msg.add_attachment(
            data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment_props.get("name") or "attachment",
        )

    return msg.as_bytes()


async def _create_email(ctx: RequestContext, conn, props: dict) -> dict:
    mailbox_ids = props.get("mailboxIds") or {}
    try:
        target_names = [decode_mailbox_id(m) for m, keep in mailbox_ids.items() if keep]
    except ValueError as exc:
        raise InvalidArguments(f"invalid mailboxIds entry: {exc}", arguments=["mailboxIds"]) from exc
    if not target_names:
        raise InvalidArguments("mailboxIds is required", arguments=["mailboxIds"])

    resolved_attachments = []
    for attachment_props in props.get("attachments") or []:
        blob_id = attachment_props.get("blobId")
        if not blob_id:
            raise InvalidArguments("attachment missing blobId")
        resolved = await _resolve_attachment_bytes(conn, ctx, blob_id)
        if resolved is None:
            raise InvalidArguments(f"unknown or unreadable blobId {blob_id!r}")
        data, content_type = resolved
        resolved_attachments.append((attachment_props, data, attachment_props.get("type") or content_type))

    raw_message = _build_mime_message(props, resolved_attachments)
    flags = tuple(keywords_to_flags(props.get("keywords") or {}))
    return await _append_message_and_build_email(conn, raw_message, target_names, flags)


async def _apply_email_update(ctx: RequestContext, conn, email_id: str, patch: dict) -> None:
    """Apply one Email/set update PatchObject (keywords + mailboxIds) to
    the message identified by `email_id`. Raises InvalidArguments/
    InvalidProperties (MethodError subclasses) or ImapError on failure.
    Factored out of Email/set's update loop so EmailSubmission/set's
    `onSuccessUpdateEmail` (RFC 8621 SS7.2.4) can apply patches "as though
    passed as an update to Email/set in the same request" through the
    identical code path, rather than duplicating the mailboxIds-move logic.
    """
    try:
        mailbox, uidvalidity, uid = decode_email_id(email_id)
    except ValueError as exc:
        raise InvalidArguments("invalid id") from exc

    # If this id moved earlier in this process's uptime, a redirect will
    # be on file (see id_redirect.py) pointing at its current physical
    # location - prefer that over the (now-stale) location the id itself
    # decodes to, so acting on an id from before a move still works.
    resolved = ctx.id_redirects.resolve(ctx.id_redirect_key, email_id)
    if resolved != email_id:
        try:
            mailbox, uidvalidity, uid = decode_email_id(resolved)
        except ValueError:
            pass  # fall through - the direct decode's stale-check below will report it

    status = await conn.select(mailbox, readonly=False)
    if status.uidvalidity != uidvalidity:
        raise InvalidArguments("mailbox UIDVALIDITY changed; id is stale")

    keywords_patch = {
        k[len("keywords/") :]: v for k, v in patch.items() if k.startswith("keywords/")
    }
    if "keywords" in patch:
        await conn.set_flags([uid], keywords_to_flags(patch["keywords"]))
    else:
        for keyword, value in keywords_patch.items():
            flags = keywords_to_flags({keyword: True})
            if value:
                await conn.add_flags([uid], flags)
            else:
                await conn.remove_flags([uid], flags)

    # A patch can replace the whole `mailboxIds` set, or (far more common
    # in practice - this is how aerc implements every move/copy/delete/
    # label operation) patch individual entries via `mailboxIds/<id>: true
    # /null`. Since our model has each Email in exactly one physical
    # mailbox, "current" for patch purposes is just that one mailbox.
    mailbox_id_patches = {
        k[len("mailboxIds/") :]: v for k, v in patch.items() if k.startswith("mailboxIds/")
    }
    new_mailbox_ids = patch.get("mailboxIds")
    if new_mailbox_ids is not None:
        target_names = {decode_mailbox_id(m) for m, keep in new_mailbox_ids.items() if keep}
    elif mailbox_id_patches:
        target_names = {mailbox}
        for mbox_id, keep in mailbox_id_patches.items():
            name = decode_mailbox_id(mbox_id)
            if keep:
                target_names.add(name)
            else:
                target_names.discard(name)
    else:
        target_names = None

    if target_names is not None:
        if not target_names:
            raise InvalidProperties(
                "a message must belong to one or more mailboxes",
                properties=["mailboxIds"],
            )
        copy_destinations = target_names - {mailbox}
        new_locations: dict[str, tuple[int, int]] = {}
        for extra in copy_destinations:
            # A pure IMAP COPY: the original id/mailbox/UID is untouched by
            # adding another mailbox, so its id stays valid and stable. The
            # new copies get their own independent ids, discoverable via a
            # later Email/query - JMAP doesn't require this response to
            # name them.
            copy_result = await conn.copy([uid], extra)
            if copy_result is not None:
                new_locations[extra] = copy_result
        if mailbox not in target_names:
            # The message is leaving its only known physical location,
            # which changes its underlying IMAP UID - this id-encoding
            # scheme has no way to carry the *same* physical identity
            # across that without a locally-stored mapping table. We still
            # perform the move (copied above; removed here): `email_id`
            # would otherwise stop resolving entirely (safe - IMAP UIDs
            # are retired, never reused, so it can never resolve to
            # different, wrong data - just notFound).
            #
            # When there's exactly one destination and the server reported
            # its new (uidvalidity, uid) via COPYUID, record an in-memory
            # redirect (id_redirect.py) instead of leaving it to go
            # permanently stale: a client that cached `email_id` (some
            # real clients, confirmed: Bulwark webmail, assume per RFC
            # 8620 SS5.3 that an Email's id never changes across an
            # update and don't handle a stale id gracefully) keeps working
            # for the rest of this process's uptime. Ambiguous fan-out
            # moves (>1 destination) or a server without UIDPLUS don't get
            # a redirect - same as the pre-redirect-cache behavior.
            if len(copy_destinations) == 1:
                new_location = new_locations.get(next(iter(copy_destinations)))
                if new_location is not None:
                    new_uidvalidity, new_uid = new_location
                    new_id = encode_email_id(next(iter(copy_destinations)), new_uidvalidity, new_uid)
                    ctx.id_redirects.record(ctx.id_redirect_key, email_id, new_id)
            await conn.set_flags([uid], ["\\Deleted"])
            await conn.expunge([uid])


@method("Email/set")
async def email_set(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
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
            for creation_id, props in create.items():
                try:
                    created[creation_id] = await _create_email(ctx, conn, props)
                except (ImapError, ServerFail, InvalidArguments) as exc:
                    body = exc.to_response() if isinstance(exc, MethodError) else ServerFail(str(exc)).to_response()
                    not_created[creation_id] = body
                    continue

            for email_id, patch in update.items():
                try:
                    await _apply_email_update(ctx, conn, email_id, patch)
                except MethodError as exc:
                    not_updated[email_id] = exc.to_response()
                    continue
                except ImapError as exc:
                    not_updated[email_id] = ServerFail(str(exc)).to_response()
                    continue
                updated[email_id] = None

            for email_id in destroy:
                try:
                    mailbox, uidvalidity, uid = decode_email_id(email_id)
                except ValueError:
                    not_destroyed[email_id] = InvalidArguments("invalid id").to_response()
                    continue
                resolved = ctx.id_redirects.resolve(ctx.id_redirect_key, email_id)
                if resolved != email_id:
                    try:
                        mailbox, uidvalidity, uid = decode_email_id(resolved)
                    except ValueError:
                        pass
                try:
                    status = await conn.select(mailbox, readonly=False)
                    if status.uidvalidity != uidvalidity:
                        not_destroyed[email_id] = InvalidArguments(
                            "mailbox UIDVALIDITY changed; id is stale"
                        ).to_response()
                        continue
                    await conn.set_flags([uid], ["\\Deleted"])
                    await conn.expunge([uid])
                except ImapError as exc:
                    not_destroyed[email_id] = ServerFail(str(exc)).to_response()
                    continue
                destroyed.append(email_id)

            new_state = await _account_mail_state(conn)
    except ImapError as exc:
        raise ServerFail(str(exc)) from exc

    return {
        "accountId": account_id,
        "oldState": None,
        "newState": new_state,
        "created": created,
        "notCreated": not_created,
        "updated": updated,
        "notUpdated": not_updated,
        "destroyed": destroyed,
        "notDestroyed": not_destroyed,
    }


@method("Email/import")
async def email_import(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    emails_arg = args.get("emails") or {}
    if not emails_arg:
        raise InvalidArguments("emails is required")

    created: dict[str, dict] = {}
    not_created: dict[str, dict] = {}

    try:
        async with ctx.imap() as conn:
            for creation_id, props in emails_arg.items():
                blob_id = props.get("blobId")
                mailbox_ids = props.get("mailboxIds") or {}
                target_names = [
                    decode_mailbox_id(m) for m, keep in mailbox_ids.items() if keep
                ]
                if not blob_id or not target_names:
                    not_created[creation_id] = InvalidArguments(
                        "blobId and mailboxIds are required", arguments=["blobId", "mailboxIds"]
                    ).to_response()
                    continue

                blob = ctx.blob_cache.get(blob_id)
                if blob is None:
                    not_created[creation_id] = InvalidArguments(
                        f"unknown blobId {blob_id!r} (expired or never uploaded)"
                    ).to_response()
                    continue
                raw_message, _content_type = blob
                flags = tuple(keywords_to_flags(props.get("keywords") or {}))

                try:
                    created[creation_id] = await _append_message_and_build_email(
                        conn, raw_message, target_names, flags
                    )
                except (ImapError, ServerFail) as exc:
                    body = exc.to_response() if isinstance(exc, MethodError) else ServerFail(str(exc)).to_response()
                    not_created[creation_id] = body
                    continue

            new_state = await _account_mail_state(conn)
    except ImapError as exc:
        raise ServerFail(str(exc)) from exc

    return {
        "accountId": account_id,
        "oldState": None,
        "newState": new_state,
        "created": created,
        "notCreated": not_created,
    }
