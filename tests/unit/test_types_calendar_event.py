from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from jmap_bridge.backends.caldav.calendar_map import encode_calendar_id
from jmap_bridge.backends.caldav.client import CalendarEntry, EventEntry
from jmap_bridge.backends.caldav.event_map import build_vevent_ical, encode_event_id
from jmap_bridge.errors import CannotCalculateChanges, InvalidArguments
from jmap_bridge.id_redirect import IdRedirectCache
from jmap_bridge.types import calendar_event as ce_types

WORK = "/alice/work/"
HOME = "/alice/home/"


class FakeCaldavConn:
    def __init__(self, calendars: dict[str, dict], events: dict[str, dict[str, str]]):
        # calendars: href -> {"name": str, "sync_token": str}
        # events: calendar_href -> {event_href: ical_text}
        self._calendars = calendars
        self._events = events

    async def list_calendars(self):
        return [
            CalendarEntry(href=href, display_name=info["name"], sync_token=info["sync_token"])
            for href, info in self._calendars.items()
        ]

    async def list_events(self, calendar_href):
        return [
            EventEntry(href=href, ical_text=text, etag=None)
            for href, text in self._events.get(calendar_href, {}).items()
        ]

    async def search_events_in_range(self, calendar_href, start, end):
        return await self.list_events(calendar_href)

    async def get_event(self, calendar_href, event_href):
        text = self._events.get(calendar_href, {}).get(event_href)
        if text is None:
            return None
        return EventEntry(href=event_href, ical_text=text, etag="etag1")

    async def create_event(self, calendar_href, ical_text):
        import re

        uid_match = re.search(r"UID:([^\r\n]+)", ical_text)
        uid = uid_match.group(1) if uid_match else "generated-uid"
        href = f"{calendar_href}{uid}.ics"
        self._events.setdefault(calendar_href, {})[href] = ical_text
        return EventEntry(href=href, ical_text=ical_text, etag="etag1")

    async def update_event(self, calendar_href, event_href, ical_text):
        self._events[calendar_href][event_href] = ical_text
        return EventEntry(href=event_href, ical_text=ical_text, etag="etag2")

    async def delete_event(self, calendar_href, event_href):
        self._events.get(calendar_href, {}).pop(event_href, None)


class FakeContext:
    account_id = "Aalice"
    id_redirect_key = ("example.com", "alice@example.com")

    def __init__(self, conn: FakeCaldavConn):
        self._conn = conn
        self.id_redirects = IdRedirectCache()

    def require_account(self, account_id):
        assert account_id == self.account_id

    def caldav(self):
        @asynccontextmanager
        async def _cm():
            yield self._conn

        return _cm()


def _default_calendars():
    return {
        WORK: {"name": "Work", "sync_token": "tok1"},
        HOME: {"name": "Home", "sync_token": "tok2"},
    }


def _make_ical(uid, title="Test event", start="2026-06-01T10:00:00", duration="PT1H"):
    return build_vevent_ical({"title": title, "start": start, "duration": duration}, uid=uid)


async def test_calendar_event_get():
    events = {WORK: {f"{WORK}ev1.ics": _make_ical("ev1")}}
    conn = FakeCaldavConn(_default_calendars(), events)
    ctx = FakeContext(conn)
    eid = encode_event_id(WORK, f"{WORK}ev1.ics")

    result = await ce_types.calendar_event_get(ctx, {"ids": [eid]})
    assert result["notFound"] == []
    assert result["list"][0]["id"] == eid
    assert result["list"][0]["title"] == "Test event"
    assert result["list"][0]["calendarIds"] == {encode_calendar_id(WORK): True}


async def test_calendar_event_get_unknown_id():
    conn = FakeCaldavConn(_default_calendars(), {})
    ctx = FakeContext(conn)
    result = await ce_types.calendar_event_get(ctx, {"ids": ["Vbogus"]})
    assert result["notFound"] == ["Vbogus"]


async def test_calendar_event_get_requires_ids():
    conn = FakeCaldavConn(_default_calendars(), {})
    ctx = FakeContext(conn)
    with pytest.raises(InvalidArguments):
        await ce_types.calendar_event_get(ctx, {})


