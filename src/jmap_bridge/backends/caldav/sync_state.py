"""CalDAV state-token payload: an account-wide vector of per-calendar
cursors.

Mirrors backends/imap/modseq_state.py's role for Mail: JMAP Calendar/
CalendarEvent state is account-wide, but a CalDAV sync-token is per
calendar collection - with no local database, the only zero-storage
account-wide cursor is the full set of per-calendar cursors, encoded into
the opaque state string itself. The Calendar id codec lives in
calendar_map.py instead (mirrors mailbox_map.py, not modseq_state.py -
Calendar is the Mailbox analog here, not the Email one).

A cursor is `(sync_token, display_name)`, not just a sync-token - confirmed
live against Radicale that renaming a calendar (a PROPPATCH on
DAV:displayname) does *not* change its sync-token, since RFC 6578
sync-tokens track *member resource* (event) changes, not the collection's
own properties. Diffing on sync-token alone would silently miss a pure
rename.
"""

from __future__ import annotations

from dataclasses import dataclass

from jmap_bridge.state import InvalidStateToken, decode_state, encode_state

STATE_KIND = "calendarstate"


class CannotCalculateChanges(Exception):
    """Raised when a changes diff can't be honestly computed from the
    given sinceState. Handlers should catch this and return the RFC 8620
    cannotCalculateChanges error rather than guessing.
    """


@dataclass(frozen=True, slots=True)
class CalendarCursor:
    sync_token: str | None
    display_name: str | None


@dataclass(frozen=True, slots=True)
class CalendarChanges:
    created: list[str]
    updated: list[str]
    destroyed: list[str]


def encode_calendar_state(cursors: dict[str, CalendarCursor]) -> str:
    payload = {
        "calendars": {
            href: {"st": c.sync_token, "dn": c.display_name} for href, c in cursors.items()
        }
    }
    return encode_state(STATE_KIND, payload)


def decode_calendar_state(token: str) -> dict[str, CalendarCursor]:
    payload = decode_state(token, STATE_KIND)
    calendars = payload.get("calendars")
    if not isinstance(calendars, dict):
        raise InvalidStateToken("calendar state payload missing 'calendars'")
    result: dict[str, CalendarCursor] = {}
    try:
        for href, cursor in calendars.items():
            result[href] = CalendarCursor(sync_token=cursor.get("st"), display_name=cursor.get("dn"))
    except (KeyError, TypeError, AttributeError) as exc:
        raise InvalidStateToken(f"malformed calendar cursor: {exc}") from exc
    return result


def diff_calendar_state(
    old: dict[str, CalendarCursor], new: dict[str, CalendarCursor]
) -> CalendarChanges:
    """Calendar/changes: a pure set-diff of calendar hrefs for created/
    destroyed. "updated" is any href present in both snapshots whose
    sync-token *or* display_name differs - the former covers the
    calendar's events changing, the latter covers the calendar's own
    properties (e.g. a rename) changing, since CalDAV has no single
    signal that covers both (see module docstring).
    """
    created = [href for href in new if href not in old]
    destroyed = [href for href in old if href not in new]
    updated = [href for href in new.keys() & old.keys() if new[href] != old[href]]
    return CalendarChanges(created=created, updated=updated, destroyed=destroyed)
