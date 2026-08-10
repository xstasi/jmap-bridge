import pytest

from jmap_bridge.backends.caldav.sync_state import (
    CalendarCursor,
    decode_calendar_state,
    diff_calendar_state,
    encode_calendar_state,
)
from jmap_bridge.state import InvalidStateToken


def test_calendar_state_round_trip():
    cursors = {
        "/alice/work/": CalendarCursor(sync_token="tok1", display_name="Work"),
        "/alice/home/": CalendarCursor(sync_token="tok2", display_name="Home"),
    }
    token = encode_calendar_state(cursors)
    assert decode_calendar_state(token) == cursors


def test_decode_calendar_state_rejects_garbage():
    with pytest.raises(InvalidStateToken):
        decode_calendar_state("not-a-real-token")


def test_diff_detects_created_updated_destroyed():
    old = {
        "/alice/work/": CalendarCursor("tok1", "Work"),
        "/alice/trash/": CalendarCursor("tokX", "Trash"),
    }
    new = {
        "/alice/work/": CalendarCursor("tok1-new", "Work"),  # sync-token changed
        "/alice/home/": CalendarCursor("tok2", "Home"),  # created
        # trash destroyed
    }
    diff = diff_calendar_state(old, new)
    assert diff.created == ["/alice/home/"]
    assert diff.updated == ["/alice/work/"]
    assert diff.destroyed == ["/alice/trash/"]


def test_diff_detects_rename_with_unchanged_sync_token():
    """Regression test: confirmed live against Radicale that renaming a
    calendar does NOT change its sync-token (RFC 6578 tokens track member
    resources, not collection properties) - diffing on sync-token alone
    would silently miss this.
    """
    old = {"/alice/work/": CalendarCursor("tok1", "Work")}
    new = {"/alice/work/": CalendarCursor("tok1", "Work Renamed")}
    diff = diff_calendar_state(old, new)
    assert diff.updated == ["/alice/work/"]


def test_diff_no_changes():
    cursors = {"/alice/work/": CalendarCursor("tok1", "Work")}
    diff = diff_calendar_state(cursors, dict(cursors))
    assert diff.created == diff.updated == diff.destroyed == []