async def test_calendar_event_query_requires_in_calendar():
    conn = FakeCaldavConn(_default_calendars(), {})
    ctx = FakeContext(conn)
    with pytest.raises(InvalidArguments):
        await ce_types.calendar_event_query(ctx, {"filter": {}})


async def test_calendar_event_query_lists_events_in_calendar():
    events = {
        WORK: {f"{WORK}ev1.ics": _make_ical("ev1"), f"{WORK}ev2.ics": _make_ical("ev2")},
        HOME: {f"{HOME}ev3.ics": _make_ical("ev3")},
    }
    conn = FakeCaldavConn(_default_calendars(), events)
    ctx = FakeContext(conn)

    result = await ce_types.calendar_event_query(
        ctx, {"filter": {"inCalendar": encode_calendar_id(WORK)}}
    )
    assert result["total"] == 2
    assert len(result["ids"]) == 2


async def test_calendar_event_query_before_after_uses_time_range_search():
    events = {WORK: {f"{WORK}ev1.ics": _make_ical("ev1")}}
    conn = FakeCaldavConn(_default_calendars(), events)
    ctx = FakeContext(conn)

    calls = []
    orig = conn.search_events_in_range

    async def spy(calendar_href, start, end):
        calls.append((start, end))
        return await orig(calendar_href, start, end)

    conn.search_events_in_range = spy

    result = await ce_types.calendar_event_query(
        ctx,
        {
            "filter": {
                "inCalendar": encode_calendar_id(WORK),
                "after": "2026-01-01T00:00:00Z",
                "before": "2026-12-31T00:00:00Z",
            }
        },
    )
    assert len(calls) == 1
    assert result["total"] == 1


async def test_calendar_event_query_unsupported_filter_rejected():
    from jmap_bridge.errors import UnsupportedFilter

    conn = FakeCaldavConn(_default_calendars(), {})
    ctx = FakeContext(conn)
    with pytest.raises(UnsupportedFilter):
        await ce_types.calendar_event_query(
            ctx, {"filter": {"inCalendar": encode_calendar_id(WORK), "text": "hello"}}
        )


async def test_calendar_event_changes_always_cannot_calculate():
    conn = FakeCaldavConn(_default_calendars(), {})
    ctx = FakeContext(conn)
    with pytest.raises(CannotCalculateChanges):
        await ce_types.calendar_event_changes(ctx, {"sinceState": "whatever"})


async def test_calendar_event_changes_requires_since_state():
    conn = FakeCaldavConn(_default_calendars(), {})
    ctx = FakeContext(conn)
    with pytest.raises(InvalidArguments):
        await ce_types.calendar_event_changes(ctx, {})


async def test_calendar_event_set_create():
    conn = FakeCaldavConn(_default_calendars(), {})
    ctx = FakeContext(conn)

    result = await ce_types.calendar_event_set(
        ctx,
        {
            "create": {
                "e1": {
                    "calendarIds": {encode_calendar_id(WORK): True},
                    "title": "New event",
                    "start": "2026-07-01T09:00:00",
                    "duration": "PT30M",
                }
            }
        },
    )
    assert result["notCreated"] == {}
    assert "e1" in result["created"]
    assert result["created"]["e1"]["title"] == "New event"
    assert len(conn._events[WORK]) == 1


async def test_calendar_event_set_create_respects_client_supplied_uid():
    """Regression test: confirmed real client behavior (Bulwark webmail's
    contacts code, same class of bug likely present here too) generates
    its own uid client-side before Foo/set create and expects the server
    to honor it, not silently swap in a different one.
    """
    conn = FakeCaldavConn(_default_calendars(), {})
    ctx = FakeContext(conn)

    result = await ce_types.calendar_event_set(
        ctx,
        {
            "create": {
                "e1": {
                    "calendarIds": {encode_calendar_id(WORK): True},
                    "uid": "client-chosen-uid-456",
                    "title": "New event",
                    "start": "2026-07-01T09:00:00",
                    "duration": "PT30M",
                }
            }
        },
    )
    assert result["notCreated"] == {}
    assert result["created"]["e1"]["uid"] == "client-chosen-uid-456"


