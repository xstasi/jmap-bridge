"""AddressBook/get, AddressBook/set, AddressBook/changes (RFC 9610 SS2).
Every AddressBook id is a deterministic encoding of its CardDAV
collection href (addressbook_map.py) - decoding an id tells us exactly
which collection to act on, with no lookup table.

No AddressBook/query: the spec defines only get/set/changes for
AddressBook (mirrors Calendar, not Mailbox) - confirmed by reading the
spec text directly, not an oversight.

`AddressBook/changes` is implemented for real (not stubbed): unlike
`ContactCard/changes` (types/contact_card.py, stubbed for the same
reason `CalendarEvent/changes` is - RFC 6578 sync-collection can't
distinguish created/updated at the member-object level), AddressBook is
the container-level type, structurally identical to Calendar, which
already has a real `Calendar/changes`. There's no technical barrier here,
and the listing it needs is already required for `AddressBook/get`'s
state string - see the Phase 3 plan.
"""

from __future__ import annotations

from typing import Any

from jmap_bridge.backends.carddav.addressbook_map import (
    build_jmap_addressbook,
    decode_addressbook_id,
    encode_addressbook_id,
)
from jmap_bridge.backends.carddav.client import CarddavConnection, CarddavError
from jmap_bridge.backends.carddav.sync_state import (
    AddressBookCursor,
    decode_addressbook_state,
    diff_addressbook_state,
    encode_addressbook_state,
)
from jmap_bridge.context import RequestContext
from jmap_bridge.dispatch import method
from jmap_bridge.errors import CannotCalculateChanges, InvalidArguments, ServerFail
from jmap_bridge.state import InvalidStateToken


async def _cursors(conn: CarddavConnection) -> dict[str, AddressBookCursor]:
    entries = await conn.list_addressbooks()
    return {
        e.href: AddressBookCursor(sync_token=e.sync_token, display_name=e.display_name)
        for e in entries
    }


@method("AddressBook/get")
async def address_book_get(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    ids = args.get("ids")
    properties = args.get("properties")

    try:
        async with ctx.carddav() as conn:
            entries = await conn.list_addressbooks()
    except CarddavError as exc:
        raise ServerFail(str(exc)) from exc

    all_addressbooks = [build_jmap_addressbook(e) for e in entries]
    state = encode_addressbook_state(
        {
            e.href: AddressBookCursor(sync_token=e.sync_token, display_name=e.display_name)
            for e in entries
        }
    )

    by_id = {a["id"]: a for a in all_addressbooks}
    if ids is None:
        selected = list(all_addressbooks)
        not_found: list[str] = []
    else:
        selected = [by_id[i] for i in ids if i in by_id]
        not_found = [i for i in ids if i not in by_id]

    if properties is not None:
        selected = [{"id": a["id"], **{p: a[p] for p in properties if p in a}} for a in selected]

    return {"accountId": account_id, "state": state, "list": selected, "notFound": not_found}


@method("AddressBook/changes")
async def address_book_changes(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    since_state = args.get("sinceState")
    if not since_state:
        raise InvalidArguments("sinceState is required")

    try:
        old_cursors = decode_addressbook_state(since_state)
    except InvalidStateToken as exc:
        raise CannotCalculateChanges(str(exc)) from exc

    try:
        async with ctx.carddav() as conn:
            new_cursors = await _cursors(conn)
    except CarddavError as exc:
        raise ServerFail(str(exc)) from exc

    diff = diff_addressbook_state(old_cursors, new_cursors)

    return {
        "accountId": account_id,
        "oldState": since_state,
        "newState": encode_addressbook_state(new_cursors),
        "hasMoreChanges": False,
        "created": [encode_addressbook_id(h) for h in diff.created],
        "updated": [encode_addressbook_id(h) for h in diff.updated],
        "destroyed": [encode_addressbook_id(h) for h in diff.destroyed],
    }


@method("AddressBook/set")
async def address_book_set(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
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
                name = props.get("name")
                if not name:
                    not_created[creation_id] = InvalidArguments(
                        "name is required", arguments=["name"]
                    ).to_response()
                    continue
                try:
                    href = await conn.create_addressbook(name)
                except CarddavError as exc:
                    not_created[creation_id] = ServerFail(str(exc)).to_response()
                    continue
                created[creation_id] = {"id": encode_addressbook_id(href)}

            for addressbook_id, props in update.items():
                try:
                    href = decode_addressbook_id(addressbook_id)
                except ValueError:
                    not_updated[addressbook_id] = InvalidArguments("invalid id").to_response()
                    continue
                new_name = props.get("name")
                if new_name:
                    try:
                        await conn.rename_addressbook(href, new_name)
                    except CarddavError as exc:
                        not_updated[addressbook_id] = ServerFail(str(exc)).to_response()
                        continue
                # The href (and therefore the id) never changes on a
                # rename - see addressbook_map.py's module docstring - so,
                # like Calendar, there's no id-redirect concern here.
                updated[addressbook_id] = None

            for addressbook_id in destroy:
                try:
                    href = decode_addressbook_id(addressbook_id)
                except ValueError:
                    not_destroyed[addressbook_id] = InvalidArguments("invalid id").to_response()
                    continue
                try:
                    await conn.delete_addressbook(href)
                except CarddavError as exc:
                    not_destroyed[addressbook_id] = ServerFail(str(exc)).to_response()
                    continue
                destroyed.append(addressbook_id)

            new_state = encode_addressbook_state(await _cursors(conn))
    except CarddavError as exc:
        raise ServerFail(str(exc)) from exc

    return {
        "accountId": account_id,
        "oldState": None,
        "newState": new_state,
        "created": created,
        "updated": updated,
        "destroyed": destroyed,
        "notCreated": not_created,
        "notUpdated": not_updated,
        "notDestroyed": not_destroyed,
    }
