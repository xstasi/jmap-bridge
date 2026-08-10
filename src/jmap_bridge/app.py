"""ASGI app assembly: routes `/.well-known/jmap`, `/session`, `/api`,
`/upload/{accountId}`, `/download/{accountId}/{blobId}/{name}`, `/events`
onto the pieces built elsewhere (auth, session, dispatch, upload, push).
Importing `jmap_bridge.types` registers every JMAP method handler as a
side effect (each `types/*.py` module calls `@method(...)` at import
time).
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

import jmap_bridge.types  # noqa: F401 - import side effect: registers all method handlers
from jmap_bridge.auth import authenticate_request
from jmap_bridge.blob_cache import BlobCache
from jmap_bridge.config import BridgeConfig, load_config
from jmap_bridge.context import RequestContext
from jmap_bridge.dispatch import dispatch_request
from jmap_bridge.errors import NotJSON, NotRequest, RequestError
from jmap_bridge.id_redirect import IdRedirectCache
from jmap_bridge.pool import ImapConnectionPool
from jmap_bridge.push import AccountWatcherRegistry, handle_events
from jmap_bridge.session import build_session
from jmap_bridge.upload import handle_download, handle_upload

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware:
    """Logs each request the moment it arrives, not just when it finishes.

    uvicorn's own access log only writes a line once a response completes -
    a request that hangs indefinitely inside a handler (e.g. stuck waiting
    on a backend connection) produces zero log output, which looks
    identical to "never reached the server at all". This middleware logs
    at DEBUG on arrival and on completion/failure, so the two cases are
    distinguishable: enable with JMAP_BRIDGE_LOG_LEVEL=DEBUG.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method, path = scope["method"], scope["path"]
        start = time.monotonic()
        logger.debug("request start: %s %s", method, path)
        status: dict[str, int] = {}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception(
                "request crashed: %s %s (%.2fs)", method, path, time.monotonic() - start
            )
            raise
        else:
            logger.debug(
                "request done: %s %s -> %s (%.2fs)",
                method, path, status.get("code"), time.monotonic() - start,
            )


def create_app(config: BridgeConfig, base_url: str) -> Starlette:
    pool = ImapConnectionPool()
    blob_cache = BlobCache()
    id_redirects = IdRedirectCache()
    watchers = AccountWatcherRegistry()

    async def well_known_jmap(request: Request) -> Response:
        return RedirectResponse(url="/session", status_code=301)

    async def session_endpoint(request: Request) -> Response:
        try:
            credentials = authenticate_request(request.headers.get("authorization"), config)
        except RequestError as exc:
            return JSONResponse(exc.to_problem(), status_code=exc.status)
        return JSONResponse(build_session(base_url, credentials))

    async def api_endpoint(request: Request) -> Response:
        try:
            credentials = authenticate_request(request.headers.get("authorization"), config)
        except RequestError as exc:
            return JSONResponse(exc.to_problem(), status_code=exc.status)

        try:
            body = await request.json()
        except ValueError:
            exc = NotJSON()
            return JSONResponse(exc.to_problem(), status_code=exc.status)
        if not isinstance(body, dict) or not isinstance(body.get("methodCalls"), list):
            exc = NotRequest("request must have a 'methodCalls' array")
            return JSONResponse(exc.to_problem(), status_code=exc.status)

        ctx = RequestContext(
            credentials=credentials, config=config, pool=pool, blob_cache=blob_cache,
            id_redirects=id_redirects,
        )
        method_responses, created_ids = await dispatch_request(
            ctx, body["methodCalls"], body.get("createdIds")
        )
        return JSONResponse(
            {
                "methodResponses": method_responses,
                "createdIds": created_ids,
                # Must match /session's own `state` (RFC 8620 SS3.4) - a
                # client is expected to compare the two and refetch
                # /session when they differ. A mismatched placeholder here
                # (previously a hardcoded "static") makes every single
                # /api call look like the session changed, triggering a
                # redundant /session refetch each time - confirmed live
                # in aerc's request log.
                "sessionState": build_session(base_url, credentials)["state"],
            }
        )

    async def upload_endpoint(request: Request) -> Response:
        return await handle_upload(request, config, pool, blob_cache)

    async def download_endpoint(request: Request) -> Response:
        return await handle_download(request, config, pool, blob_cache)

    async def events_endpoint(request: Request) -> Response:
        return await handle_events(request, config, watchers)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        pool.start()
        try:
            yield
        finally:
            await pool.stop()

    return Starlette(
        routes=[
            Route("/.well-known/jmap", well_known_jmap, methods=["GET"]),
            Route("/session", session_endpoint, methods=["GET"]),
            Route("/api", api_endpoint, methods=["POST"]),
            Route("/upload/{account_id}", upload_endpoint, methods=["POST"]),
            Route("/download/{account_id}/{blob_id}/{name}", download_endpoint, methods=["GET"]),
            Route("/events", events_endpoint, methods=["GET"]),
        ],
        middleware=[Middleware(RequestLoggingMiddleware)],
        lifespan=lifespan,
    )


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("JMAP_BRIDGE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config_path = os.environ.get("JMAP_BRIDGE_CONFIG", "config/domains.yaml")
    base_url = os.environ.get("JMAP_BRIDGE_BASE_URL", "http://localhost:8080")
    config = load_config(config_path)
    app = create_app(config, base_url)

    # TLS is optional here and meant for local/dev use (self-signed cert)
    # or a small deployment terminating TLS itself. For anything public,
    # prefer a reverse proxy (nginx/Caddy) doing real ACME/Let's Encrypt
    # certs in front of a plain-HTTP bridge - that's also the pattern the
    # jmap-perl reference server documents (SETUP.md).
    ssl_keyfile = os.environ.get("JMAP_BRIDGE_SSL_KEYFILE")
    ssl_certfile = os.environ.get("JMAP_BRIDGE_SSL_CERTFILE")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        ssl_keyfile=ssl_keyfile,
        ssl_certfile=ssl_certfile,
    )


if __name__ == "__main__":
    main()
