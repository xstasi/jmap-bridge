"""CalDAV VEVENT <-> JMAP CalendarEvent (JSCalendar Event,
draft-ietf-calext-jscalendarbis-15) mapping.

Recurrence scope (see the Phase 2 plan): a single `recurrenceRule` (the
current JSCalendar spec obsoletes the older plural form) with the common
RRULE parts only (FREQ/INTERVAL/COUNT/UNTIL/BYDAY/BYMONTHDAY/BYMONTH -
`_recurrence_rule_from_vrecur`/`_recurrence_rule_to_vrecur`), plus
`recurrenceOverrides`' `excluded` (EXDATE-equivalent) and property-patch
(RECURRENCE-ID-equivalent) forms - the RDATE-equivalent case (a
recurrence-id not matching the rule at all, i.e. an added one-off
occurrence) is deferred, since most calendar UIs create a standalone
event for that instead.

CalendarEvent id = deterministic encoding of `(calendar_href, event_href)`
(mirrors Email id = encoding of `(mailbox, uidvalidity, uid)`) - decoding
gives exactly what's needed to GET/PUT/DELETE the resource directly, no
lookup table for the common (never-moved) case. A moved event
(`calendarIds` changed) is kept resolvable via id_redirect.py, the same
mechanism as Email's mailboxIds move (see types/calendar_event.py).

Write-path round-trip fidelity: `apply_jscalendar_patch` mutates the
parsed `icalendar.Calendar` in place, touching only the properties the
JMAP patch actually changed - never rebuilds the VEVENT from scratch on
an update, so VALARM/ATTENDEE/CATEGORIES/X- properties outside this
file's v1 property subset survive an update untouched. This is the
CalDAV-bridge equivalent of Email/set never rewriting a message body
wholesale, only touching flags/mailboxIds.

`recurrenceOverrides` are read back as a *diff* between each override
VEVENT and the master's current property values (see
`_override_patch_from_vevent`), since a real RECURRENCE-ID VEVENT is a
complete standalone component per RFC 5545, not a literal stored patch -
this file computes the minimal patch itself. Known edge case from this
approach: iCalendar's override model doesn't distinguish "this occurrence
explicitly clears property X" from "X was never set on this occurrence
at all," so editing the *master's* properties independently after
overrides already exist can make an untouched override's reported patch
gain or lose an entry for that property on the next read (its stored
value didn't change, but the baseline it's diffed against did). Narrow
and not expected to bite common usage (create overrides after the
master's properties are settled), documented rather than solved for v1.

Deferred (not modeled at all in this file, v1 scope): virtualLocations,
recurrenceId/recurrenceIdTimeZone, absolute (DATE-TIME) VALARM triggers
(only offset triggers are mapped), multi-location (`locations` supports
at most one entry), embedding a VTIMEZONE block for non-UTC timezones
(relies on the recipient having the IANA tzdata for a bare TZID
reference - confirmed this works against Radicale in practice),
organizerCalendarAddress as a distinct top-level property (the
`participants["organizer"]` entry already covers the same information),
`CalendarEvent/parse` (spec method for parsing a raw blob into
JSCalendar without creating it - Bulwark webmail uses this for .ics file
import; this module's own extraction logic could back it directly if
built later).

`calendarIds` is capped at exactly one entry (see session.py's
`maxCalendarsPerEvent: 1` and types/calendar_event.py's `_create_event`/
`_apply_event_update`) - CalDAV has no native multi-collection-membership
primitive, unlike IMAP COPY for Email's analogous case. Confirmed this is
a real, not just theoretical, gap: Bulwark webmail's calendar store
genuinely treats `calendarIds` as multi-valued (linking an event into a
second calendar without removing it from the first, unlinking by
removing just one key while keeping others) - a client actually doing
that against this bridge gets a hard `invalidProperties` error instead.
Flagged for a decision rather than silently worked around, the same way
Email's id-stability-across-move question was raised explicitly earlier
in this project - extending this to real multi-membership would need
Email-style fan-out (PUT the same VEVENT into each target calendar,
independent hrefs) without CalDAV's IMAP-COPY-equivalent identity
tracking.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import date, datetime, timedelta, timezone

import icalendar

from jmap_bridge.backends.caldav.calendar_map import canonicalize_calendar_href
from jmap_bridge.webdav_common.href import canonicalize_href_path

_STATUS_TO_JSCAL = {"CONFIRMED": "confirmed", "CANCELLED": "cancelled", "TENTATIVE": "tentative"}
_JSCAL_TO_STATUS = {v: k for k, v in _STATUS_TO_JSCAL.items()}

_TRANSP_TO_FREEBUSY = {"OPAQUE": "busy", "TRANSPARENT": "free"}
_FREEBUSY_TO_TRANSP = {v: k for k, v in _TRANSP_TO_FREEBUSY.items()}

_CLASS_TO_PRIVACY = {"PUBLIC": "public", "PRIVATE": "private", "CONFIDENTIAL": "secret"}
_PRIVACY_TO_CLASS = {v: k for k, v in _CLASS_TO_PRIVACY.items()}

_PARTSTAT_TO_STATUS = {
    "NEEDS-ACTION": "needs-action",
    "ACCEPTED": "accepted",
    "DECLINED": "declined",
    "TENTATIVE": "tentative",
    "DELEGATED": "delegated",
}
_STATUS_TO_PARTSTAT = {v: k for k, v in _PARTSTAT_TO_STATUS.items()}

_ACTION_TO_JSCAL = {"DISPLAY": "display", "EMAIL": "email", "AUDIO": "display"}
_JSCAL_TO_ACTION = {"display": "DISPLAY", "email": "EMAIL"}

_DATETIME_CLUSTER = {"start", "duration", "timeZone", "showWithoutTime"}

_FREQ_TO_JSCAL = {
    "SECONDLY": "secondly",
    "MINUTELY": "minutely",
    "HOURLY": "hourly",
    "DAILY": "daily",
    "WEEKLY": "weekly",
    "MONTHLY": "monthly",
    "YEARLY": "yearly",
}
_JSCAL_TO_FREQ = {v: k for k, v in _FREQ_TO_JSCAL.items()}

_WEEKDAY_TO_JSCAL = {"MO": "mo", "TU": "tu", "WE": "we", "TH": "th", "FR": "fr", "SA": "sa", "SU": "su"}
_JSCAL_TO_WEEKDAY = {v: k for k, v in _WEEKDAY_TO_JSCAL.items()}


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def encode_event_id(calendar_href: str, event_href: str) -> str:
    calendar_path = canonicalize_calendar_href(calendar_href)
    event_path = canonicalize_href_path(event_href)
    raw = json.dumps([calendar_path, event_path], separators=(",", ":")).encode("utf-8")
    return "V" + _b64url_encode(raw)


def decode_event_id(event_id: str) -> tuple[str, str]:
    """Inverse of encode_event_id. Raises ValueError on malformed input -
    callers should map that to a `notFound` result, not propagate it as
    a server error.
    """
    if not event_id.startswith("V"):
        raise ValueError(f"not a CalendarEvent id: {event_id!r}")
    try:
        calendar_href, event_href = json.loads(_b64url_decode(event_id[1:]))
        return str(calendar_href), str(event_href)
    except Exception as exc:
        raise ValueError(f"malformed CalendarEvent id {event_id!r}: {exc}") from exc


def _local_datetime_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_local_datetime(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise ValueError(f"invalid LocalDateTime {s!r}: {exc}") from exc


def _start_and_timezone(dtstart_prop) -> tuple[str, str | None, bool]:
    """Returns (start_local_datetime_str, timeZone_or_None, showWithoutTime)."""
    value = dtstart_prop.dt
    if isinstance(value, datetime):
        tzid = dtstart_prop.params.get("TZID")
        if tzid:
            time_zone = str(tzid)
        elif value.tzinfo is not None:
            time_zone = "Etc/UTC"
        else:
            time_zone = None
        return _local_datetime_str(value.replace(tzinfo=None)), time_zone, False
    if isinstance(value, date):
        return f"{value.isoformat()}T00:00:00", None, True
    raise ValueError(f"unexpected DTSTART value type: {type(value)!r}")


def _duration_str(start_value, dtend_prop, duration_prop) -> str:
    if duration_prop is not None:
        td = duration_prop.dt
    elif dtend_prop is not None:
        td = dtend_prop.dt - start_value
    else:
        td = timedelta(0)
    return icalendar.vDuration(td).to_ical().decode("ascii")


def _resolve_timezone(time_zone: str):
    if time_zone == "Etc/UTC":
        return timezone.utc
    import zoneinfo

    return zoneinfo.ZoneInfo(time_zone)


def _to_ical_value(local_str: str, time_zone: str | None, show_without_time: bool):
    """JSCalendar (LocalDateTime, timeZone, showWithoutTime) -> the
    `date`/`datetime` value icalendar expects for DTSTART/DTEND/
    RECURRENCE-ID/EXDATE - the same conversion `_apply_datetime_cluster`
    needs for the master event's own DTSTART, and recurrenceOverrides
    handling needs for EXDATE/RECURRENCE-ID values (an override's
    recurrence-id key is always in the *master's* timeZone - see the
    module docstring / the plan's recurrenceOverrides gotchas).
    """
    naive = _parse_local_datetime(local_str)
    if show_without_time:
        return naive.date()
    if time_zone:
        return naive.replace(tzinfo=_resolve_timezone(time_zone))
    return naive


def _participant_from_prop(prop, *, is_organizer: bool) -> tuple[str, dict]:
    email = str(prop)
    if email.upper().startswith("MAILTO:"):
        email = email[len("MAILTO:") :]
    name = prop.params.get("CN")
    partstat = prop.params.get("PARTSTAT")
    if partstat:
        participation_status = _PARTSTAT_TO_STATUS.get(str(partstat).upper(), "needs-action")
    else:
        participation_status = "accepted" if is_organizer else "needs-action"
    rsvp = prop.params.get("RSVP")
    expect_reply = bool(rsvp) and str(rsvp).upper() == "TRUE"
    roles = {"owner": True} if is_organizer else {"attendee": True}
    participant_id = "organizer" if is_organizer else (email or "attendee")
    return participant_id, {
        "name": str(name) if name else None,
        "email": email,
        "kind": None,
        "roles": roles,
        "participationStatus": participation_status,
        "expectReply": expect_reply,
    }


def _participants_from_vevent(vevent) -> dict | None:
    participants: dict[str, dict] = {}
    organizer = vevent.get("organizer")
    if organizer is not None:
        pid, p = _participant_from_prop(organizer, is_organizer=True)
        participants[pid] = p
    attendees = vevent.get("attendee")
    if attendees is not None:
        if not isinstance(attendees, list):
            attendees = [attendees]
        for i, att in enumerate(attendees):
            pid, p = _participant_from_prop(att, is_organizer=False)
            if pid in participants:
                pid = f"{pid}-{i}"
            participants[pid] = p
    return participants or None


def _alerts_from_vevent(vevent) -> dict | None:
    alerts: dict[str, dict] = {}
    for i, valarm in enumerate(vevent.walk("VALARM")):
        trigger = valarm.get("trigger")
        if trigger is None:
            continue
        trigger_value = trigger.dt
        if not isinstance(trigger_value, timedelta):
            continue  # absolute (DATE-TIME) trigger - deferred, see module docstring
        related = trigger.params.get("RELATED", "START")
        offset = icalendar.vDuration(trigger_value).to_ical().decode("ascii")
        action_prop = valarm.get("action")
        action_raw = str(action_prop) if action_prop is not None else "DISPLAY"
        action = _ACTION_TO_JSCAL.get(action_raw.upper(), "display")
        alerts[f"alert{i + 1}"] = {
            "trigger": {
                "@type": "OffsetTrigger",
                "offset": offset,
                "relativeTo": "end" if str(related).upper() == "END" else "start",
            },
            "acknowledged": None,
            "action": action,
        }
    return alerts or None


def _extract_event_props(vevent) -> dict:
    """The v1 JSCalendar property subset for one VEVENT component - shared
    by the master event (`ical_to_jscalendar_event`) and by
    `_override_patch_from_vevent`'s diff-against-master logic for
    `recurrenceOverrides` (both need the identical extraction, just
    compared against different baselines).
    """
    dtstart = vevent.get("dtstart")
    if dtstart is None:
        raise ValueError("VEVENT missing required DTSTART")
    dtend = vevent.get("dtend")
    duration_prop = vevent.get("duration")
    start_str, time_zone, show_without_time = _start_and_timezone(dtstart)
    duration = _duration_str(dtstart.dt, dtend, duration_prop)

    status_prop = vevent.get("status")
    status = _STATUS_TO_JSCAL.get(str(status_prop).upper(), "confirmed") if status_prop else "confirmed"
    transp_prop = vevent.get("transp")
    free_busy_status = (
        _TRANSP_TO_FREEBUSY.get(str(transp_prop).upper(), "busy") if transp_prop else "busy"
    )
    class_prop = vevent.get("class")
    privacy = _CLASS_TO_PRIVACY.get(str(class_prop).upper()) if class_prop else None
    priority_prop = vevent.get("priority")
    priority = int(priority_prop) if priority_prop is not None else None
    description_prop = vevent.get("description")
    location_prop = vevent.get("location")
    locations = {"loc1": {"name": str(location_prop)}} if location_prop else None

    return {
        "title": str(vevent.get("summary")) if vevent.get("summary") is not None else "",
        "description": str(description_prop) if description_prop is not None else None,
        "start": start_str,
        "duration": duration,
        "timeZone": time_zone,
        "showWithoutTime": show_without_time,
        "status": status,
        "freeBusyStatus": free_busy_status,
        "priority": priority,
        "privacy": privacy,
        "locations": locations,
    }


def _parse_byday(value: str) -> dict:
    import re

    match = re.match(r"([+-]?\d+)?([A-Za-z]{2})$", str(value))
    if not match:
        raise ValueError(f"invalid BYDAY value: {value!r}")
    n, day = match.groups()
    result: dict = {"day": _WEEKDAY_TO_JSCAL.get(day.upper(), day.lower())}
    if n:
        result["nthOfPeriod"] = int(n)
    return result


def _format_byday(entry: dict) -> str:
    day = _JSCAL_TO_WEEKDAY.get(entry.get("day"), str(entry.get("day", "")).upper())
    n = entry.get("nthOfPeriod")
    return f"{n}{day}" if n is not None else day


def _recurrence_rule_from_vrecur(rrule, time_zone: str | None) -> dict:
    """RRULE -> JSCalendar RecurrenceRule (jscalendarbis-15 SS3.3.3),
    common parts only (FREQ/INTERVAL/COUNT/UNTIL/BYDAY/BYMONTHDAY/
    BYMONTH) - see the module docstring's v1 scope note.
    """
    freq_raw = str(rrule.get("FREQ", ["DAILY"])[0])
    result: dict = {"@type": "RecurrenceRule", "frequency": _FREQ_TO_JSCAL.get(freq_raw, "daily")}
    if "INTERVAL" in rrule:
        result["interval"] = int(rrule["INTERVAL"][0])
    if "COUNT" in rrule:
        result["count"] = int(rrule["COUNT"][0])
    if "UNTIL" in rrule:
        until_value = rrule["UNTIL"][0]
        if isinstance(until_value, datetime):
            if time_zone and time_zone != "Etc/UTC":
                until_value = until_value.astimezone(_resolve_timezone(time_zone))
            result["until"] = _local_datetime_str(until_value.replace(tzinfo=None))
        else:
            result["until"] = f"{until_value.isoformat()}T00:00:00"
    if "BYDAY" in rrule:
        result["byDay"] = [_parse_byday(v) for v in rrule["BYDAY"]]
    if "BYMONTHDAY" in rrule:
        result["byMonthDay"] = [int(v) for v in rrule["BYMONTHDAY"]]
    if "BYMONTH" in rrule:
        result["byMonth"] = [str(v) for v in rrule["BYMONTH"]]
    return result


def _recurrence_rule_to_vrecur(rule: dict, time_zone: str | None) -> dict:
    freq = _JSCAL_TO_FREQ.get(rule.get("frequency"), "DAILY")
    result: dict = {"FREQ": [freq]}
    if "interval" in rule:
        result["INTERVAL"] = [rule["interval"]]
    if "count" in rule:
        result["COUNT"] = [rule["count"]]
    if "until" in rule:
        until_dt = _parse_local_datetime(rule["until"])
        if time_zone:
            until_dt = until_dt.replace(tzinfo=_resolve_timezone(time_zone))
            if time_zone != "Etc/UTC":
                until_dt = until_dt.astimezone(timezone.utc)
        result["UNTIL"] = [until_dt]
    if "byDay" in rule:
        result["BYDAY"] = [_format_byday(e) for e in rule["byDay"]]
    if "byMonthDay" in rule:
        result["BYMONTHDAY"] = [int(v) for v in rule["byMonthDay"]]
    if "byMonth" in rule:
        result["BYMONTH"] = [int(v) for v in rule["byMonth"]]
    return result


def _recurrence_id_key(recurrence_id_prop, master_time_zone: str | None) -> str:
    """The LocalDateTime key an override/exclusion is filed under -
    always expressed in the *master* event's timeZone (jscalendarbis-15
    SS3.3.4), which may differ from the override's own TZID - confirmed
    as a real gotcha to get right in the plan's design review.
    """
    value = recurrence_id_prop.dt
    if isinstance(value, datetime):
        if master_time_zone and master_time_zone != "Etc/UTC":
            value = value.astimezone(_resolve_timezone(master_time_zone))
        elif value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return _local_datetime_str(value.replace(tzinfo=None))
    return f"{value.isoformat()}T00:00:00"


def _override_patch_from_vevent(override_vevent, master_props: dict, recurrence_key: str) -> dict:
    """A minimal PatchObject: only the v1-tracked properties that differ
    from what's expected for this occurrence if nothing had changed - a
    real RECURRENCE-ID VEVENT is a complete standalone component per RFC
    5545 (not a literal diff), so this file computes the diff itself to
    produce a properly minimal JSCalendar patch rather than echoing back
    every property unconditionally. `start` is compared against this
    occurrence's own natural (recurrence-id-derived) start, not the
    master's - the master's `start` is only ever its first occurrence, so
    diffing against it would make every override falsely claim a moved
    start (jscalendarbis-15 SS3.3.4: an override with no `start` means
    the occurrence's start is exactly what the rule would produce).
    """
    baseline = {**master_props, "start": recurrence_key}
    override_props = _extract_event_props(override_vevent)
    patch: dict = {}
    for key, value in override_props.items():
        if baseline.get(key) != value:
            patch[key] = value
    return patch


def _recurrence_overrides_from_vevents(master_vevent, other_vevents: list, master_props: dict, time_zone: str | None) -> dict | None:
    overrides: dict[str, dict] = {}
    exdates = master_vevent.get("exdate")
    if exdates is not None:
        if not isinstance(exdates, list):
            exdates = [exdates]
        for exdate_prop in exdates:
            for value in exdate_prop.dts:
                key = _recurrence_id_key(value, time_zone)
                overrides[key] = {"excluded": True}
    for override_vevent in other_vevents:
        recurrence_id = override_vevent.get("recurrence-id")
        if recurrence_id is None:
            continue  # no matching master concept here - shouldn't happen, skip rather than crash
        key = _recurrence_id_key(recurrence_id, time_zone)
        try:
            overrides[key] = _override_patch_from_vevent(override_vevent, master_props, key)
        except ValueError:
            continue  # malformed override component - skip rather than crash the whole event
    return overrides or None


def ical_to_jscalendar_event(ical_text: str, event_id: str, calendar_id: str) -> dict:
    cal = icalendar.Calendar.from_ical(ical_text)
    all_vevents = cal.walk("VEVENT")
    if not all_vevents:
        raise ValueError("no VEVENT found in calendar object")
    # The master is the one VEVENT without RECURRENCE-ID - RECURRENCE-ID
    # VEVENTs (override instances) share the same UID but are handled
    # separately, via _recurrence_overrides_from_vevents below.
    masters = [v for v in all_vevents if v.get("recurrence-id") is None]
    vevent = masters[0] if masters else all_vevents[0]
    other_vevents = [v for v in all_vevents if v is not vevent]

    uid_prop = vevent.get("uid")
    props = _extract_event_props(vevent)

    result = {
        "id": event_id,
        "calendarIds": {calendar_id: True},
        "uid": str(uid_prop) if uid_prop is not None else "",
        **props,
        "participants": _participants_from_vevent(vevent),
        "alerts": _alerts_from_vevent(vevent),
    }

    rrule = vevent.get("rrule")
    if rrule is not None:
        result["recurrenceRule"] = _recurrence_rule_from_vrecur(rrule, props["timeZone"])
        result["recurrenceOverrides"] = _recurrence_overrides_from_vevents(
            vevent, other_vevents, props, props["timeZone"]
        )
    return result


def _apply_datetime_cluster(vevent, props: dict) -> None:
    existing_dtstart = vevent.get("dtstart")
    if existing_dtstart is not None:
        existing_start_str, existing_tz, existing_show_without_time = _start_and_timezone(
            existing_dtstart
        )
    else:
        existing_start_str, existing_tz, existing_show_without_time = None, None, False

    start_str = props["start"] if "start" in props else existing_start_str
    if start_str is None:
        raise ValueError("start is required")
    time_zone = props["timeZone"] if "timeZone" in props else existing_tz
    show_without_time = (
        props["showWithoutTime"] if "showWithoutTime" in props else existing_show_without_time
    )

    if "duration" in props:
        duration_str = props["duration"]
    elif existing_dtstart is not None:
        duration_str = _duration_str(existing_dtstart.dt, vevent.get("dtend"), vevent.get("duration"))
    else:
        duration_str = "PT0S"
    duration_td = icalendar.vDuration.from_ical(duration_str)

    for key in ("dtstart", "dtend", "duration"):
        if key in vevent:
            del vevent[key]

    start_value = _to_ical_value(start_str, time_zone, show_without_time)
    vevent.add("dtstart", start_value)
    vevent.add("dtend", start_value + duration_td)


def _apply_participants(vevent, participants: dict) -> None:
    for key in ("organizer", "attendee"):
        if key in vevent:
            del vevent[key]
    for _pid, p in participants.items():
        email = p.get("email")
        if not email:
            continue
        roles = p.get("roles") or {}
        params = {}
        if p.get("name"):
            params["CN"] = p["name"]
        if roles.get("owner"):
            vevent.add("organizer", f"mailto:{email}", parameters=params)
            continue
        partstat = _STATUS_TO_PARTSTAT.get(p.get("participationStatus"), "NEEDS-ACTION")
        params["PARTSTAT"] = partstat
        params["RSVP"] = "TRUE" if p.get("expectReply") else "FALSE"
        vevent.add("attendee", f"mailto:{email}", parameters=params)


def _apply_alerts(vevent, alerts: dict) -> None:
    for sub in list(vevent.subcomponents):
        if sub.name == "VALARM":
            vevent.subcomponents.remove(sub)
    for _aid, alert in alerts.items():
        trigger_spec = alert.get("trigger") or {}
        if trigger_spec.get("@type") != "OffsetTrigger":
            continue  # absolute triggers deferred, see module docstring
        offset_td = icalendar.vDuration.from_ical(trigger_spec["offset"])
        valarm = icalendar.Alarm()
        params = {}
        if trigger_spec.get("relativeTo") == "end":
            params["RELATED"] = "END"
        valarm.add("trigger", offset_td, parameters=params)
        valarm.add("action", _JSCAL_TO_ACTION.get(alert.get("action"), "DISPLAY"))
        vevent.add_component(valarm)


def _apply_jscalendar_props(vevent, props: dict) -> None:
    if "title" in props:
        vevent["summary"] = props["title"] or ""

    if "description" in props:
        if "description" in vevent:
            del vevent["description"]
        if props["description"]:
            vevent.add("description", props["description"])

    if _DATETIME_CLUSTER & props.keys():
        _apply_datetime_cluster(vevent, props)

    if "recurrenceRule" in props:
        if "rrule" in vevent:
            del vevent["rrule"]
        if props["recurrenceRule"]:
            dtstart = vevent.get("dtstart")
            _, current_tz, _ = _start_and_timezone(dtstart) if dtstart is not None else (None, None, False)
            vevent.add("rrule", _recurrence_rule_to_vrecur(props["recurrenceRule"], current_tz))

    if "status" in props:
        if "status" in vevent:
            del vevent["status"]
        vevent.add("status", _JSCAL_TO_STATUS.get(props["status"], "CONFIRMED"))

    if "freeBusyStatus" in props:
        if "transp" in vevent:
            del vevent["transp"]
        vevent.add("transp", _FREEBUSY_TO_TRANSP.get(props["freeBusyStatus"], "OPAQUE"))

    if "privacy" in props:
        if "class" in vevent:
            del vevent["class"]
        if props["privacy"]:
            vevent.add("class", _PRIVACY_TO_CLASS.get(props["privacy"], "PUBLIC"))

    if "priority" in props:
        if "priority" in vevent:
            del vevent["priority"]
        if props["priority"] is not None:
            vevent.add("priority", props["priority"])

    if "locations" in props:
        if "location" in vevent:
            del vevent["location"]
        locations = props["locations"] or {}
        first = next(iter(locations.values()), None)
        if first and first.get("name"):
            vevent.add("location", first["name"])

    if "participants" in props:
        _apply_participants(vevent, props["participants"] or {})

    if "alerts" in props:
        _apply_alerts(vevent, props["alerts"] or {})


def _apply_recurrence_overrides(cal, master_vevent, overrides: dict) -> None:
    """`recurrenceOverrides` is replaced wholesale when the patch names
    it (JMAP patches are property-level, not deep-merged) - remove every
    existing EXDATE and override VEVENT component first, then rebuild
    from `overrides`. Only the `excluded` (EXDATE-equivalent) and
    property-patch (RECURRENCE-ID-equivalent) forms are supported - see
    the module docstring for the deferred RDATE-equivalent (added
    one-off occurrence) case.
    """
    if "exdate" in master_vevent:
        del master_vevent["exdate"]
    master_uid = str(master_vevent.get("uid"))
    for sub in [s for s in cal.subcomponents if s.name == "VEVENT" and s is not master_vevent]:
        if str(sub.get("uid")) == master_uid and sub.get("recurrence-id") is not None:
            cal.subcomponents.remove(sub)

    if not overrides:
        return

    dtstart = master_vevent.get("dtstart")
    _, master_time_zone, master_show_without_time = _start_and_timezone(dtstart)
    master_props = _extract_event_props(master_vevent)

    exclude_values = []
    for key, patch in overrides.items():
        recurrence_value = _to_ical_value(key, master_time_zone, master_show_without_time)
        if patch == {"excluded": True}:
            exclude_values.append(recurrence_value)
            continue
        override_vevent = icalendar.Event()
        override_vevent.add("uid", master_uid)
        override_vevent.add("recurrence-id", recurrence_value)
        override_vevent.add("dtstamp", datetime.now(timezone.utc))
        # Baseline = master's other properties + this occurrence's own
        # natural (unmodified) start; an explicit "start" in the patch
        # (a moved occurrence) wins over that baseline - see the plan's
        # recurrenceOverrides gotchas.
        merged = {**master_props, "start": key, **patch}
        _apply_jscalendar_props(override_vevent, merged)
        cal.add_component(override_vevent)

    if exclude_values:
        master_vevent.add("exdate", exclude_values)


def build_vevent_ical(props: dict, uid: str | None = None) -> str:
    """Build a full VCALENDAR/VEVENT from JMAP CalendarEvent creation
    properties. `uid` - if not given, a fresh uuid4 is minted; this
    becomes part of the event's href for the object's entire lifetime
    (see backends/caldav/client.py's create_event).
    """
    cal = icalendar.Calendar()
    cal.add("prodid", "-//jmap-bridge//EN")
    cal.add("version", "2.0")
    vevent = icalendar.Event()
    event_uid = uid or str(uuid.uuid4())
    vevent.add("uid", event_uid)
    vevent.add("dtstamp", datetime.now(timezone.utc))
    if "start" not in props:
        raise ValueError("start is required")
    _apply_jscalendar_props(vevent, props)
    cal.add_component(vevent)
    if "recurrenceOverrides" in props:
        _apply_recurrence_overrides(cal, vevent, props.get("recurrenceOverrides") or {})
    return cal.to_ical().decode("utf-8")


def _unescape_json_pointer_segment(segment: str) -> str:
    return segment.replace("~1", "/").replace("~0", "~")


def _merge_patch_keys(existing: dict, patch: dict, prefix: str) -> dict:
    """Merge `<prefix>/<id>` and `<prefix>/<id>/<subkey>` patch keys into
    a copy of `existing` (the full current map for that top-level
    property). Real clients patch a single map *entry* this way instead
    of replacing the whole map - confirmed live in Bulwark webmail's
    source for both `recurrenceOverrides/<recurrenceId>[/<key>]` (its
    per-occurrence edit/delete flow) and `participants/<id>/<key>` (its
    RSVP flow); the exact same pattern as Email's `mailboxIds/<id>`/
    `keywords/<keyword>` patch keys from the Mail-side aerc review.
    A `null` value at either depth deletes that entry/sub-property.
    """
    result = {k: (dict(v) if isinstance(v, dict) else v) for k, v in existing.items()}
    key_prefix = prefix + "/"
    for key, value in patch.items():
        if not key.startswith(key_prefix):
            continue
        rest = key[len(key_prefix) :]
        if "/" in rest:
            entry_id_raw, sub_key_raw = rest.split("/", 1)
            entry_id = _unescape_json_pointer_segment(entry_id_raw)
            sub_key = _unescape_json_pointer_segment(sub_key_raw)
            if not isinstance(result.get(entry_id), dict):
                result[entry_id] = {}
            if value is None:
                result[entry_id].pop(sub_key, None)
            else:
                result[entry_id][sub_key] = value
        else:
            entry_id = _unescape_json_pointer_segment(rest)
            if value is None:
                result.pop(entry_id, None)
            else:
                result[entry_id] = value
    return result


def apply_jscalendar_patch(ical_text: str, patch: dict) -> str:
    """Update path: mutate the parsed `icalendar.Calendar` in place,
    touching only properties the patch actually changed - preserves
    everything else (VALARM, ATTENDEE, CATEGORIES, X- properties, ...)
    byte-for-byte-equivalent. See module docstring.

    `participants/<id>[/<key>]` and `recurrenceOverrides/<recurrenceId>
    [/<key>]` patch keys (see `_merge_patch_keys`) are resolved against
    the object's *current* state before falling through to the normal
    whole-map appliers (`_apply_participants`/`_apply_recurrence_overrides`),
    so both patch styles funnel through the same single code path.
    """
    cal = icalendar.Calendar.from_ical(ical_text)
    all_vevents = cal.walk("VEVENT")
    if not all_vevents:
        raise ValueError("no VEVENT found in calendar object")
    masters = [v for v in all_vevents if v.get("recurrence-id") is None]
    vevent = masters[0] if masters else all_vevents[0]
    other_vevents = [v for v in all_vevents if v is not vevent]

    if "participants" not in patch and any(k.startswith("participants/") for k in patch):
        existing_participants = _participants_from_vevent(vevent) or {}
        patch = {**patch, "participants": _merge_patch_keys(existing_participants, patch, "participants")}

    if "recurrenceOverrides" not in patch and any(k.startswith("recurrenceOverrides/") for k in patch):
        props = _extract_event_props(vevent)
        existing_overrides = (
            _recurrence_overrides_from_vevents(vevent, other_vevents, props, props["timeZone"]) or {}
        )
        patch = {
            **patch,
            "recurrenceOverrides": _merge_patch_keys(existing_overrides, patch, "recurrenceOverrides"),
        }

    _apply_jscalendar_props(vevent, patch)
    if "recurrenceOverrides" in patch:
        _apply_recurrence_overrides(cal, vevent, patch.get("recurrenceOverrides") or {})
    return cal.to_ical().decode("utf-8")
