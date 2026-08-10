"""EventSource push (RFC 8620 SS7.3): `GET /events`.

Honest scope: we have no live change-notification source yet (that would
need IMAP IDLE support per pooled connection, not built - see the plan's
Phase 1 deferred list). What this endpoint *does* do is establish a real,
spec-compliant `text/event-stream` connection and keep it alive with
`ping` events at the client's requested interval, so clients that treat a
failed/missing push connection as fatal (observed: one client aborted its
whole session on a 404 here) can proceed. It does not push `StateChange`
events when mail actually changes - clients relying on this for real-time
updates will need to keep polling `Email/changes`/`Mailbox/changes` in the
meantime; a real implementation needs IMAP IDLE wired through the
connection pool, which is future work.
"""

from __future__ import annotations

import asyncio
import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from jmap_bridge.auth import authenticate_request
from jmap_bridge.config import BridgeConfig
from jmap_bridge.errors import RequestError

DEFAULT_PING_SECONDS = 30
MAX_PING_SECONDS = 3600


def parse_ping_seconds(raw: str | None) -> int:
    try:
        requested = int(raw or "0")
    except ValueError:
        requested = 0
    return max(0, min(requested, MAX_PING_SECONDS)) or DEFAULT_PING_SECONDS


async def event_stream(*, ping_seconds: int, close_after_state: bool, max_events: int | None = None):
    """The SSE body. `max_events` bounds the loop for tests; production
    callers (handle_events) leave it None to run until the client
    disconnects (CancelledError).
    """
    if close_after_state:
        # Nothing meaningful to report without a real change source; honor
        # the client's request to close immediately rather than holding a
        # connection open for no reason.
        return
    sent = 0
    try:
        while max_events is None or sent < max_events:
            await asyncio.sleep(ping_seconds)
            yield f"event: ping\ndata: {json.dumps({'interval': ping_seconds})}\n\n"
            sent += 1
    except asyncio.CancelledError:
        return


async def handle_events(request: Request, config: BridgeConfig) -> Response:
    try:
        authenticate_request(request.headers.get("authorization"), config)
    except RequestError as exc:
        return JSONResponse(exc.to_problem(), status_code=exc.status)

    close_after_state = request.query_params.get("closeafter") == "state"
    ping_seconds = parse_ping_seconds(request.query_params.get("ping"))

    return StreamingResponse(
        event_stream(ping_seconds=ping_seconds, close_after_state=close_after_state),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
