"""EventSource push (RFC 8620 SS7.3): `GET /events`.

Real push, not just keepalive pings: `AccountWatcherRegistry` runs one
lightweight background poller per account that has at least one open
`/events` connection. Every `POLL_INTERVAL_SECONDS`, it compares the
account's mailbox cursor vector - the same SELECT-based logic
`Mailbox/get` uses (backends/imap: state.py's design) - against the
previous poll, and pushes a `StateChange` SSE event to every subscriber
when it changes.

This is polling, not raw IMAP IDLE, deliberately. IDLE's blocking
`idle_check()` call has to run via `asyncio.to_thread` (imapclient is
synchronous), and an `asyncio.to_thread` call can't be cancelled mid-wait
- the underlying thread keeps blocking until the timeout it was given
elapses, regardless of whether the coroutine awaiting it gets cancelled.
That makes prompt, reliable cleanup on SSE disconnect hard to get right.
A short poll interval gets equivalent user-visible latency (single-digit
seconds instead of "instant") with much simpler code that reuses the
same cursor-comparison logic already proven correct for Mailbox/get,
rather than a second, harder-to-test change-detection path. If sub-second
push latency is ever needed, revisit with real IDLE then.

Security note: unlike pool.py (which never retains a plaintext password
beyond the single connection-open call it's given), a watcher *does* hold
the account's password in memory for as long as it's polling - it has to
be able to reconnect on its own if its dedicated connection drops,
without a fresh HTTP request around to resupply a password. This is
bounded, not indefinite: a watcher and its held credentials are torn down
the moment the last subscriber (the last open /events connection for that
account) disconnects.
"""

from __future__ import annotations

import asyncio
import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from jmap_bridge.auth import Credentials, authenticate_request
from jmap_bridge.backends.imap.client import ImapConnection, ImapError
from jmap_bridge.backends.imap.modseq_state import encode_mail_state
from jmap_bridge.config import BridgeConfig
from jmap_bridge.errors import RequestError
from jmap_bridge.session import encode_account_id
from jmap_bridge.types.mailbox import _cursors_from_statuses, _list_selectable_mailboxes, _status_map

logger = logging.getLogger(__name__)

DEFAULT_PING_SECONDS = 30
MAX_PING_SECONDS = 3600
POLL_INTERVAL_SECONDS = 10


def parse_ping_seconds(raw: str | None) -> int:
    try:
        requested = int(raw or "0")
    except ValueError:
        requested = 0
    return max(0, min(requested, MAX_PING_SECONDS)) or DEFAULT_PING_SECONDS


class AccountWatcher:
    """Polls one account's mailbox cursors on a dedicated IMAP connection
    (outside the shared request pool - pool connections are meant to be
    short-lived and request-scoped, not held open indefinitely) and fans
    out a StateChange payload to every subscriber queue when they change.
    """

    def __init__(self, credentials: Credentials, poll_interval_seconds: float = POLL_INTERVAL_SECONDS):
        self._credentials = credentials
        self._poll_interval_seconds = poll_interval_seconds
        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._last_cursors: dict | None = None

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> bool:
        """Returns True once this watcher has no subscribers left, so the
        registry knows to drop it."""
        self._subscribers.discard(queue)
        if not self._subscribers and self._task is not None:
            self._task.cancel()
            self._task = None
        return not self._subscribers

    async def _run(self) -> None:
        conn: ImapConnection | None = None
        try:
            while True:
                await asyncio.sleep(self._poll_interval_seconds)
                if conn is None:
                    conn = await self._try_connect()
                    if conn is None:
                        continue
                try:
                    entries = await _list_selectable_mailboxes(conn)
                    cursors = _cursors_from_statuses(await _status_map(conn, entries))
                except ImapError as exc:
                    logger.warning("push watcher lost its IMAP connection, will reconnect: %s", exc)
                    await conn.logout()
                    conn = None
                    continue

                if self._last_cursors is not None and cursors != self._last_cursors:
                    self._notify(cursors)
                self._last_cursors = cursors
        except asyncio.CancelledError:
            pass
        finally:
            if conn is not None:
                await conn.logout()

    async def _try_connect(self) -> ImapConnection | None:
        imap_config = self._credentials.domain_config.imap
        try:
            return await ImapConnection.connect_and_login(
                imap_config.host,
                imap_config.port,
                imap_config.tls,
                self._credentials.email,
                self._credentials.password,
            )
        except ImapError as exc:
            logger.warning("push watcher could not connect, will retry: %s", exc)
            return None

    def _notify(self, cursors: dict) -> None:
        # RFC 8620 SS7.1: `changed`'s inner object is keyed by JMAP *data
        # type name* ("Email", "Mailbox", "Thread", ...), not a
        # capability URN - a client watching for "did Email change"
        # won't recognize an unfamiliar key. Our design has one combined
        # cursor for all three types, so the same state value is valid
        # for each of them; list all three so a client that only wires up
        # one type's handler (e.g. just "Mailbox", as happened here)
        # doesn't miss that its Email list is stale too.
        account_id = encode_account_id(self._credentials.email)
        state = encode_mail_state(cursors)
        payload = {
            "@type": "StateChange",
            "changed": {account_id: {"Mailbox": state, "Email": state, "Thread": state}},
        }
        for queue in list(self._subscribers):
            queue.put_nowait(payload)


