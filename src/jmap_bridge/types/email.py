"""Email/get, Email/query, Email/set, Email/import, Email/changes
(RFC 8621 SS4). Every Email id is a deterministic encoding of
`(mailbox, uidvalidity, uid)` (modseq_state.py) - decoding an id tells us
exactly what to IMAP-fetch, with no lookup table.

Multi-mailbox Email/set (mailboxIds with >1 entry) is implemented via real
IMAP COPY per the plan's confirmed decision: each mailbox gets its own
physical message with an independent UID, so "the same JMAP Email in two
mailboxes" doesn't survive as one identity across a flag change made
through only one of the two ids - this is a known, accepted gap between
the two protocols' models, not a bug.
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
from jmap_bridge.backends.imap.modseq_state import decode_email_id, encode_email_id, encode_mail_state
from jmap_bridge.context import RequestContext
from jmap_bridge.dispatch import method
from jmap_bridge.errors import (
    CannotCalculateChanges,
    InvalidArguments,
    MethodError,
    ServerFail,
    UnsupportedFilter,
    UnsupportedSort,
)
from jmap_bridge.types.mailbox import _cursors_from_statuses, _list_selectable_mailboxes, _status_map


async def _account_mail_state(conn) -> str:
    entries = await _list_selectable_mailboxes(conn)
    return encode_mail_state(_cursors_from_statuses(await _status_map(conn, entries)))


async def _fetch_emails_by_id(conn, ids: list[str]) -> tuple[dict[str, dict], list[str]]:
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
            found, not_found = await _fetch_emails_by_id(conn, ids)
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
}

_SUPPORTED_FILTER_KEYS = {
    "inMailbox", "hasKeyword", "notKeyword", "subject", "text", "from", "to", "cc", "bcc",
    "before", "after",
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


def _build_search_criteria(filter_: dict) -> list:
    """Translate a flat JMAP FilterCondition into IMAP SEARCH criteria, so
    filtering happens server-side instead of requiring every message body
    to be fetched into the bridge first. `inMailbox` is handled by the
    caller's SELECT, not here.

    Nested FilterOperator (AND/OR/NOT of sub-filters) and any
    FilterCondition property not in `_SUPPORTED_FILTER_KEYS` (e.g.
    minSize/maxSize/hasAttachment/thread-keyword conditions) are not yet
    implemented - raising UnsupportedFilter is the honest response
    (RFC 8620 SS5.5), not silently ignoring part of the client's filter.
    """
    if "conditions" in filter_ or "operator" in filter_:
        raise UnsupportedFilter("AND/OR/NOT filter operators are not supported yet")
    unsupported = set(filter_) - _SUPPORTED_FILTER_KEYS
    if unsupported:
        raise UnsupportedFilter(f"unsupported filter properties: {sorted(unsupported)}")

    criteria: list = []
    if "hasKeyword" in filter_:
        criteria.extend(_keyword_search_term(filter_["hasKeyword"], negate=False))
    if "notKeyword" in filter_:
        criteria.extend(_keyword_search_term(filter_["notKeyword"], negate=True))
    if filter_.get("subject"):
        criteria.extend(["SUBJECT", filter_["subject"]])
    if filter_.get("text"):
        criteria.extend(["TEXT", filter_["text"]])
    if filter_.get("from"):
        criteria.extend(["FROM", filter_["from"]])
    if filter_.get("to"):
        criteria.extend(["TO", filter_["to"]])
    if filter_.get("cc"):
        criteria.extend(["CC", filter_["cc"]])
    if filter_.get("bcc"):
        criteria.extend(["BCC", filter_["bcc"]])
    if filter_.get("before"):
        criteria.extend(["BEFORE", _imap_search_date(filter_["before"], "before")])
    if filter_.get("after"):
        criteria.extend(["SINCE", _imap_search_date(filter_["after"], "after")])
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
    if "conditions" in filter_ or "operator" in filter_:
        # A FilterOperator (AND/OR/NOT of sub-filters) is a fundamentally
        # different shape than FilterCondition - check for it before
        # assuming flat filter fields like inMailbox exist at all, so the
        # error names the real gap (operators unsupported) rather than a
        # confusing "inMailbox missing" for a filter that was never
        # meant to have one at the top level.
        raise UnsupportedFilter("AND/OR/NOT filter operators are not supported yet")
    in_mailbox = filter_.get("inMailbox")
    if not in_mailbox:
        raise InvalidArguments(
            "filter.inMailbox is required (account-wide search is not supported yet)"
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
    """Always returns `cannotCalculateChanges` (RFC 8620 SS5.2), honestly:
    detecting *destroyed* messages requires QRESYNC's VANISHED response,
    which `imapclient` doesn't parse (see backends/imap/client.py) - rather
    than report updates while silently omitting some destroyed ids (a
    correctness bug worse than the honest failure), this is unimplemented
    until VANISHED support is added and verified against a real server.
    """
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    if not args.get("sinceState"):
        raise InvalidArguments("sinceState is required")
    raise CannotCalculateChanges(
        "Email/changes destroyed-detection requires QRESYNC VANISHED support, "
        "not yet implemented - client should fall back to a full resync"
    )


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


def _build_mime_message(props: dict, resolved_attachments: list[tuple[dict, bytes, str]]) -> bytes:
    """Build an RFC822 message from JMAP Email creation properties
    (RFC 8621 SS4.6): address headers, subject, a text and/or html body
    from bodyValues (referenced by textBody/htmlBody partIds), and
    attachments. Deferred: client-supplied `headers`/`header:X`, custom
    Message-Id/Date (we always mint our own), inline `cid`-referenced
    body images beyond a flat attachment list.
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
                    mailbox, uidvalidity, uid = decode_email_id(email_id)
                except ValueError:
                    not_updated[email_id] = InvalidArguments("invalid id").to_response()
                    continue
                try:
                    status = await conn.select(mailbox, readonly=False)
                    if status.uidvalidity != uidvalidity:
                        not_updated[email_id] = InvalidArguments(
                            "mailbox UIDVALIDITY changed; id is stale"
                        ).to_response()
                        continue

                    keywords_patch = {
                        k[len("keywords/") :]: v
                        for k, v in patch.items()
                        if k.startswith("keywords/")
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

                    new_mailbox_ids = patch.get("mailboxIds")
                    if new_mailbox_ids is not None:
                        target_names = {
                            decode_mailbox_id(m) for m, keep in new_mailbox_ids.items() if keep
                        }
                        if mailbox in target_names:
                            # Adding mailboxes (a superset of the current one) is a
                            # pure IMAP COPY: the original id/mailbox/UID is
                            # untouched, so its id stays valid and stable. The new
                            # copies get their own independent ids, discoverable via
                            # a later Email/query - JMAP doesn't require this
                            # response to name them.
                            for extra in target_names - {mailbox}:
                                await conn.copy([uid], extra)
                        else:
                            # Removing the message's only mailbox (a "move") would
                            # change its physical IMAP UID, and this id-encoding
                            # scheme (SS3a of the plan) has no stable identity to
                            # carry across that - JMAP requires an object's id to
                            # stay fixed across an update, which we cannot honor
                            # here without a local id-mapping table (the thing this
                            # design deliberately avoids). Fail explicitly rather
                            # than silently returning a stale or wrong id.
                            not_updated[email_id] = InvalidArguments(
                                "moving an Email to a different sole mailbox is not "
                                "yet supported (would require a locally-stored id "
                                "mapping); add the destination mailbox alongside "
                                "the current one instead, or destroy + re-import"
                            ).to_response()
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
