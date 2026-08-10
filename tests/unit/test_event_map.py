import pytest

from jmap_bridge.backends.caldav.event_map import (
    apply_jscalendar_patch,
    build_vevent_ical,
    decode_event_id,
    encode_event_id,
    ical_to_jscalendar_event,
)

FULL_PROPS = {
    "title": "Team sync",
    "description": "Weekly sync",
    "start": "2026-01-15T09:00:00",
    "duration": "PT1H",
    "timeZone": "America/New_York",
    "showWithoutTime": False,
    "status": "confirmed",
    "freeBusyStatus": "busy",
    "priority": 5,
    "privacy": "private",
    "locations": {"loc1": {"name": "Room 42"}},
    "participants": {
        "organizer": {
            "name": "Alice",
            "email": "alice@example.com",
            "roles": {"owner": True},
            "participationStatus": "accepted",
            "expectReply": False,
        },
        "bob@example.com": {
            "name": "Bob",
            "email": "bob@example.com",
            "roles": {"attendee": True},
            "participationStatus": "needs-action",
            "expectReply": True,
        },
    },
    "alerts": {
        "alert1": {
            "trigger": {"@type": "OffsetTrigger", "offset": "-PT15M", "relativeTo": "start"},
            "action": "display",
        },
    },
}


def test_event_id_round_trip_and_normalizes_percent_encoding():
    """Regression test: event hrefs share the same percent-encoding
    inconsistency risk as calendar hrefs (see calendar_map.py) - a
    calendar_href/event_href pair that differ only in `%40` vs `@` must
    still produce the same id.
    """
    eid = encode_event_id(
        "http://dav.example.com/alice%40example.com/work/",
        "http://dav.example.com/alice@example.com/work/abc.ics",
    )
    calendar_href, event_href = decode_event_id(eid)
    assert calendar_href == "/alice@example.com/work/"
    assert event_href == "/alice@example.com/work/abc.ics"


def test_decode_event_id_rejects_garbage():
    with pytest.raises(ValueError):
        decode_event_id("not-an-event-id")


def test_full_round_trip():
    ical_text = build_vevent_ical(FULL_PROPS, uid="test-uid-1")
    parsed = ical_to_jscalendar_event(ical_text, "Vsomeid", "Ccal1")

    assert parsed["uid"] == "test-uid-1"
    assert parsed["title"] == "Team sync"
    assert parsed["description"] == "Weekly sync"
    assert parsed["start"] == "2026-01-15T09:00:00"
    assert parsed["duration"] == "PT1H"
    assert parsed["timeZone"] == "America/New_York"
    assert parsed["showWithoutTime"] is False
    assert parsed["status"] == "confirmed"
    assert parsed["freeBusyStatus"] == "busy"
    assert parsed["priority"] == 5
    assert parsed["privacy"] == "private"
    assert parsed["locations"] == {"loc1": {"name": "Room 42"}}
    assert parsed["calendarIds"] == {"Ccal1": True}
    assert "organizer" in parsed["participants"]
    assert parsed["participants"]["organizer"]["roles"] == {"owner": True}
    assert "bob@example.com" in parsed["participants"]
    assert parsed["participants"]["bob@example.com"]["participationStatus"] == "needs-action"
    assert parsed["alerts"]["alert1"]["trigger"]["offset"] == "-PT15M"


def test_partial_patch_preserves_untouched_properties():
    """Round-trip fidelity: patching just `title` must not disturb
    description, location, participants, or alerts - the in-place
    mutation contract event_map.py's module docstring commits to.
    """
    ical_text = build_vevent_ical(FULL_PROPS, uid="test-uid-1")
    patched = apply_jscalendar_patch(ical_text, {"title": "Team sync (renamed)"})
    reparsed = ical_to_jscalendar_event(patched, "Vsomeid", "Ccal1")

    assert reparsed["title"] == "Team sync (renamed)"
    assert reparsed["description"] == "Weekly sync"
    assert reparsed["start"] == "2026-01-15T09:00:00"
    assert reparsed["locations"] == {"loc1": {"name": "Room 42"}}
    assert reparsed["alerts"]["alert1"]["trigger"]["offset"] == "-PT15M"
    assert "bob@example.com" in reparsed["participants"]


def test_patch_changing_only_start_preserves_duration_and_timezone():
    ical_text = build_vevent_ical(FULL_PROPS, uid="test-uid-1")
    patched = apply_jscalendar_patch(ical_text, {"start": "2026-01-16T09:00:00"})
    reparsed = ical_to_jscalendar_event(patched, "Vsomeid", "Ccal1")

    assert reparsed["start"] == "2026-01-16T09:00:00"
    assert reparsed["duration"] == "PT1H"
    assert reparsed["timeZone"] == "America/New_York"


