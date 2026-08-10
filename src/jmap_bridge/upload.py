"""Blob upload/download (RFC 8620 SS6): `POST /upload/{accountId}` stages
bytes in the in-process `BlobCache` (blob_cache.py) for a subsequent
`Email/set`/`Email/import`; `GET /download/{accountId}/{blobId}/{name}`
serves either a staged upload or - for blobIds produced by
`email_map.encode_blob_id` - a specific part of a real backend message,
fetched live over IMAP (never cached to disk).
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from jmap_bridge.auth import authenticate_request
from jmap_bridge.backends.imap.client import ImapError
from jmap_bridge.backends.imap.email_map import decode_blob_id, extract_blob_part
from jmap_bridge.blob_cache import BlobCache
from jmap_bridge.config import BridgeConfig
from jmap_bridge.context import RequestContext
from jmap_bridge.errors import RequestError
from jmap_bridge.pool import ImapConnectionPool
from jmap_bridge.session import encode_account_id

MAX_UPLOAD_BYTES = 50_000_000


async def handle_upload(
    request: Request, config: BridgeConfig, pool: ImapConnectionPool, blob_cache: BlobCache
) -> Response:
    try:
        credentials = authenticate_request(request.headers.get("authorization"), config)
    except RequestError as exc:
        return JSONResponse(exc.to_problem(), status_code=exc.status)

    account_id = request.path_params["account_id"]
    if account_id != encode_account_id(credentials.email):
        return JSONResponse({"type": "notFound"}, status_code=404)

    body = await request.body()
    if len(body) > MAX_UPLOAD_BYTES:
        return JSONResponse({"type": "requestTooLarge"}, status_code=413)

    content_type = request.headers.get("content-type", "application/octet-stream")
    try:
        blob_id = blob_cache.put(body, content_type)
    except MemoryError:
        return JSONResponse({"type": "serverUnavailable"}, status_code=503)

    return JSONResponse(
        {"accountId": account_id, "blobId": blob_id, "type": content_type, "size": len(body)}
    )


async def handle_download(
    request: Request, config: BridgeConfig, pool: ImapConnectionPool, blob_cache: BlobCache
) -> Response:
    try:
        credentials = authenticate_request(request.headers.get("authorization"), config)
    except RequestError as exc:
        return JSONResponse(exc.to_problem(), status_code=exc.status)

    account_id = request.path_params["account_id"]
    blob_id = request.path_params["blob_id"]
    if account_id != encode_account_id(credentials.email):
        return JSONResponse({"type": "notFound"}, status_code=404)

    staged = blob_cache.get(blob_id)
    if staged is not None:
        data, content_type = staged
        return Response(data, media_type=content_type)

    try:
        mailbox, uidvalidity, uid, part_index = decode_blob_id(blob_id)
    except ValueError:
        return JSONResponse({"type": "notFound"}, status_code=404)

    ctx = RequestContext(credentials=credentials, config=config, pool=pool, blob_cache=blob_cache)
    try:
        async with ctx.imap() as conn:
            status = await conn.select(mailbox, readonly=True)
            if status.uidvalidity != uidvalidity:
                return JSONResponse({"type": "notFound"}, status_code=404)
            fetched = await conn.fetch([uid], ["RFC822"])
    except ImapError:
        return JSONResponse({"type": "serverUnavailable"}, status_code=503)

    data = fetched.get(uid, {}).get(b"RFC822")
    if data is None:
        return JSONResponse({"type": "notFound"}, status_code=404)

    part = extract_blob_part(data, part_index)
    if part is None:
        return JSONResponse({"type": "notFound"}, status_code=404)
    payload, content_type = part
    return Response(payload, media_type=content_type)
