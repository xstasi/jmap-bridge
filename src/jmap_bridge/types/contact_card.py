"""ContactCard/get, ContactCard/query, ContactCard/set, ContactCard/changes
(RFC 9610 SS3) - non-group subset (see card_map.py's module docstring;
group support - `kind:"group"` + `members` - added in a later pass, per
the Phase 3 plan).

Every ContactCard id is a deterministic encoding of
`(addressbook_href, card_href)` (card_map.py) - decoding tells us exactly
what to GET/PUT/DELETE, with no lookup table for the common (never-moved)
case. A moved card (`addressBookIds` changed) reuses id_redirect.py, the
same mechanism as CalendarEvent's `calendarIds` move.

`ContactCard/changes` always raises `cannotCalculateChanges`, mirroring
`CalendarEvent/changes`'s stub exactly, for the identical reason (RFC
6578 sync-collection can't distinguish created/updated at the
member-object level) - confirmed safe since Bulwark webmail never calls
it.

Deferred (not silently dropped - see the plan for the full list):
`ContactCard/copy`, `ContactCard/parse` (Bulwark does vCard import/export
100% client-side, never calls this), `ContactCard/query` filter
properties beyond `inAddressBook` (required, mirrors `inCalendar`) and
`text` (fetch-then-locally-filter, CardDAV has no standard full-text
search - free given the batch-fetch already needed for the addressbook).
"""

from __future__ import annotations

from typing import Any

from jmap_bridge.backends.carddav.addressbook_map import decode_addressbook_id, encode_addressbook_id
from jmap_bridge.backends.carddav.card_map import (
    apply_jscontact_patch,
    build_vcard_text,
    decode_card_id,
    encode_card_id,
    extract_uid,
    vcard_to_jscontact_card,
)
from jmap_bridge.backends.carddav.client import CarddavConnection, CarddavError
from jmap_bridge.backends.carddav.sync_state import encode_addressbook_state
from jmap_bridge.context import RequestContext
from jmap_bridge.dispatch import method
from jmap_bridge.errors import (
    CannotCalculateChanges,
    InvalidArguments,
    InvalidProperties,
    MethodError,
    ServerFail,
    UnsupportedFilter,
)
from jmap_bridge.types.address_book import _cursors
from jmap_bridge.webdav_common.href import canonicalize_href_path

_SUPPORTED_FILTER_KEYS = {"inAddressBook", "text"}
_TEXT_SEARCH_GROUPS = ("nicknames", "organizations", "titles", "emails", "phones", "onlineServices", "notes")


async def _account_addressbook_state(conn: CarddavConnection) -> str:
    return encode_addressbook_state(await _cursors(conn))


def _matches_text(card_obj: dict, query: str) -> bool:
    query_lower = query.lower()
    haystacks: list[str] = []
    if card_obj.get("name"):
        haystacks.extend(c.get("value", "") for c in card_obj["name"].get("components") or [])
    for group_key in _TEXT_SEARCH_GROUPS:
        for entry in (card_obj.get(group_key) or {}).values():
            haystacks.extend(str(v) for v in entry.values() if isinstance(v, str))
    return any(query_lower in h.lower() for h in haystacks)


async def _fetch_one_card(
    conn: CarddavConnection, addressbook_href: str, card_href: str, *, report_id: str
) -> dict | None:
    try:
        entry = await conn.get_card(addressbook_href, card_href)
    except CarddavError:
        return None
    if entry is None:
        return None
    try:
        return vcard_to_jscontact_card(entry.vcard_text, report_id, encode_addressbook_id(addressbook_href))
    except ValueError:
        return None  # malformed/unparseable object on the server - unreadable, not a crash


async def _fetch_cards_by_id(
    ctx: RequestContext, conn: CarddavConnection, ids: list[str]
) -> tuple[dict[str, dict], list[str]]:
    """One multiget REPORT per addressbook (not one GET per id) - ids are
    grouped by their decoded addressbook_href first.
    """
    groups: dict[str, list[tuple[str, str]]] = {}
    not_found: list[str] = []
    for cid in ids:
        try:
            addressbook_href, card_href = decode_card_id(cid)
        except ValueError:
            not_found.append(cid)
            continue
        groups.setdefault(addressbook_href, []).append((cid, card_href))

    found: dict[str, dict] = {}
    for addressbook_href, items in groups.items():
        try:
            entries = await conn.get_cards(addressbook_href, [h for _, h in items])
        except CarddavError:
            not_found.extend(cid for cid, _ in items)
            continue
        by_path = {canonicalize_href_path(e.href): e for e in entries}
        addressbook_id = encode_addressbook_id(addressbook_href)
        for cid, card_href in items:
            entry = by_path.get(canonicalize_href_path(card_href))
            if entry is None:
                not_found.append(cid)
                continue
            try:
                found[cid] = vcard_to_jscontact_card(entry.vcard_text, cid, addressbook_id)
            except ValueError:
                not_found.append(cid)

    # Anything still missing might be an id from before a move we still
    # have a redirect on file for (id_redirect.py) - resolve and retry
    # individually before giving up, mirroring types/calendar_event.py's
    # identical fallback.
    still_missing, not_found = not_found, []
    for cid in still_missing:
        resolved = ctx.id_redirects.resolve(ctx.id_redirect_key, cid)
        obj = None
        if resolved != cid:
            try:
                r_addressbook_href, r_card_href = decode_card_id(resolved)
            except ValueError:
                r_addressbook_href = None
            if r_addressbook_href is not None:
                obj = await _fetch_one_card(conn, r_addressbook_href, r_card_href, report_id=cid)
        if obj is not None:
            found[cid] = obj
        else:
            not_found.append(cid)

    return found, not_found