def test_all_day_event():
    props = {"title": "Holiday", "start": "2026-03-01T00:00:00", "duration": "P1D", "showWithoutTime": True}
    ical_text = build_vevent_ical(props, uid="test-uid-2")
    parsed = ical_to_jscalendar_event(ical_text, "Vid2", "Ccal1")

    assert parsed["showWithoutTime"] is True
    assert parsed["timeZone"] is None
    assert parsed["start"] == "2026-03-01T00:00:00"
    assert parsed["duration"] == "P1D"


def test_utc_event():
    props = {"title": "UTC event", "start": "2026-04-01T12:00:00", "duration": "PT30M", "timeZone": "Etc/UTC"}
    ical_text = build_vevent_ical(props, uid="test-uid-3")
    parsed = ical_to_jscalendar_event(ical_text, "Vid3", "Ccal1")

    assert parsed["timeZone"] == "Etc/UTC"
    assert parsed["start"] == "2026-04-01T12:00:00"


def test_floating_time_event():
    props = {"title": "Floating", "start": "2026-05-01T08:00:00", "duration": "PT1H"}
    ical_text = build_vevent_ical(props, uid="test-uid-4")
    parsed = ical_to_jscalendar_event(ical_text, "Vid4", "Ccal1")

    assert parsed["timeZone"] is None
    assert parsed["showWithoutTime"] is False
    assert parsed["start"] == "2026-05-01T08:00:00"


def test_build_vevent_requires_start():
    with pytest.raises(ValueError):
        build_vevent_ical({"title": "No start"})


RECURRING_PROPS = {
    "title": "Standup",
    "start": "2026-01-05T09:00:00",
    "duration": "PT15M",
    "timeZone": "America/New_York",
    "recurrenceRule": {
        "@type": "RecurrenceRule",
        "frequency": "weekly",
        "interval": 1,
        "byDay": [{"day": "mo"}, {"day": "we"}, {"day": "fr"}],
        "count": 10,
    },
}


def test_recurrence_rule_round_trip():
    ical_text = build_vevent_ical(RECURRING_PROPS, uid="rec-uid-1")
    parsed = ical_to_jscalendar_event(ical_text, "Vid1", "Ccal1")
    rule = parsed["recurrenceRule"]
    assert rule["frequency"] == "weekly"
    assert rule["interval"] == 1
    assert rule["count"] == 10
    assert {"day": "mo"} in rule["byDay"]
    assert {"day": "we"} in rule["byDay"]
    assert {"day": "fr"} in rule["byDay"]


def test_recurrence_rule_until():
    props = {
        **RECURRING_PROPS,
        "recurrenceRule": {"@type": "RecurrenceRule", "frequency": "daily", "until": "2026-03-01T09:00:00"},
    }
    ical_text = build_vevent_ical(props, uid="rec-uid-until")
    parsed = ical_to_jscalendar_event(ical_text, "Vid1", "Ccal1")
    assert parsed["recurrenceRule"]["until"] == "2026-03-01T09:00:00"


def test_recurrence_override_excluded():
    ical_text = build_vevent_ical(RECURRING_PROPS, uid="rec-uid-1")
    patched = apply_jscalendar_patch(
        ical_text, {"recurrenceOverrides": {"2026-01-07T09:00:00": {"excluded": True}}}
    )
    parsed = ical_to_jscalendar_event(patched, "Vid1", "Ccal1")
    assert parsed["recurrenceOverrides"] == {"2026-01-07T09:00:00": {"excluded": True}}
    # base event must be unaffected
    assert parsed["title"] == "Standup"
    assert parsed["recurrenceRule"]["frequency"] == "weekly"


def test_recurrence_override_modified_instance_is_minimal_patch():
    """The override patch must contain only what actually changed for
    that occurrence - not every property, and not a spurious `start`
    just because this occurrence's date differs from the master's first
    occurrence (see event_map.py's _override_patch_from_vevent).
    """
    ical_text = build_vevent_ical(RECURRING_PROPS, uid="rec-uid-1")
    patched = apply_jscalendar_patch(
        ical_text,
        {
            "recurrenceOverrides": {
                "2026-01-09T09:00:00": {
                    "title": "Standup (moved room)",
                    "locations": {"loc1": {"name": "Room B"}},
                }
            }
        },
    )
    parsed = ical_to_jscalendar_event(patched, "Vid1", "Ccal1")
    override = parsed["recurrenceOverrides"]["2026-01-09T09:00:00"]
    assert override == {"title": "Standup (moved room)", "locations": {"loc1": {"name": "Room B"}}}


