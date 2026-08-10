"""Shared WebDAV href canonicalization - used by both `backends/caldav/`
and `backends/carddav/` id encoding (calendar_map.py, addressbook_map.py,
event_map.py, card_map.py), since it's generic DAV-level behavior, not
specific to either protocol.

Confirmed live, independently, against Radicale for *both* CalDAV and
CardDAV collections: the same physical resource's href comes back
percent-encoded (`alice%40example.com`) immediately after creation but
unencoded (`alice@example.com`) from a subsequent PROPFIND listing -
without unquoting, a just-created object's id wouldn't match what a
later `Foo/get` computes for it.
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit


def canonicalize_href_path(href: str) -> str:
    """Strip scheme+host and percent-decode, so the same resource
    produces a byte-identical path regardless of which code path
    produced the href.
    """
    return unquote(urlsplit(href).path)


def canonicalize_collection_href(href: str) -> str:
    """Like `canonicalize_href_path`, plus normalizing a trailing slash -
    a collection (Calendar/AddressBook) href is conventionally
    slash-terminated; unlike a member resource's href (Event/Card),
    which is a single file-like resource and shouldn't get one forced on.
    """
    path = canonicalize_href_path(href)
    if not path.endswith("/"):
        path += "/"
    return path
