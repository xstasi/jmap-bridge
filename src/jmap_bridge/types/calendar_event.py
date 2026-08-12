"""CalendarEvent/get, CalendarEvent/query, CalendarEvent/set,
CalendarEvent/changes (draft-ietf-jmap-calendars-26 SS5) - non-recurring
subset (see event_map.py's module docstring; recurrence is added in a
later pass, per the Phase 2 plan).

Every CalendarEvent id is a deterministic encoding of
`(calendar_href, event_href)` (event_map.py) - decoding tells us exactly
what to GET/PUT/DELETE, with no lookup table for the common (never-moved)
case. A moved event (`calendarIds` changed) is kept resolvable via
id_redirect.py, the same mechanism as Email's mailboxIds move.

Deferred (not silently dropped - see the plan for the full list):
`CalendarEvent/copy`, `CalendarEvent/queryChanges`, `expandRecurrences`,
`CalendarEvent/query` filter properties other than `inCalendar`/`before`/
`after` (`text`/`title`/`description`/`location`/`owner`/`attendee` would
need fetch-then-locally-filter, CalDAV has no standard full-text search).

`CalendarEvent/changes` always raises `cannotCalculateChanges`, mirroring
the exact existing pattern in `types/thread.py`'s `Thread/changes`: RFC
6578 sync-collection genuinely cannot distinguish a created href from an
updated one (confirmed reading the `caldav` library's source directly),
and Bulwark webmail (the only real client reviewed so far) never calls
this method at all - see the plan for the full reasoning.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from jmap_bridge.backends.caldav.calendar_map import decode_calendar_id, encode_calendar_id
from jmap_bridge.backends.caldav.client import CaldavConnection, CaldavError
from jmap_bridge.backends.caldav.event_map import (
    apply_jscalendar_patch,
    build_vevent_ical,
    decode_event_id,
    encode_event_id,
    ical_to_jscalendar_event,
)
from jmap_bridge.backends.caldav.sync_state import encode_calendar_state
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
from jmap_bridge.types.calendar import _cursors

_SUPPORTED_FILTER_KEYS = {"inCalendar", "before", "after"}


async def _account_calendar_state(conn: CaldavConnection) -> str:
    return encode_calendar_state(await _cursors(conn))


async def _resolve_calendar_hrefs(conn: CaldavConnection, filter_: dict) -> list[str]:
    """Extract which calendar(s) a CalendarEvent/query filter restricts to.
    Handles the shapes Bulwark webmail actually sends (found live):

    - No `inCalendar` at all (only `before`/`after`, or an empty filter):
      every calendar in the account. Unlike Email/query's required
      inMailbox (mailboxes can be numerous and deep), fanning out here is
      fine - accounts realistically have a handful of calendars, same
      reasoning as ContactCard/query's optional inAddressBook.
    - A flat `{"inCalendar": id}` condition: that one calendar.
    - `{"operator": "OR", "conditions": [{"inCalendar": id}, ...]}`: this
      bridge previously rejected the whole query outright the moment an
      account had more than one calendar, because Bulwark's own
      buildInCalendarFilter() only ever sends a flat condition for
      exactly one calendar - for more than one, it sends an OR of
      single-inCalendar conditions instead (Stalwart, its reference
      server, implements the singular `inCalendar` condition per the
      draft spec but not the plural `inCalendars` array, so a client
      wanting several calendars has no other spec-legal way to ask).
    """
    if "operator" in filter_ or "conditions" in filter_:
        conditions = filter_.get("conditions")
        if filter_.get("operator") != "OR" or not isinstance(conditions, list) or not conditions:
            raise UnsupportedFilter("only a flat OR of inCalendar conditions is supported")
        hrefs = []
        for cond in conditions:
            if not isinstance(cond, dict) or set(cond) != {"inCalendar"}:
                raise UnsupportedFilter("only a flat OR of inCalendar conditions is supported")
            try:
                hrefs.append(decode_calendar_id(cond["inCalendar"]))
            except ValueError as exc:
                raise InvalidArguments(f"invalid inCalendar id: {exc}") from exc
        return hrefs

    unsupported = set(filter_) - _SUPPORTED_FILTER_KEYS
    if unsupported:
        raise UnsupportedFilter(f"unsupported filter properties: {sorted(unsupported)}")

    in_calendar = filter_.get("inCalendar")
    if in_calendar:
        try:
            return [decode_calendar_id(in_calendar)]
        except ValueError as exc:
            raise InvalidArguments(f"invalid inCalendar id: {exc}") from exc

    return [entry.href for entry in await conn.list_calendars()]


def _parse_utc_datetime(value: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidArguments(f"invalid {field}: {value!r}") from exc


async def _fetch_one_event(
    conn: CaldavConnection, calendar_href: str, event_href: str, *, report_id: str
) -> dict | None:
    try:
        entry = await conn.get_event(calendar_href, event_href)
    except CaldavError:
        return None
    if entry is None:
        return None
    try:
        return ical_to_jscalendar_event(entry.ical_text, report_id, encode_calendar_id(calendar_href))
    except ValueError:
        return None  # malformed/unparseable object on the server - unreadable, not a crash


async def _fetch_events_by_id(
    ctx: RequestContext, conn: CaldavConnection, ids: list[str]
) -> tuple[dict[str, dict], list[str]]:
    """One GET per id - no CalDAV multiget batching for v1 (see the
    module docstring's deferred list isn't the place for this, it's a
    performance note, not a correctness gap): acceptable for the common
    "get a handful of events" case; a client requesting many ids at once
    pays N round trips, not 1. Fast-follow if this proves to matter.
    """
    found: dict[str, dict] = {}
    not_found: list[str] = []
    for eid in ids:
        try:
            calendar_href, event_href = decode_event_id(eid)
        except ValueError:
            not_found.append(eid)
            continue
        obj = await _fetch_one_event(conn, calendar_href, event_href, report_id=eid)
        if obj is None:
            resolved = ctx.id_redirects.resolve(ctx.id_redirect_key, eid)
            if resolved != eid:
                try:
                    r_calendar_href, r_event_href = decode_event_id(resolved)
                except ValueError:
                    r_calendar_href = None
                if r_calendar_href is not None:
                    obj = await _fetch_one_event(conn, r_calendar_href, r_event_href, report_id=eid)
        if obj is not None:
            found[eid] = obj
        else:
            not_found.append(eid)
    return found, not_found


@method("CalendarEvent/get")
async def calendar_event_get(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    ids = args.get("ids")
    if ids is None:
        raise InvalidArguments("ids is required (full-account enumeration is not supported)")
    properties = args.get("properties")

    try:
        async with ctx.caldav() as conn:
            found, not_found = await _fetch_events_by_id(ctx, conn, ids)
            state = await _account_calendar_state(conn)
    except CaldavError as exc:
        raise ServerFail(str(exc)) from exc

    result_list = [found[i] for i in ids if i in found]
    if properties is not None:
        prop_set = set(properties) | {"id"}
        result_list = [{k: v for k, v in event.items() if k in prop_set} for event in result_list]

    return {"accountId": account_id, "state": state, "list": result_list, "notFound": not_found}


@method("CalendarEvent/query")
async def calendar_event_query(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    filter_ = args.get("filter") or {}

    before = filter_.get("before")
    after = filter_.get("after")

    try:
        async with ctx.caldav() as conn:
            calendar_hrefs = await _resolve_calendar_hrefs(conn, filter_)
            result_ids: list[str] = []
            for calendar_href in calendar_hrefs:
                if before or after:
                    start = _parse_utc_datetime(after, "after") if after else datetime.min.replace(tzinfo=timezone.utc)
                    end = _parse_utc_datetime(before, "before") if before else datetime.max.replace(tzinfo=timezone.utc)
                    entries = await conn.search_events_in_range(calendar_href, start, end)
                else:
                    entries = await conn.list_events(calendar_href)
                result_ids.extend(encode_event_id(calendar_href, e.href) for e in entries)
            query_state = await _account_calendar_state(conn)
    except CaldavError as exc:
        raise ServerFail(str(exc)) from exc

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


@method("CalendarEvent/changes")
async def calendar_event_changes(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    if not args.get("sinceState"):
        raise InvalidArguments("sinceState is required")
    raise CannotCalculateChanges(
        "CalendarEvent/changes is not implemented - RFC 6578 sync-collection "
        "cannot distinguish a created href from an updated one without locally "
        "stored state this bridge deliberately doesn't keep (see types/thread.py's "
        "Thread/changes for the identical pattern); client should fall back to a "
        "full resync"
    )


async def _create_event(ctx: RequestContext, conn: CaldavConnection, props: dict) -> dict:
    calendar_ids = props.get("calendarIds") or {}
    target_ids = [cid for cid, keep in calendar_ids.items() if keep]
    if len(target_ids) != 1:
        raise InvalidProperties(
            "calendarIds must have exactly one entry (maxCalendarsPerEvent: 1)",
            properties=["calendarIds"],
        )
    try:
        calendar_href = decode_calendar_id(target_ids[0])
    except ValueError as exc:
        raise InvalidArguments(f"invalid calendarIds entry: {exc}", arguments=["calendarIds"]) from exc

    try:
        # Respect a client-supplied uid if given, rather than always
        # minting our own - same fix as ContactCard/set's _create_card
        # (types/contact_card.py), found by cross-checking against real
        # client behavior: a client that generates its own uid before
        # CalendarEvent/set create expects the server to honor it, not
        # silently swap in a different one its local cache never learns
        # about.
        ical_text = build_vevent_ical(props, uid=props.get("uid") or None)
    except ValueError as exc:
        raise InvalidArguments(str(exc)) from exc

    entry = await conn.create_event(calendar_href, ical_text)
    event_id = encode_event_id(calendar_href, entry.href)
    return ical_to_jscalendar_event(entry.ical_text, event_id, encode_calendar_id(calendar_href))


async def _apply_event_update(ctx: RequestContext, conn: CaldavConnection, event_id: str, patch: dict) -> None:
    try:
        calendar_href, event_href = decode_event_id(event_id)
    except ValueError as exc:
        raise InvalidArguments("invalid id") from exc

    resolved = ctx.id_redirects.resolve(ctx.id_redirect_key, event_id)
    if resolved != event_id:
        try:
            calendar_href, event_href = decode_event_id(resolved)
        except ValueError:
            pass

    entry = await conn.get_event(calendar_href, event_href)
    if entry is None:
        raise InvalidArguments("event not found or id is stale")

    calendar_ids_patch = {
        k[len("calendarIds/") :]: v for k, v in patch.items() if k.startswith("calendarIds/")
    }
    new_calendar_ids = patch.get("calendarIds")
    target_calendar_id: str | None = None
    if new_calendar_ids is not None:
        target_ids = [cid for cid, keep in new_calendar_ids.items() if keep]
        if len(target_ids) != 1:
            raise InvalidProperties(
                "calendarIds must have exactly one entry (maxCalendarsPerEvent: 1)",
                properties=["calendarIds"],
            )
        target_calendar_id = target_ids[0]
    elif calendar_ids_patch:
        current_id = encode_calendar_id(calendar_href)
        target_ids = {current_id}
        for cid, keep in calendar_ids_patch.items():
            if keep:
                target_ids.add(cid)
            else:
                target_ids.discard(cid)
        if len(target_ids) != 1:
            raise InvalidProperties(
                "calendarIds must have exactly one entry (maxCalendarsPerEvent: 1)",
                properties=["calendarIds"],
            )
        target_calendar_id = next(iter(target_ids))

    property_patch = {
        k: v for k, v in patch.items() if k != "calendarIds" and not k.startswith("calendarIds/")
    }
    ical_text = entry.ical_text
    if property_patch:
        try:
            ical_text = apply_jscalendar_patch(ical_text, property_patch)
        except ValueError as exc:
            raise InvalidArguments(str(exc)) from exc

    current_calendar_id = encode_calendar_id(calendar_href)
    if target_calendar_id is not None and target_calendar_id != current_calendar_id:
        # A calendarIds change is a "move" - CalDAV has no native
        # multi-collection membership (see session.py's
        # maxCalendarsPerEvent: 1), so this deletes the old resource and
        # creates a new one, exactly mirroring _apply_email_update's
        # "leaving its only known physical location" branch for Email.
        try:
            new_calendar_href = decode_calendar_id(target_calendar_id)
        except ValueError as exc:
            raise InvalidArguments(f"invalid calendarIds entry: {exc}", arguments=["calendarIds"]) from exc
        new_entry = await conn.create_event(new_calendar_href, ical_text)
        await conn.delete_event(calendar_href, event_href)
        new_id = encode_event_id(new_calendar_href, new_entry.href)
        ctx.id_redirects.record(ctx.id_redirect_key, event_id, new_id)
    elif property_patch:
        await conn.update_event(calendar_href, event_href, ical_text)


@method("CalendarEvent/set")
async def calendar_event_set(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
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
                try:
                    created[creation_id] = await _create_event(ctx, conn, props)
                except (CaldavError, InvalidArguments, InvalidProperties) as exc:
                    body = exc.to_response() if isinstance(exc, MethodError) else ServerFail(str(exc)).to_response()
                    not_created[creation_id] = body
                    continue

            for event_id, patch in update.items():
                try:
                    await _apply_event_update(ctx, conn, event_id, patch)
                except MethodError as exc:
                    not_updated[event_id] = exc.to_response()
                    continue
                except CaldavError as exc:
                    not_updated[event_id] = ServerFail(str(exc)).to_response()
                    continue
                updated[event_id] = None

            for event_id in destroy:
                try:
                    calendar_href, event_href = decode_event_id(event_id)
                except ValueError:
                    not_destroyed[event_id] = InvalidArguments("invalid id").to_response()
                    continue
                resolved = ctx.id_redirects.resolve(ctx.id_redirect_key, event_id)
                if resolved != event_id:
                    try:
                        calendar_href, event_href = decode_event_id(resolved)
                    except ValueError:
                        pass
                try:
                    await conn.delete_event(calendar_href, event_href)
                except CaldavError as exc:
                    not_destroyed[event_id] = ServerFail(str(exc)).to_response()
                    continue
                destroyed.append(event_id)

            new_state = await _account_calendar_state(conn)
    except CaldavError as exc:
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
