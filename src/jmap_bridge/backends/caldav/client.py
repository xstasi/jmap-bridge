"""Thin async wrapper around `caldav.aio.AsyncDAVClient` (see the Phase 2
plan's spike: confirmed live against a real server that this client's
discovery, sync-collection REPORT, time-range REPORT, and ETag-conflict
behavior all work as needed). Unlike `imapclient` (Phase 1's IMAP
library, which has no async option and is wrapped per-call in
`asyncio.to_thread` - see backends/imap/client.py), this library has
genuine async support, so no thread-wrapping is needed here.

A single `CaldavConnection` wraps one authenticated `AsyncDAVClient`,
valid for the lifetime of one JMAP HTTP request/batch (see context.py's
`ctx.caldav()`) - principal discovery happens once per request, not once
per method call within a batch.

An event/calendar href is independently addressable once known (no
per-request re-listing needed to act on one), which is what lets
`backends/caldav/event_map.py`'s id encoding skip a lookup table the same
way `decode_email_id`/`decode_mailbox_id` do for Mail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import caldav
from caldav.aio import AsyncDAVClient
from caldav.elements import dav
from caldav.lib.error import DAVError, NotFoundError


class CaldavError(Exception):
    """Wraps any caldav/WebDAV protocol failure."""


@dataclass(frozen=True, slots=True)
class CalendarEntry:
    href: str
    display_name: str | None
    sync_token: str | None


@dataclass(frozen=True, slots=True)
class EventEntry:
    href: str
    ical_text: str
    etag: str | None


class CaldavConnection:
    def __init__(self, client: AsyncDAVClient, principal_href: str):
        self._client = client
        self.principal_href = principal_href

    @classmethod
    async def connect(cls, url: str, username: str, password: str) -> "CaldavConnection":
        client = AsyncDAVClient(
            url=url,
            username=username,
            password=password,
            auth_type="basic",
            # Mirrors IMAP's explicit tls modes (config.py's ImapConfig):
            # honor whatever scheme the configured url actually uses
            # rather than forcing https, so a self-hosted plain-http
            # server (or the test fixture) isn't rejected outright.
            require_tls=url.startswith("https://"),
        )
        try:
            principal = await client.principal()
        except DAVError as exc:
            await client.close()
            raise CaldavError(f"CalDAV discovery at {url!r} failed: {exc}") from exc
        return cls(client, str(principal.url))

    async def close(self) -> None:
        await self._client.close()

    def _principal(self) -> caldav.Principal:
        return caldav.Principal(client=self._client, url=self.principal_href)

    def calendar(self, href: str) -> caldav.Calendar:
        return caldav.Calendar(client=self._client, url=href)

    async def list_calendars(self) -> list[CalendarEntry]:
        try:
            calendars = await self._principal().calendars()
        except DAVError as exc:
            raise CaldavError(f"listing calendars failed: {exc}") from exc
        entries = []
        for cal in calendars:
            try:
                name = await cal.get_display_name()
            except DAVError:
                name = None
            try:
                props = await cal.get_properties([dav.SyncToken()])
                sync_token = props.get(dav.SyncToken().tag)
            except DAVError:
                sync_token = None
            entries.append(CalendarEntry(href=str(cal.url), display_name=name, sync_token=sync_token))
        return entries

    async def create_calendar(self, name: str) -> str:
        try:
            cal = await self._principal().make_calendar(name=name)
        except DAVError as exc:
            raise CaldavError(f"creating calendar {name!r} failed: {exc}") from exc
        return str(cal.url)

    async def rename_calendar(self, href: str, new_name: str) -> None:
        try:
            await self.calendar(href).set_properties([dav.DisplayName(new_name)])
        except DAVError as exc:
            raise CaldavError(f"renaming calendar {href!r} failed: {exc}") from exc

    async def delete_calendar(self, href: str) -> None:
        try:
            await self.calendar(href).delete()
        except DAVError as exc:
            raise CaldavError(f"deleting calendar {href!r} failed: {exc}") from exc

    async def list_events(self, calendar_href: str) -> list[EventEntry]:
        """Every event in a calendar - no native CalDAV "list everything"
        primitive beyond a REPORT with an always-true filter, which
        `calendar.events()` does. Used for CalendarEvent/query's fallback
        (no before/after bounds given).
        """
        try:
            events = await self.calendar(calendar_href).events()
        except DAVError as exc:
            raise CaldavError(f"listing events in {calendar_href!r} failed: {exc}") from exc
        return [EventEntry(href=str(e.url), ical_text=e.data, etag=e.etag) for e in events]

    async def search_events_in_range(
        self, calendar_href: str, start: datetime, end: datetime
    ) -> list[EventEntry]:
        """RFC 4791 SS7.8 time-range REPORT - server-side filtering, so
        CalendarEvent/query's before/after doesn't need every event
        fetched into the bridge first. `expand=False`: recurrence
        expansion is deliberately not used (see the plan's Deferred
        list) - a matching recurring event is returned once, not once
        per matching occurrence.
        """
        try:
            results = await self.calendar(calendar_href).search(
                start=start, end=end, event=True, expand=False
            )
        except DAVError as exc:
            raise CaldavError(f"time-range search in {calendar_href!r} failed: {exc}") from exc
        return [EventEntry(href=str(e.url), ical_text=e.data, etag=e.etag) for e in results]

    async def get_event(self, calendar_href: str, event_href: str) -> EventEntry | None:
        try:
            event = await self.calendar(calendar_href).event_by_url(event_href)
        except NotFoundError:
            return None
        except DAVError as exc:
            raise CaldavError(f"fetching event {event_href!r} failed: {exc}") from exc
        return EventEntry(href=str(event.url), ical_text=event.data, etag=event.etag)

    async def create_event(self, calendar_href: str, ical_text: str) -> EventEntry:
        """The destination href is derived by the server/library from the
        iCalendar UID (confirmed live: an event created with UID `X` in
        the VEVENT ends up at `.../X.ics`) - so the caller controls the
        href indirectly by choosing the UID, not by passing one explicitly.
        """
        try:
            event = await self.calendar(calendar_href).add_event(ical_text)
        except DAVError as exc:
            raise CaldavError(f"creating event in {calendar_href!r} failed: {exc}") from exc
        return EventEntry(href=str(event.url), ical_text=event.data, etag=event.etag)

    async def update_event(self, calendar_href: str, event_href: str, ical_text: str) -> EventEntry:
        """Fetch-mutate-save: `event_by_url` does a real GET, so the
        `Event` object's etag reflects the resource's current state at
        that moment; `.save()` then PUTs with an If-Match built from that
        etag (`Event.etag` has no public setter - confirmed live, it can
        only come from a real load - so there's no way to inject a
        caller-supplied etag here even if we wanted to). This gives real
        optimistic-concurrency conflict detection "for free": a genuinely
        concurrent write landing between our GET and PUT raises
        `ETagMismatchError` (a `DAVError` subclass, left unwrapped here so
        callers can catch it specifically), confirmed live in the spike.
        """
        try:
            event = await self.calendar(calendar_href).event_by_url(event_href)
        except NotFoundError:
            raise CaldavError(f"event {event_href!r} not found") from None
        except DAVError as exc:
            raise CaldavError(f"fetching event {event_href!r} for update failed: {exc}") from exc
        event.data = ical_text
        await event.save()
        return EventEntry(href=str(event.url), ical_text=event.data, etag=event.etag)

    async def delete_event(self, calendar_href: str, event_href: str) -> None:
        try:
            event = await self.calendar(calendar_href).event_by_url(event_href)
            await event.delete()
        except NotFoundError:
            pass  # already gone - destroy is idempotent
        except DAVError as exc:
            raise CaldavError(f"deleting event {event_href!r} failed: {exc}") from exc
