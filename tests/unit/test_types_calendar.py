from contextlib import asynccontextmanager

import pytest

from jmap_bridge.backends.caldav.calendar_map import encode_calendar_id
from jmap_bridge.backends.caldav.client import CalendarEntry
from jmap_bridge.errors import CannotCalculateChanges, InvalidArguments
from jmap_bridge.types import calendar as calendar_types


class FakeCaldavConn:
    def __init__(self, calendars: dict[str, dict]):
        # href -> {"name": str, "sync_token": str}
        self._calendars = calendars

    async def list_calendars(self):
        return [
            CalendarEntry(href=href, display_name=info["name"], sync_token=info["sync_token"])
            for href, info in self._calendars.items()
        ]

    async def create_calendar(self, name):
        href = f"/alice/{name.lower().replace(' ', '-')}/"
        self._calendars[href] = {"name": name, "sync_token": "tok-new"}
        return href

    async def rename_calendar(self, href, new_name):
        self._calendars[href]["name"] = new_name

    async def delete_calendar(self, href):
        del self._calendars[href]


class FakeContext:
    account_id = "Aalice"

    def __init__(self, conn: FakeCaldavConn):
        self._conn = conn

    def require_account(self, account_id):
        assert account_id == self.account_id

    def caldav(self):
        @asynccontextmanager
        async def _cm():
            yield self._conn

        return _cm()


def _default_calendars():
    return {
        "/alice/work/": {"name": "Work", "sync_token": "tok1"},
        "/alice/home/": {"name": "Home", "sync_token": "tok2"},
    }


async def test_calendar_get_returns_all():
    conn = FakeCaldavConn(_default_calendars())
    ctx = FakeContext(conn)
    result = await calendar_types.calendar_get(ctx, {})
    assert result["accountId"] == "Aalice"
    by_name = {c["name"]: c for c in result["list"]}
    assert "Work" in by_name and "Home" in by_name
    assert result["notFound"] == []


async def test_calendar_get_with_ids_and_properties():
    conn = FakeCaldavConn(_default_calendars())
    ctx = FakeContext(conn)
    work_id = encode_calendar_id("/alice/work/")
    result = await calendar_types.calendar_get(ctx, {"ids": [work_id, "Cbogus"], "properties": ["name"]})
    assert result["list"] == [{"id": work_id, "name": "Work"}]
    assert result["notFound"] == ["Cbogus"]


async def test_calendar_changes_detects_created_updated_destroyed():
    calendars = _default_calendars()
    conn = FakeCaldavConn(calendars)
    ctx = FakeContext(conn)
    initial = await calendar_types.calendar_get(ctx, {})
    since_state = initial["state"]

    calendars["/alice/work/"]["name"] = "Work Renamed"  # updated
    calendars["/alice/personal/"] = {"name": "Personal", "sync_token": "tok3"}  # created
    del calendars["/alice/home/"]  # destroyed

    changes = await calendar_types.calendar_changes(ctx, {"sinceState": since_state})
    assert changes["created"] == [encode_calendar_id("/alice/personal/")]
    assert changes["updated"] == [encode_calendar_id("/alice/work/")]
    assert changes["destroyed"] == [encode_calendar_id("/alice/home/")]


async def test_calendar_changes_rejects_missing_since_state():
    conn = FakeCaldavConn(_default_calendars())
    ctx = FakeContext(conn)
    with pytest.raises(InvalidArguments):
        await calendar_types.calendar_changes(ctx, {})


async def test_calendar_changes_garbage_since_state_is_cannot_calculate():
    conn = FakeCaldavConn(_default_calendars())
    ctx = FakeContext(conn)
    with pytest.raises(CannotCalculateChanges):
        await calendar_types.calendar_changes(ctx, {"sinceState": "not-a-real-token"})


async def test_calendar_set_create_update_destroy():
    calendars = _default_calendars()
    conn = FakeCaldavConn(calendars)
    ctx = FakeContext(conn)

    result = await calendar_types.calendar_set(
        ctx,
        {
            "create": {"c1": {"name": "Projects"}},
            "update": {encode_calendar_id("/alice/work/"): {"name": "Old Work"}},
            "destroy": [encode_calendar_id("/alice/home/")],
        },
    )
    assert "c1" in result["created"]
    assert any(info["name"] == "Projects" for info in calendars.values())
    assert calendars["/alice/work/"]["name"] == "Old Work"
    assert "/alice/home/" not in calendars
    assert result["destroyed"] == [encode_calendar_id("/alice/home/")]
    assert result["notCreated"] == {}
    assert result["notUpdated"] == {}
    assert result["notDestroyed"] == {}


async def test_calendar_set_create_requires_name():
    conn = FakeCaldavConn(_default_calendars())
    ctx = FakeContext(conn)
    result = await calendar_types.calendar_set(ctx, {"create": {"c1": {}}})
    assert result["notCreated"]["c1"]["type"] == "invalidArguments"


async def test_calendar_set_update_keeps_same_id_after_rename():
    """The href (and therefore the id) never changes on a rename - unlike
    Mailbox, no id_redirect is needed, and `updated` must echo back the
    same id the client passed either way (RFC 8620 SS5.3).
    """
    calendars = _default_calendars()
    conn = FakeCaldavConn(calendars)
    ctx = FakeContext(conn)
    work_id = encode_calendar_id("/alice/work/")

    result = await calendar_types.calendar_set(ctx, {"update": {work_id: {"name": "New Name"}}})
    assert result["updated"] == {work_id: None}

    get_result = await calendar_types.calendar_get(ctx, {"ids": [work_id]})
    assert get_result["list"][0]["id"] == work_id
    assert get_result["list"][0]["name"] == "New Name"


async def test_calendar_set_destroy_unknown_id():
    conn = FakeCaldavConn(_default_calendars())
    ctx = FakeContext(conn)
    result = await calendar_types.calendar_set(ctx, {"destroy": ["Cnotreal!!!"]})
    assert "Cnotreal!!!" in result["notDestroyed"]
