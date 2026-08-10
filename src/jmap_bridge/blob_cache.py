"""Transient in-memory staging area for uploaded blobs (RFC 8620 SS6.1).

This is a deliberate, narrow exception to "no local storage": a client
must be able to POST attachment bytes to `/upload` *before* they're
referenced by an `Email/set`/`Email/import` call, so *something* has to
hold those bytes in the gap between the two requests - even a fully
stateful bridge stages blobs this way. The scope here is kept as small as
that gap requires: process memory only (never disk), a short TTL, and a
total-size cap, so a restart or a slow client simply means "upload again"
rather than a durable store to reason about.

Blob ids are unguessable 128-bit tokens; combined with every request
requiring valid backend credentials, that's adequate protection against a
stranger requesting your blob, but it does *not* scope a blob to a
specific account the way encode_email_id ties an Email id to real backend
data - a compromised or malicious user on the *same* domain who obtains
another user's blob id (e.g. via a leaked URL) could fetch it. Acceptable
for the MVP's single-tenant-credential-passthrough model; flagged here
rather than silently assumed safe.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

DEFAULT_TTL_SECONDS = 600
DEFAULT_MAX_TOTAL_BYTES = 200_000_000


@dataclass(slots=True)
class _BlobEntry:
    data: bytes
    content_type: str
    expires_at: float


class BlobCache:
    def __init__(
        self,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_total_bytes = max_total_bytes
        self._entries: dict[str, _BlobEntry] = {}
        self._total_bytes = 0

    def _sweep_expired(self) -> None:
        now = time.monotonic()
        expired = [bid for bid, e in self._entries.items() if e.expires_at <= now]
        for bid in expired:
            self._total_bytes -= len(self._entries.pop(bid).data)

    def put(self, data: bytes, content_type: str) -> str:
        self._sweep_expired()
        if self._total_bytes + len(data) > self._max_total_bytes:
            raise MemoryError("blob cache capacity exceeded")
        blob_id = "U" + uuid.uuid4().hex
        self._entries[blob_id] = _BlobEntry(
            data=data, content_type=content_type, expires_at=time.monotonic() + self._ttl_seconds
        )
        self._total_bytes += len(data)
        return blob_id

    def get(self, blob_id: str) -> tuple[bytes, str] | None:
        self._sweep_expired()
        entry = self._entries.get(blob_id)
        if entry is None:
            return None
        return entry.data, entry.content_type
