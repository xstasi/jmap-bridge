import pytest

from jmap_bridge.backends.carddav.sync_state import (
    AddressBookCursor,
    decode_addressbook_state,
    diff_addressbook_state,
    encode_addressbook_state,
)
from jmap_bridge.state import InvalidStateToken


def test_addressbook_state_round_trip():
    cursors = {
        "/alice/personal/": AddressBookCursor(sync_token="tok1", display_name="Personal"),
        "/alice/work/": AddressBookCursor(sync_token="tok2", display_name="Work"),
    }
    token = encode_addressbook_state(cursors)
    assert decode_addressbook_state(token) == cursors


def test_decode_addressbook_state_rejects_garbage():
    with pytest.raises(InvalidStateToken):
        decode_addressbook_state("not-a-real-token")


def test_diff_detects_created_updated_destroyed():
    old = {
        "/alice/personal/": AddressBookCursor("tok1", "Personal"),
        "/alice/trash/": AddressBookCursor("tokX", "Trash"),
    }
    new = {
        "/alice/personal/": AddressBookCursor("tok1-new", "Personal"),  # sync-token changed
        "/alice/work/": AddressBookCursor("tok2", "Work"),  # created
        # trash destroyed
    }
    diff = diff_addressbook_state(old, new)
    assert diff.created == ["/alice/work/"]
    assert diff.updated == ["/alice/personal/"]
    assert diff.destroyed == ["/alice/trash/"]


def test_diff_detects_rename_with_unchanged_sync_token():
    old = {"/alice/personal/": AddressBookCursor("tok1", "Personal")}
    new = {"/alice/personal/": AddressBookCursor("tok1", "Personal Renamed")}
    diff = diff_addressbook_state(old, new)
    assert diff.updated == ["/alice/personal/"]


def test_diff_no_changes():
    cursors = {"/alice/personal/": AddressBookCursor("tok1", "Personal")}
    diff = diff_addressbook_state(cursors, dict(cursors))
    assert diff.created == diff.updated == diff.destroyed == []
