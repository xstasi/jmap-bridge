"""CardDAV collection <-> JMAP AddressBook (RFC 9610 SS2) mapping.

Mirrors calendar_map.py's Calendar mapping exactly: JMAP AddressBook ids
are opaque strings the client round-trips; there's no id-mapping table,
the id is a deterministic encoding of the addressbook's CardDAV
collection href. An AddressBook rename is a PROPPATCH on DAV:displayname
- the href, and therefore the id, never changes - so, like Calendar,
AddressBook never needs id_redirect.py.
"""

from __future__ import annotations

import base64

from jmap_bridge.backends.carddav.client import AddressBookEntry
from jmap_bridge.webdav_common.href import canonicalize_collection_href


def canonicalize_addressbook_href(href: str) -> str:
    return canonicalize_collection_href(href)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def encode_addressbook_id(href: str) -> str:
    # "D" - not "B", which encode_blob_id (email_map.py) already uses;
    # every JMAP id type in this bridge uses a distinct single-letter
    # prefix (A=account, B=email blob, C=calendar, E=email, M=mailbox,
    # T=thread, U=staged upload blob, V=calendarevent) so a garbled id
    # from the wrong context fails decode cleanly instead of silently
    # misparsing as a different type.
    canonical = canonicalize_addressbook_href(href)
    return "D" + _b64url_encode(canonical.encode("utf-8"))


def decode_addressbook_id(addressbook_id: str) -> str:
    """Inverse of encode_addressbook_id. Raises ValueError on malformed
    input - callers should map that to a `notFound` result, not propagate
    it as a server error.
    """
    if not addressbook_id.startswith("D"):
        raise ValueError(f"not an AddressBook id: {addressbook_id!r}")
    try:
        return _b64url_decode(addressbook_id[1:]).decode("utf-8")
    except Exception as exc:
        raise ValueError(f"malformed AddressBook id {addressbook_id!r}: {exc}") from exc


def build_jmap_addressbook(entry: AddressBookEntry) -> dict:
    """CardDAV collection -> JMAP AddressBook object (RFC 9610 SS2).
    `description`/`shareWith` aren't populated (plain CardDAV has nowhere
    standard to store these, and sharing is out of scope for this MVP -
    see the plan's Deferred list); `isDefault` is always False (no
    reliable cross-server signal for "the" default address book without
    vendor-specific properties).
    """
    return {
        "id": encode_addressbook_id(entry.href),
        "name": entry.display_name or "",
        "description": None,
        "sortOrder": 0,
        "isDefault": False,
        "isSubscribed": True,
        "shareWith": None,
        "myRights": {
            "mayRead": True,
            "mayWrite": True,
            "mayShare": False,
            "mayDelete": True,
        },
    }
