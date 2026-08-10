"""CalDAV collection <-> JMAP Calendar (draft-ietf-jmap-calendars-26 SS4)
mapping.

JMAP Calendar ids are opaque strings the client round-trips; there's no
id-mapping table, the id is a deterministic encoding of the calendar's
CalDAV collection href (mirrors the Mailbox id approach in
mailbox_map.py). Unlike a Mailbox rename (which changes the IMAP name the
id is derived from), a Calendar rename is a PROPPATCH on DAV:displayname
- the href, and therefore the id, never changes - so unlike Mailbox,
Calendar never needs id_redirect.py.
"""

from __future__ import annotations

import base64

from jmap_bridge.backends.caldav.client import CalendarEntry
from jmap_bridge.webdav_common.href import canonicalize_collection_href


def canonicalize_calendar_href(href: str) -> str:
    """Calendar-flavored name for the shared collection-href
    canonicalizer (webdav_common/href.py) - confirmed live against
    Radicale that this matters for real: right after `make_calendar()`,
    a `@` in the path (from an email-address username) comes back
    percent-encoded (`alice%40example.com`), but a follow-up PROPFIND
    listing returns the literal `alice@example.com` for the identical
    resource. Without unquoting, a just-created calendar's id wouldn't
    match what a subsequent get computes for it - a real bug caught this
    way, not a hypothetical one.
    """
    return canonicalize_collection_href(href)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def encode_calendar_id(href: str) -> str:
    canonical = canonicalize_calendar_href(href)
    return "C" + _b64url_encode(canonical.encode("utf-8"))


def decode_calendar_id(calendar_id: str) -> str:
    """Inverse of encode_calendar_id. Raises ValueError on malformed
    input - callers should map that to a `notFound` result, not propagate
    it as a server error, since it just means the client handed back an
    id that isn't (or is no longer) one of ours.
    """
    if not calendar_id.startswith("C"):
        raise ValueError(f"not a Calendar id: {calendar_id!r}")
    try:
        return _b64url_decode(calendar_id[1:]).decode("utf-8")
    except Exception as exc:
        raise ValueError(f"malformed Calendar id {calendar_id!r}: {exc}") from exc


def build_jmap_calendar(entry: CalendarEntry) -> dict:
    """CalDAV collection -> JMAP Calendar object. `description`/`color`/
    `timeZone`/`shareWith` aren't populated (plain CalDAV has nowhere
    standard to store some of these, and this is intentionally the
    minimal v1 property set - see the plan's Deferred list); `isDefault`
    is always False (no reliable cross-server signal for "the" default
    calendar without vendor-specific properties).
    """
    return {
        "id": encode_calendar_id(entry.href),
        "name": entry.display_name or "",
        "description": None,
        "color": None,
        "sortOrder": 0,
        "isSubscribed": True,
        "isVisible": True,
        "isDefault": False,
        "includeInAvailability": "all",
        "defaultAlertsWithTime": None,
        "defaultAlertsWithoutTime": None,
        "timeZone": None,
        "shareWith": None,
        "myRights": {
            "mayReadFreeBusy": True,
            "mayReadItems": True,
            "mayWriteAll": True,
            "mayWriteOwn": True,
            "mayUpdatePrivate": True,
            "mayRSVP": True,
            "mayShare": False,
            "mayDelete": True,
        },
    }
