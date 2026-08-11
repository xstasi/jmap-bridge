# Known gaps and deferred features

This bridge implements all three planned phases — Mail↔IMAP, Calendars↔CalDAV,
Contacts↔CardDAV — and each has been live-tested against a real backend and at
least one real JMAP client (aerc and/or Bulwark webmail). This document lists
what's *not* covered: deliberate scoping decisions, architectural tradeoffs
inherent to the zero-local-storage design, and operational gaps. Nothing here
is a silent omission — each item is also noted in the relevant module's
docstring at the point it would otherwise be implemented.

## Deferred methods and properties, by phase

### Mail (Phase 1)
- `Email/queryChanges` — not implemented. `Email/query` always reports
  `canCalculateChanges: false`, so a spec-compliant client never depends on
  it (confirmed against aerc's source).
- `inMailboxOtherThan` filter — would need aggregating a search across
  multiple mailboxes; raises `unsupportedFilter` rather than being silently
  ignored.
- `Email/copy` — not implemented. Low priority given credential-passthrough
  auth means a session only ever has one account, so cross-account copy
  isn't reachable in this bridge's model anyway.
- `Identity/set` — identities are derived read-only from domain config; no
  persistence layer for user-managed identities.
- `VacationResponse/get`/`/set` — would need a ManageSieve (RFC 5804)
  backend client, a protocol this bridge doesn't speak at all.
- `PushSubscription/get`/`/set` — browser Web Push registration; unrelated
  to and separate from the SSE `/events` push this bridge does implement.
- `Email/get`'s lightweight path (used whenever a caller's `properties`
  don't need real body content — confirmed this covers aerc's entire
  Email/get usage) never downloads the message, deriving `bodyStructure`
  from IMAP's own `BODYSTRUCTURE` fetch item instead. Two narrow,
  confirmed-live consequences: each `EmailBodyPart`'s `headers` field is
  always `[]` in this path (neither aerc's `bodyProperties` nor RFC 8621's
  own default includes it — this bridge doesn't support the
  `bodyProperties` argument's per-part filtering at all yet, a pre-existing
  gap this didn't introduce); and `size` for a base64-encoded part is an
  approximation (exact 4:3 ratio, doesn't know the on-wire line-wrap CRLF
  overhead BODYSTRUCTURE doesn't report) rather than the exact decoded
  byte count the full-fetch path computes.

### Calendars (Phase 2)
- `CalendarEvent/changes` — stubbed to always raise `cannotCalculateChanges`.
  RFC 6578 sync-collection can't distinguish a created href from an updated
  one without locally-stored state this bridge deliberately doesn't keep;
  confirmed safe since Bulwark webmail never calls it. (`Calendar/changes`,
  the container-level type, *is* implemented for real.)
- `CalendarEvent/copy`, `CalendarEvent/parse`, `expandRecurrences`,
  `ParticipantIdentity/*`, `CalendarEventNotification/*` (scheduling/iTIP —
  no meeting-invite email flow), `Principal/getAvailability` — not
  implemented.
- Calendar sharing (`shareWith`) — not implemented, consistent with the
  whole bridge's no-delegated-accounts model.
- `CalendarEvent/query` filters limited to `inCalendar` (required) and
  `before`/`after` (native CalDAV time-range REPORT) — `text`/`title`/etc.
  would need fetch-then-locally-filter and aren't built yet.
- Recurrence overrides: the RDATE-equivalent case (an added one-off
  occurrence not matching the recurrence rule) isn't supported, only
  `excluded` and modified-instance overrides.
- RFC 9554's extended postal-address component set (room/floor/building/
  block/subdistrict/district/direction/landmark) isn't supported.
- No VTIMEZONE block is embedded for non-UTC events — relies on the
  recipient having the IANA tzdata for a bare `TZID` reference (confirmed
  this works against Radicale in practice, but isn't universally guaranteed).
- **`maxCalendarsPerEvent` is capped at 1** — CalDAV has no native
  multi-collection-membership primitive. Confirmed this is a real mismatch,
  not just a theoretical one: Bulwark webmail's calendar code genuinely
  treats `calendarIds` as multi-valued (adding an event to a second
  calendar without removing it from the first). A client doing that against
  this bridge gets a hard `invalidProperties` error.

### Contacts (Phase 3)
- `ContactCard/changes` — stubbed, identical reasoning to
  `CalendarEvent/changes`. (`AddressBook/changes` *is* implemented for real.)
- `ContactCard/copy`, `ContactCard/parse` — not implemented; Bulwark does
  vCard import/export entirely client-side and never calls either.
- `media` (photos) — not implemented. Confirmed real-world relevance:
  Bulwark sends photos as inline base64 `data:` URIs; a client trying to
  set one against this bridge finds it silently doesn't save (an unmodeled
  property is inert, not an error).
- `cryptoKeys`, `directories`, `links`, `localizations`, `personalInfo`,
  `speakToAs`, `calendars`/`schedulingAddresses`, `relatedTo` — not
  modeled.
- AddressBook sharing (`shareWith`) — not implemented.
- `ContactCard/query` filters limited to `inAddressBook` (required) and
  `text` (fetch-then-locally-filter).
- RFC 9554's extended postal-address component set — not supported (same
  as Calendar).