@method("ContactCard/get")
async def contact_card_get(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    ids = args.get("ids")
    if ids is None:
        raise InvalidArguments("ids is required (full-account enumeration is not supported)")
    properties = args.get("properties")

    try:
        async with ctx.carddav() as conn:
            found, not_found = await _fetch_cards_by_id(ctx, conn, ids)
            state = await _account_addressbook_state(conn)
    except CarddavError as exc:
        raise ServerFail(str(exc)) from exc

    result_list = [found[i] for i in ids if i in found]
    if properties is not None:
        prop_set = set(properties) | {"id"}
        result_list = [{k: v for k, v in card.items() if k in prop_set} for card in result_list]

    return {"accountId": account_id, "state": state, "list": result_list, "notFound": not_found}


@method("ContactCard/query")
async def contact_card_query(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    filter_ = args.get("filter") or {}

    unsupported = set(filter_) - _SUPPORTED_FILTER_KEYS
    if unsupported:
        raise UnsupportedFilter(f"unsupported filter properties: {sorted(unsupported)}")

    in_addressbook = filter_.get("inAddressBook")
    if not in_addressbook:
        raise InvalidArguments(
            "filter must include inAddressBook - cross-addressbook query is not supported"
        )
    try:
        addressbook_href = decode_addressbook_id(in_addressbook)
    except ValueError as exc:
        raise InvalidArguments(f"invalid inAddressBook id: {exc}") from exc

    text = filter_.get("text")

    try:
        async with ctx.carddav() as conn:
            entries = await conn.list_cards(addressbook_href)
            query_state = await _account_addressbook_state(conn)
    except CarddavError as exc:
        raise ServerFail(str(exc)) from exc

    addressbook_id = encode_addressbook_id(addressbook_href)
    result_ids = []
    for e in entries:
        card_id = encode_card_id(addressbook_href, e.href)
        if text:
            try:
                card_obj = vcard_to_jscontact_card(e.vcard_text, card_id, addressbook_id)
            except ValueError:
                continue
            if not _matches_text(card_obj, text):
                continue
        result_ids.append(card_id)

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


@method("ContactCard/changes")
async def contact_card_changes(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    if not args.get("sinceState"):
        raise InvalidArguments("sinceState is required")
    raise CannotCalculateChanges(
        "ContactCard/changes is not implemented - RFC 6578 sync-collection cannot "
        "distinguish a created href from an updated one without locally stored state "
        "this bridge deliberately doesn't keep (see types/calendar_event.py's "
        "CalendarEvent/changes for the identical pattern); client should fall back "
        "to a full resync"
    )


async def _create_card(ctx: RequestContext, conn: CarddavConnection, props: dict) -> dict:
    addressbook_ids = props.get("addressBookIds") or {}
    target_ids = [aid for aid, keep in addressbook_ids.items() if keep]
    if len(target_ids) != 1:
        raise InvalidProperties(
            "addressBookIds must have exactly one entry (maxAddressBooksPerCard: 1)",
            properties=["addressBookIds"],
        )
    try:
        addressbook_href = decode_addressbook_id(target_ids[0])
    except ValueError as exc:
        raise InvalidArguments(
            f"invalid addressBookIds entry: {exc}", arguments=["addressBookIds"]
        ) from exc

    try:
        # Respect a client-supplied uid if given, rather than always
        # minting our own - confirmed real client behavior (Bulwark
        # webmail generates its own uid client-side before Contact/set
        # create and expects the server to honor it, not silently swap
        # in a different one the client's own local cache never learns
        # about).
        vcard_text, uid = build_vcard_text(props, uid=props.get("uid") or None)
    except ValueError as exc:
        raise InvalidArguments(str(exc)) from exc

    entry = await conn.create_card(addressbook_href, vcard_text, uid)
    card_id = encode_card_id(addressbook_href, entry.href)
    return vcard_to_jscontact_card(entry.vcard_text, card_id, encode_addressbook_id(addressbook_href))


async def _apply_card_update(ctx: RequestContext, conn: CarddavConnection, card_id: str, patch: dict) -> None:
    try:
        addressbook_href, card_href = decode_card_id(card_id)
    except ValueError as exc:
        raise InvalidArguments("invalid id") from exc

    resolved = ctx.id_redirects.resolve(ctx.id_redirect_key, card_id)
    if resolved != card_id:
        try:
            addressbook_href, card_href = decode_card_id(resolved)
        except ValueError:
            pass

    entry = await conn.get_card(addressbook_href, card_href)
    if entry is None:
        raise InvalidArguments("card not found or id is stale")

    addressbook_ids_patch = {
        k[len("addressBookIds/") :]: v for k, v in patch.items() if k.startswith("addressBookIds/")
    }
    new_addressbook_ids = patch.get("addressBookIds")
    target_addressbook_id: str | None = None
    if new_addressbook_ids is not None:
        target_ids = [aid for aid, keep in new_addressbook_ids.items() if keep]
        if len(target_ids) != 1:
            raise InvalidProperties(
                "addressBookIds must have exactly one entry (maxAddressBooksPerCard: 1)",
                properties=["addressBookIds"],
            )
        target_addressbook_id = target_ids[0]
    elif addressbook_ids_patch:
        current_id = encode_addressbook_id(addressbook_href)
        target_ids = {current_id}
        for aid, keep in addressbook_ids_patch.items():
            if keep:
                target_ids.add(aid)
            else:
                target_ids.discard(aid)
        if len(target_ids) != 1:
            raise InvalidProperties(
                "addressBookIds must have exactly one entry (maxAddressBooksPerCard: 1)",
                properties=["addressBookIds"],
            )
        target_addressbook_id = next(iter(target_ids))

    property_patch = {
        k: v for k, v in patch.items() if k != "addressBookIds" and not k.startswith("addressBookIds/")
    }
    vcard_text = entry.vcard_text
    if property_patch:
        try:
            vcard_text = apply_jscontact_patch(vcard_text, property_patch)
        except ValueError as exc:
            raise InvalidArguments(str(exc)) from exc

    current_addressbook_id = encode_addressbook_id(addressbook_href)
    if target_addressbook_id is not None and target_addressbook_id != current_addressbook_id:
        # A move: no native multi-collection membership in CardDAV, same
        # as CalendarEvent's calendarIds move - delete the old resource,
        # create a new one, record an id_redirect.
        try:
            new_addressbook_href = decode_addressbook_id(target_addressbook_id)
        except ValueError as exc:
            raise InvalidArguments(
                f"invalid addressBookIds entry: {exc}", arguments=["addressBookIds"]
            ) from exc
        new_entry = await conn.create_card(new_addressbook_href, vcard_text, extract_uid(vcard_text))
        await conn.delete_card(addressbook_href, card_href)
        new_id = encode_card_id(new_addressbook_href, new_entry.href)
        ctx.id_redirects.record(ctx.id_redirect_key, card_id, new_id)
    elif property_patch:
        await conn.update_card(addressbook_href, card_href, vcard_text)


@method("ContactCard/set")
async def contact_card_set(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
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
        async with ctx.carddav() as conn:
            for creation_id, props in create.items():
                try:
                    created[creation_id] = await _create_card(ctx, conn, props)
                except (CarddavError, InvalidArguments, InvalidProperties) as exc:
                    body = exc.to_response() if isinstance(exc, MethodError) else ServerFail(str(exc)).to_response()
                    not_created[creation_id] = body
                    continue

            for card_id, patch in update.items():
                try:
                    await _apply_card_update(ctx, conn, card_id, patch)
                except MethodError as exc:
                    not_updated[card_id] = exc.to_response()
                    continue
                except CarddavError as exc:
                    not_updated[card_id] = ServerFail(str(exc)).to_response()
                    continue
                updated[card_id] = None

            for card_id in destroy:
                try:
                    addressbook_href, card_href = decode_card_id(card_id)
                except ValueError:
                    not_destroyed[card_id] = InvalidArguments("invalid id").to_response()
                    continue
                resolved = ctx.id_redirects.resolve(ctx.id_redirect_key, card_id)
                if resolved != card_id:
                    try:
                        addressbook_href, card_href = decode_card_id(resolved)
                    except ValueError:
                        pass
                try:
                    await conn.delete_card(addressbook_href, card_href)
                except CarddavError as exc:
                    not_destroyed[card_id] = ServerFail(str(exc)).to_response()
                    continue
                destroyed.append(card_id)

            new_state = await _account_addressbook_state(conn)
    except CarddavError as exc:
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
