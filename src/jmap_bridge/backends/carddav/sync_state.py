"""CardDAV state-token payload: an account-wide vector of per-addressbook
cursors.

Mirrors backends/caldav/sync_state.py exactly: JMAP AddressBook state is
account-wide, but a CardDAV sync-token is per addressbook collection -
with no local database, the only zero-storage account-wide cursor is the
full set of per-addressbook cursors, encoded into the opaque state
string itself. The AddressBook id codec lives in addressbook_map.py
instead (mirrors calendar_map.py, not this module).

A cursor is `(sync_token, display_name)`, not just a sync-token - same
reasoning as Calendar's cursor: renaming a collection (a PROPPATCH on
DAV:displayname) doesn't bump its sync-token, since RFC 6578 sync-tokens
track *member resource* changes, not the collection's own properties.
"""

from __future__ import annotations

from dataclasses import dataclass

from jmap_bridge.state import InvalidStateToken, decode_state, encode_state

STATE_KIND = "addressbookstate"


class CannotCalculateChanges(Exception):
    """Raised when a changes diff can't be honestly computed from the
    given sinceState. Handlers should catch this and return the RFC 8620
    cannotCalculateChanges error rather than guessing.
    """


@dataclass(frozen=True, slots=True)
class AddressBookCursor:
    sync_token: str | None
    display_name: str | None


@dataclass(frozen=True, slots=True)
class AddressBookChanges:
    created: list[str]
    updated: list[str]
    destroyed: list[str]


def encode_addressbook_state(cursors: dict[str, AddressBookCursor]) -> str:
    payload = {
        "addressbooks": {
            href: {"st": c.sync_token, "dn": c.display_name} for href, c in cursors.items()
        }
    }
    return encode_state(STATE_KIND, payload)


def decode_addressbook_state(token: str) -> dict[str, AddressBookCursor]:
    payload = decode_state(token, STATE_KIND)
    addressbooks = payload.get("addressbooks")
    if not isinstance(addressbooks, dict):
        raise InvalidStateToken("addressbook state payload missing 'addressbooks'")
    result: dict[str, AddressBookCursor] = {}
    try:
        for href, cursor in addressbooks.items():
            result[href] = AddressBookCursor(sync_token=cursor.get("st"), display_name=cursor.get("dn"))
    except (KeyError, TypeError, AttributeError) as exc:
        raise InvalidStateToken(f"malformed addressbook cursor: {exc}") from exc
    return result


def diff_addressbook_state(
    old: dict[str, AddressBookCursor], new: dict[str, AddressBookCursor]
) -> AddressBookChanges:
    """AddressBook/changes: a pure set-diff of addressbook hrefs for
    created/destroyed. "updated" is any href present in both snapshots
    whose sync-token *or* display_name differs.
    """
    created = [href for href in new if href not in old]
    destroyed = [href for href in old if href not in new]
    updated = [href for href in new.keys() & old.keys() if new[href] != old[href]]
    return AddressBookChanges(created=created, updated=updated, destroyed=destroyed)
