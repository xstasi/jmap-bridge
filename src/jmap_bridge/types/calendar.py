"""Calendar/get, Calendar/set, Calendar/changes
(draft-ietf-jmap-calendars-26 SS4). Every Calendar id is a deterministic
encoding of its CalDAV collection href (calendar_map.py) - decoding an id
tells us exactly which collection to act on, with no lookup table.

No Calendar/query: the spec defines only get/set/changes for Calendar
(unlike Mailbox, which also has query) - confirmed by reading the spec
text directly, not an oversight.
"""

from __future__ import annotations

from typing import Any

from jmap_bridge.backends.caldav.calendar_map import (
    build_jmap_calendar,
    decode_calendar_id,
    encode_calendar_id,
)
from jmap_bridge.backends.caldav.client import CaldavConnection, CaldavError
from jmap_bridge.backends.caldav.sync_state import (
    CalendarCursor,
    decode_calendar_state,
    diff_calendar_state,
    encode_calendar_state,
)
from jmap_bridge.context import RequestContext
from jmap_bridge.dispatch import method
from jmap_bridge.errors import CannotCalculateChanges, InvalidArguments, ServerFail
from jmap_bridge.state import InvalidStateToken


async def _cursors(conn: CaldavConnection) -> dict[str, CalendarCursor]:
    entries = await conn.list_calendars()
    return {
        e.href: CalendarCursor(sync_token=e.sync_token, display_name=e.display_name) for e in entries
    }


@method("Calendar/get")
async def calendar_get(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    ids = args.get("ids")
    properties = args.get("properties")

    try:
        async with ctx.caldav() as conn:
            entries = await conn.list_calendars()
    except CaldavError as exc:
        raise ServerFail(str(exc)) from exc

    all_calendars = [build_jmap_calendar(e) for e in entries]
    state = encode_calendar_state(
        {e.href: CalendarCursor(sync_token=e.sync_token, display_name=e.display_name) for e in entries}
    )

    by_id = {c["id"]: c for c in all_calendars}
    if ids is None:
        selected = list(all_calendars)
        not_found: list[str] = []
    else:
        selected = [by_id[i] for i in ids if i in by_id]
        not_found = [i for i in ids if i not in by_id]

    if properties is not None:
        selected = [{"id": c["id"], **{p: c[p] for p in properties if p in c}} for c in selected]

    return {"accountId": account_id, "state": state, "list": selected, "notFound": not_found}


@method("Calendar/changes")
async def calendar_changes(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    since_state = args.get("sinceState")
    if not since_state:
        raise InvalidArguments("sinceState is required")

    try:
        old_cursors = decode_calendar_state(since_state)
    except InvalidStateToken as exc:
        raise CannotCalculateChanges(str(exc)) from exc

    try:
        async with ctx.caldav() as conn:
            new_cursors = await _cursors(conn)
    except CaldavError as exc:
        raise ServerFail(str(exc)) from exc

    diff = diff_calendar_state(old_cursors, new_cursors)

    return {
        "accountId": account_id,
        "oldState": since_state,
        "newState": encode_calendar_state(new_cursors),
        "hasMoreChanges": False,
        "created": [encode_calendar_id(h) for h in diff.created],
        "updated": [encode_calendar_id(h) for h in diff.updated],
        "destroyed": [encode_calendar_id(h) for h in diff.destroyed],
    }


@method("Calendar/set")
async def calendar_set(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
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
        async with ctx.caldav() as conn:
            for creation_id, props in create.items():
                name = props.get("name")
                if not name:
                    not_created[creation_id] = InvalidArguments(
                        "name is required", arguments=["name"]
                    ).to_response()
                    continue
                try:
                    href = await conn.create_calendar(name)
                except CaldavError as exc:
                    not_created[creation_id] = ServerFail(str(exc)).to_response()
                    continue
                created[creation_id] = {"id": encode_calendar_id(href)}

            for calendar_id, props in update.items():
                try:
                    href = decode_calendar_id(calendar_id)
                except ValueError:
                    not_updated[calendar_id] = InvalidArguments("invalid id").to_response()
                    continue
                new_name = props.get("name")
                if new_name:
                    try:
                        await conn.rename_calendar(href, new_name)
                    except CaldavError as exc:
                        not_updated[calendar_id] = ServerFail(str(exc)).to_response()
                        continue
                # The href (and therefore the id) never changes on a
                # rename - see calendar_map.py's module docstring - so,
                # unlike Mailbox, there's no id-redirect concern here.
                updated[calendar_id] = None

            for calendar_id in destroy:
                try:
                    href = decode_calendar_id(calendar_id)
                except ValueError:
                    not_destroyed[calendar_id] = InvalidArguments("invalid id").to_response()
                    continue
                try:
                    await conn.delete_calendar(href)
                except CaldavError as exc:
                    not_destroyed[calendar_id] = ServerFail(str(exc)).to_response()
                    continue
                destroyed.append(calendar_id)

            new_state = encode_calendar_state(await _cursors(conn))
    except CaldavError as exc:
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
