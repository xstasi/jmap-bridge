import re

import pytest

from jmap_bridge.backends.caldav.calendar_map import (
    build_jmap_calendar,
    canonicalize_calendar_href,
    decode_calendar_id,
    encode_calendar_id,
)
from jmap_bridge.backends.caldav.client import CalendarEntry


def test_calendar_id_round_trip():
    calendar_id = encode_calendar_id("http://dav.example.com/alice/work/")
    assert decode_calendar_id(calendar_id) == "/alice/work/"


def test_calendar_id_is_jmap_id_safe():
    calendar_id = encode_calendar_id("http://dav.example.com/alice/wörk cal/")
    assert re.fullmatch(r"[A-Za-z0-9_-]+", calendar_id)


def test_decode_calendar_id_rejects_garbage():
    with pytest.raises(ValueError):
        decode_calendar_id("not-a-calendar-id")


def test_decode_calendar_id_rejects_wrong_prefix():
    with pytest.raises(ValueError):
        decode_calendar_id("Xabcdef")


def test_canonicalize_strips_scheme_and_host():
    assert canonicalize_calendar_href("https://dav.example.com/alice/work/") == "/alice/work/"


def test_canonicalize_adds_trailing_slash():
    assert canonicalize_calendar_href("https://dav.example.com/alice/work") == "/alice/work/"


def test_canonicalize_unquotes_percent_encoding():
    """Regression test: confirmed live against Radicale that the *same*
    calendar's href comes back percent-encoded right after creation
    (`alice%40example.com`) but unencoded from a subsequent PROPFIND
    listing (`alice@example.com`) - without unquoting, a just-created
    calendar's id wouldn't match what Calendar/get computes for it.
    """
    encoded_id = encode_calendar_id("http://dav.example.com/alice%40example.com/work/")
    decoded_id = encode_calendar_id("http://dav.example.com/alice@example.com/work/")
    assert encoded_id == decoded_id


def test_canonicalize_is_stable_across_absolute_and_relative_forms():
    """The same physical calendar must produce the same id whether the
    server handed back an absolute or relative href - confirmed live that
    CalDAV servers aren't always consistent about this.
    """
    absolute_id = encode_calendar_id("http://dav.example.com/alice/work/")
    relative_id = encode_calendar_id("/alice/work/")
    assert absolute_id == relative_id


def test_build_jmap_calendar_maps_name():
    entry = CalendarEntry(href="http://dav.example.com/alice/work/", display_name="Work", sync_token="tok1")
    calendar = build_jmap_calendar(entry)
    assert calendar["id"] == encode_calendar_id(entry.href)
    assert calendar["name"] == "Work"
    assert calendar["isDefault"] is False


def test_build_jmap_calendar_handles_missing_display_name():
    entry = CalendarEntry(href="http://dav.example.com/alice/work/", display_name=None, sync_token=None)
    calendar = build_jmap_calendar(entry)
    assert calendar["name"] == ""