class AccountWatcherRegistry:
    """One AccountWatcher per (domain, username) with at least one open
    /events connection; shared across requests like the connection pool.
    """

    def __init__(self) -> None:
        self._watchers: dict[tuple[str, str], AccountWatcher] = {}

    def subscribe(self, credentials: Credentials) -> tuple[AccountWatcher, asyncio.Queue]:
        key = (credentials.domain, credentials.email)
        watcher = self._watchers.get(key)
        if watcher is None:
            watcher = AccountWatcher(credentials)
            self._watchers[key] = watcher
        return watcher, watcher.subscribe()

    def unsubscribe(self, credentials: Credentials, watcher: AccountWatcher, queue: asyncio.Queue) -> None:
        key = (credentials.domain, credentials.email)
        if watcher.unsubscribe(queue) and self._watchers.get(key) is watcher:
            del self._watchers[key]


async def event_stream(
    queue: asyncio.Queue, *, ping_seconds: int, close_after_state: bool, max_events: int | None = None
):
    """The SSE body, consuming an already-subscribed queue. `max_events`
    bounds the loop for tests; production callers leave it None to run
    until the client disconnects (the generator is closed, raising
    GeneratorExit/CancelledError inside the `await`).
    """
    if close_after_state:
        # Nothing to report without polling first; honor the client's
        # request to close immediately rather than holding a connection
        # open for no reason.
        return
    sent = 0
    while max_events is None or sent < max_events:
        try:
            payload = await asyncio.wait_for(queue.get(), timeout=ping_seconds)
        except asyncio.TimeoutError:
            yield f"event: ping\ndata: {json.dumps({'interval': ping_seconds})}\n\n"
        else:
            yield f"event: state\ndata: {json.dumps(payload)}\n\n"
        sent += 1


async def handle_events(request: Request, config: BridgeConfig, watchers: AccountWatcherRegistry) -> Response:
    try:
        credentials = authenticate_request(request.headers.get("authorization"), config)
    except RequestError as exc:
        return JSONResponse(exc.to_problem(), status_code=exc.status)

    close_after_state = request.query_params.get("closeafter") == "state"
    ping_seconds = parse_ping_seconds(request.query_params.get("ping"))

    if close_after_state:
        return StreamingResponse(
            event_stream(asyncio.Queue(), ping_seconds=ping_seconds, close_after_state=True),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    watcher, queue = watchers.subscribe(credentials)

    async def stream_and_cleanup():
        try:
            async for chunk in event_stream(queue, ping_seconds=ping_seconds, close_after_state=False):
                yield chunk
        finally:
            watchers.unsubscribe(credentials, watcher, queue)

    return StreamingResponse(
        stream_and_cleanup(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
