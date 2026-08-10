import re

import pytest

from jmap_bridge.backends.carddav.addressbook_map import (
    build_jmap_addressbook,
    canonicalize_addressbook_href,
    decode_addressbook_id,
    encode_addressbook_id,
)
from jmap_bridge.backends.carddav.client import AddressBookEntry


def test_addressbook_id_round_trip():
    addressbook_id = encode_addressbook_id("http://dav.example.com/alice/personal/")
    assert decode_addressbook_id(addressbook_id) == "/alice/personal/"


def test_addressbook_id_is_jmap_id_safe():
    addressbook_id = encode_addressbook_id("http://dav.example.com/alice/wörk book/")
    assert re.fullmatch(r"[A-Za-z0-9_-]+", addressbook_id)


def test_decode_addressbook_id_rejects_garbage():
    with pytest.raises(ValueError):
        decode_addressbook_id("not-an-addressbook-id")


def test_decode_addressbook_id_rejects_wrong_prefix():
    with pytest.raises(ValueError):
        decode_addressbook_id("Xabcdef")


def test_addressbook_id_does_not_collide_with_email_blob_id_prefix():
    """encode_blob_id (email_map.py) already uses "B" - AddressBook must
    use a distinct prefix so a garbled id from the wrong context fails
    decode cleanly instead of silently misparsing as a different type.
    """
    addressbook_id = encode_addressbook_id("http://dav.example.com/alice/personal/")
    assert not addressbook_id.startswith("B")


def test_canonicalize_strips_scheme_and_host():
    assert canonicalize_addressbook_href("https://dav.example.com/alice/personal/") == "/alice/personal/"


def test_canonicalize_adds_trailing_slash():
    assert canonicalize_addressbook_href("https://dav.example.com/alice/personal") == "/alice/personal/"


def test_canonicalize_unquotes_percent_encoding():
    """Regression test: confirmed live against Radicale that the *same*
    addressbook's href comes back percent-encoded right after creation
    (`alice%40example.com`) but unencoded from a subsequent PROPFIND
    listing (`alice@example.com`).
    """
    encoded_id = encode_addressbook_id("http://dav.example.com/alice%40example.com/personal/")
    decoded_id = encode_addressbook_id("http://dav.example.com/alice@example.com/personal/")
    assert encoded_id == decoded_id


def test_build_jmap_addressbook_maps_name():
    entry = AddressBookEntry(href="http://dav.example.com/alice/personal/", display_name="Personal", sync_token="tok1")
    addressbook = build_jmap_addressbook(entry)
    assert addressbook["id"] == encode_addressbook_id(entry.href)
    assert addressbook["name"] == "Personal"
    assert addressbook["isDefault"] is False


def test_build_jmap_addressbook_handles_missing_display_name():
    entry = AddressBookEntry(href="http://dav.example.com/alice/personal/", display_name=None, sync_token=None)
    addressbook = build_jmap_addressbook(entry)
    assert addressbook["name"] == ""