async def test_calendar_event_set_create_requires_exactly_one_calendar():
    conn = FakeCaldavConn(_default_calendars(), {})
    ctx = FakeContext(conn)
    result = await ce_types.calendar_event_set(
        ctx,
        {"create": {"e1": {"calendarIds": {}, "title": "x", "start": "2026-07-01T09:00:00"}}},
    )
    assert result["notCreated"]["e1"]["type"] == "invalidProperties"


async def test_calendar_event_set_update_property_patch():
    events = {WORK: {f"{WORK}ev1.ics": _make_ical("ev1")}}
    conn = FakeCaldavConn(_default_calendars(), events)
    ctx = FakeContext(conn)
    eid = encode_event_id(WORK, f"{WORK}ev1.ics")

    result = await ce_types.calendar_event_set(ctx, {"update": {eid: {"title": "Renamed"}}})
    assert result["notUpdated"] == {}
    assert "Renamed" in conn._events[WORK][f"{WORK}ev1.ics"]


async def test_calendar_event_set_move_via_full_calendar_ids_replacement():
    events = {WORK: {f"{WORK}ev1.ics": _make_ical("ev1")}}
    conn = FakeCaldavConn(_default_calendars(), events)
    ctx = FakeContext(conn)
    eid = encode_event_id(WORK, f"{WORK}ev1.ics")

    result = await ce_types.calendar_event_set(
        ctx, {"update": {eid: {"calendarIds": {encode_calendar_id(HOME): True}}}}
    )
    assert result["notUpdated"] == {}
    assert f"{WORK}ev1.ics" not in conn._events.get(WORK, {})
    assert len(conn._events[HOME]) == 1


async def test_calendar_event_set_move_via_patch_key():
    events = {WORK: {f"{WORK}ev1.ics": _make_ical("ev1")}}
    conn = FakeCaldavConn(_default_calendars(), events)
    ctx = FakeContext(conn)
    eid = encode_event_id(WORK, f"{WORK}ev1.ics")

    result = await ce_types.calendar_event_set(
        ctx,
        {
            "update": {
                eid: {
                    f"calendarIds/{encode_calendar_id(WORK)}": None,
                    f"calendarIds/{encode_calendar_id(HOME)}": True,
                }
            }
        },
    )
    assert result["notUpdated"] == {}
    assert len(conn._events[HOME]) == 1


async def test_calendar_event_move_records_id_redirect_and_get_resolves_it():
    events = {WORK: {f"{WORK}ev1.ics": _make_ical("ev1")}}
    conn = FakeCaldavConn(_default_calendars(), events)
    ctx = FakeContext(conn)
    old_id = encode_event_id(WORK, f"{WORK}ev1.ics")

    result = await ce_types.calendar_event_set(
        ctx, {"update": {old_id: {"calendarIds": {encode_calendar_id(HOME): True}}}}
    )
    assert result["notUpdated"] == {}

    get_result = await ce_types.calendar_event_get(ctx, {"ids": [old_id]})
    assert get_result["notFound"] == []
    assert get_result["list"][0]["id"] == old_id
    assert get_result["list"][0]["calendarIds"] == {encode_calendar_id(HOME): True}


async def test_calendar_event_set_destroy():
    events = {WORK: {f"{WORK}ev1.ics": _make_ical("ev1")}}
    conn = FakeCaldavConn(_default_calendars(), events)
    ctx = FakeContext(conn)
    eid = encode_event_id(WORK, f"{WORK}ev1.ics")

    result = await ce_types.calendar_event_set(ctx, {"destroy": [eid]})
    assert result["destroyed"] == [eid]
    assert f"{WORK}ev1.ics" not in conn._events[WORK]


async def test_calendar_event_set_destroy_on_redirected_id():
    events = {WORK: {f"{WORK}ev1.ics": _make_ical("ev1")}}
    conn = FakeCaldavConn(_default_calendars(), events)
    ctx = FakeContext(conn)
    old_id = encode_event_id(WORK, f"{WORK}ev1.ics")

    await ce_types.calendar_event_set(
        ctx, {"update": {old_id: {"calendarIds": {encode_calendar_id(HOME): True}}}}
    )
    result = await ce_types.calendar_event_set(ctx, {"destroy": [old_id]})
    assert result["destroyed"] == [old_id]
    assert result["notDestroyed"] == {}
    assert not conn._events.get(HOME, {})