- `maxAddressBooksPerCard` is capped at 1, same reasoning as Calendar's cap
  — but lower-risk here, since Bulwark's *own* contact code also treats
  `addressBookIds` as effectively single-valued in practice.
- Structured name mapping is lossy for `surname2`/`generation` components
  (Spanish-style double surnames, generational suffixes): vCard's `N`
  property has no dedicated slot for either, so they're appended onto
  Family/Honorific-suffix on write and can't be split apart again on read.

## Architectural tradeoffs (by design, not bugs)

- **Zero local storage** is the core design goal, but it has a real
  consequence: a few small in-memory caches exist as narrow, documented
  exceptions (`id_redirect.py` for stale ids after a move/rename,
  `blob_cache.py` for staged uploads between `/upload` and a following
  `Email/set`, `push.py`'s per-account IMAP password held only while an
  `/events` subscriber is connected) — none of them survive a process
  restart. A moved/renamed object's old id stops resolving the moment the
  bridge restarts, even though it worked a moment before.
- **Every mail-state computation (`Email/get`'s `state`, `Email/query`'s
  `queryState`, `Mailbox/changes`, `Mailbox/query`) still costs one IMAP
  round trip per mailbox in the account** (a full `SELECT` sweep - see
  `backends/imap/` design notes on why `SELECT`, not `STATUS`, is used).
  Memoized per HTTP request now (`context.py`'s `_RequestCache`, added
  after finding an account with 16 mailboxes triggering ~85 redundant
  SELECTs - 5 full sweeps - for a single batched request), so a batch of
  several methods only pays this cost once - but a single request still
  scales with mailbox count, and an account with hundreds of folders will
  still feel this on every request. `Mailbox/get` specifically still
  isn't covered by the cache (it needs live unseen counts via `SEARCH
  UNSEEN`, a different and more expensive per-mailbox sweep with no
  cross-call redundancy to fix, since only one call site uses it).
- **Push (`/events`) only covers Mail.** Calendar and Contacts have no
  real-time push at all — a client has to poll `Calendar/get`/
  `AddressBook/get` for a changed `state` string, same as Bulwark's own
  polling pattern (confirmed: it never relies on push for either type).
- **No IMAP IDLE** — Mail push is 10-second polling, not a real IDLE
  connection, to avoid the complexity of cleanly cancelling a blocking IDLE
  call mid-wait.
- **No connection pooling for CalDAV/CardDAV** — deliberate: both are
  stateless HTTP+Basic-auth with no expensive handshake like IMAP LOGIN to
  amortize, unlike the IMAP connection pool.
- **Single-tenant credential passthrough only** — no delegated/shared
  accounts anywhere in the bridge; every JMAP session maps to exactly one
  backend account. Gmail/Microsoft 365-class OAuth-only providers are out
  of scope entirely (Basic/plain-auth backends only).

## Operational gaps

- No CI pipeline.
- No production deployment guide (the included `Dockerfile` targets local/
  test use; `docker-compose.test.yml` is a test fixture, not a deployment
  config).
- TLS is a self-signed dev certificate (`certs/dev-*.pem`) — fine for local
  use or behind a reverse proxy doing real ACME/Let's Encrypt certs, not
  for direct public exposure.
- No structured logging/observability beyond default `uvicorn` request logs.
- No end-user setup README yet — this file and the module docstrings are
  currently the only documentation.