def test_recurrence_override_moved_time_includes_start():
    ical_text = build_vevent_ical(RECURRING_PROPS, uid="rec-uid-1")
    patched = apply_jscalendar_patch(
        ical_text,
        {"recurrenceOverrides": {"2026-01-12T09:00:00": {"start": "2026-01-12T14:00:00"}}},
    )
    parsed = ical_to_jscalendar_event(patched, "Vid1", "Ccal1")
    assert parsed["recurrenceOverrides"]["2026-01-12T09:00:00"]["start"] == "2026-01-12T14:00:00"


def test_recurrence_overrides_replaced_wholesale_by_patch():
    ical_text = build_vevent_ical(RECURRING_PROPS, uid="rec-uid-1")
    with_override = apply_jscalendar_patch(
        ical_text, {"recurrenceOverrides": {"2026-01-07T09:00:00": {"excluded": True}}}
    )
    cleared = apply_jscalendar_patch(with_override, {"recurrenceOverrides": {}})
    parsed = ical_to_jscalendar_event(cleared, "Vid1", "Ccal1")
    assert not parsed.get("recurrenceOverrides")


def test_patch_not_touching_recurrence_overrides_preserves_them():
    ical_text = build_vevent_ical(RECURRING_PROPS, uid="rec-uid-1")
    with_override = apply_jscalendar_patch(
        ical_text, {"recurrenceOverrides": {"2026-01-07T09:00:00": {"excluded": True}}}
    )
    patched = apply_jscalendar_patch(with_override, {"description": "Daily standup"})
    parsed = ical_to_jscalendar_event(patched, "Vid1", "Ccal1")
    assert parsed["description"] == "Daily standup"
    assert parsed["recurrenceOverrides"]["2026-01-07T09:00:00"] == {"excluded": True}


def test_recurrence_override_patch_key_sets_single_field():
    """Regression test: confirmed live in Bulwark webmail's source that
    real clients patch a single occurrence via
    `recurrenceOverrides/<recurrenceId>/<key>`, not whole-map
    replacement - the same pattern as Email's mailboxIds/<id>.
    """
    ical_text = build_vevent_ical(RECURRING_PROPS, uid="rec-uid-1")
    patched = apply_jscalendar_patch(ical_text, {"recurrenceOverrides/2026-01-09T09:00:00/title": "Moved"})
    parsed = ical_to_jscalendar_event(patched, "Vid1", "Ccal1")
    assert parsed["recurrenceOverrides"]["2026-01-09T09:00:00"] == {"title": "Moved"}


def test_recurrence_override_patch_key_delete_whole_entry():
    ical_text = build_vevent_ical(RECURRING_PROPS, uid="rec-uid-1")
    with_override = apply_jscalendar_patch(
        ical_text, {"recurrenceOverrides": {"2026-01-07T09:00:00": {"excluded": True}}}
    )
    cleared = apply_jscalendar_patch(with_override, {"recurrenceOverrides/2026-01-07T09:00:00": None})
    parsed = ical_to_jscalendar_event(cleared, "Vid1", "Ccal1")
    assert not parsed.get("recurrenceOverrides")


def test_recurrence_override_patch_key_preserves_other_overrides():
    ical_text = build_vevent_ical(RECURRING_PROPS, uid="rec-uid-1")
    with_overrides = apply_jscalendar_patch(
        ical_text,
        {
            "recurrenceOverrides": {
                "2026-01-07T09:00:00": {"excluded": True},
                "2026-01-09T09:00:00": {"title": "First title"},
            }
        },
    )
    patched = apply_jscalendar_patch(
        with_overrides, {"recurrenceOverrides/2026-01-09T09:00:00/title": "Second title"}
    )
    parsed = ical_to_jscalendar_event(patched, "Vid1", "Ccal1")
    overrides = parsed["recurrenceOverrides"]
    assert overrides["2026-01-07T09:00:00"] == {"excluded": True}
    assert overrides["2026-01-09T09:00:00"] == {"title": "Second title"}


def test_participant_patch_key_updates_single_field():
    ical_text = build_vevent_ical(FULL_PROPS, uid="test-uid-1")
    patched = apply_jscalendar_patch(
        ical_text, {"participants/bob@example.com/participationStatus": "accepted"}
    )
    parsed = ical_to_jscalendar_event(patched, "Vid1", "Ccal1")
    assert parsed["participants"]["bob@example.com"]["participationStatus"] == "accepted"
    # organizer must be untouched
    assert parsed["participants"]["organizer"]["email"] == "alice@example.com"


def test_status_and_freebusy_defaults_when_absent():
    props = {"title": "Minimal", "start": "2026-06-01T10:00:00", "duration": "PT1H"}
    ical_text = build_vevent_ical(props, uid="test-uid-5")
    parsed = ical_to_jscalendar_event(ical_text, "Vid5", "Ccal1")
    assert parsed["status"] == "confirmed"
    assert parsed["freeBusyStatus"] == "busy"
