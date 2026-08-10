"""Credential-passthrough connection pool (see plan SS5): keeps a small
number of authenticated IMAP connections warm per `(domain, username)`,
entirely in-process memory — nothing is ever written to disk, and nothing
is shared with an external cache. A bug here is a security bug, not just a
performance bug: review changes to this file with that in mind.

Password handling: the pool key deliberately excludes the password, so a
password rotation doesn't get silently masked by a stale cached session.
Instead, each pooled entry carries a SHA-256 hash of the password that
authenticated it; on checkout we compare hashes with `hmac.compare_digest`
and evict (never reuse) any entry whose hash doesn't match the password
just presented. The plaintext password itself is only ever held in a local
variable for the duration of opening a fresh connection — never stored in
a pool entry, never serialized.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from jmap_bridge.backends.imap.client import ImapConnection, ImapError

logger = logging.getLogger(__name__)

PoolKey = tuple[str, str]  # (domain, username)

DEFAULT_MAX_PER_USER = 4
DEFAULT_IDLE_EVICTION_SECONDS = 300
SWEEP_INTERVAL_SECONDS = 30


@dataclass(slots=True)
class _Entry:
    connection: ImapConnection
    password_hash: bytes
    last_used: float


class ImapConnectionPool:
    def __init__(self) -> None:
        self._idle: dict[PoolKey, list[_Entry]] = {}
        self._open_count: dict[PoolKey, int] = {}
        self._max_per_user: dict[PoolKey, int] = {}
        self._idle_eviction_seconds: dict[PoolKey, int] = {}
        self._condition = asyncio.Condition()
        self._sweep_task: asyncio.Task | None = None

    def start(self) -> None:
        if self._sweep_task is None:
            self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweep_task
            self._sweep_task = None
        async with self._condition:
            for entries in self._idle.values():
                for entry in entries:
                    await entry.connection.logout()
            self._idle.clear()
            self._open_count.clear()

    @staticmethod
    def _hash_password(password: str) -> bytes:
        return hashlib.sha256(password.encode("utf-8")).digest()

    @asynccontextmanager
    async def checkout(
        self,
        *,
        domain: str,
        username: str,
        password: str,
        host: str,
        port: int,
        tls: str,
        max_per_user: int = DEFAULT_MAX_PER_USER,
        idle_eviction_seconds: int = DEFAULT_IDLE_EVICTION_SECONDS,
    ):
        """Check out an authenticated IMAP connection for `(domain,
        username)`, opening a fresh one (up to `max_per_user` concurrent)
        if none are idle, or reusing an idle one whose stored password hash
        matches. A connection that raised `ImapError` inside the `with`
        block is treated as possibly-corrupted and closed rather than
        returned to the pool.
        """
        key = (domain, username)
        password_hash = self._hash_password(password)
        connection = await self._acquire(
            key, password_hash, host, port, tls, username, password, max_per_user, idle_eviction_seconds
        )
        try:
            yield connection
        except ImapError:
            async with self._condition:
                self._open_count[key] = self._open_count.get(key, 1) - 1
                self._condition.notify_all()
            await connection.logout()
            raise
        else:
            async with self._condition:
                self._idle.setdefault(key, []).append(
                    _Entry(connection, password_hash, time.monotonic())
                )
                self._condition.notify_all()

    async def _acquire(
        self,
        key: PoolKey,
        password_hash: bytes,
        host: str,
        port: int,
        tls: str,
        username: str,
        password: str,
        max_per_user: int,
        idle_eviction_seconds: int,
    ) -> ImapConnection:
        stale: list[_Entry] = []
        async with self._condition:
            self._max_per_user[key] = max_per_user
            self._idle_eviction_seconds[key] = idle_eviction_seconds
            while True:
                entries = self._idle.setdefault(key, [])
                for i, entry in enumerate(entries):
                    if hmac.compare_digest(entry.password_hash, password_hash):
                        return entries.pop(i).connection

                if entries:
                    # Every idle entry has a stale (rotated) password - evict
                    # them all rather than silently reusing an old session.
                    stale.extend(entries)
                    self._open_count[key] = self._open_count.get(key, 0) - len(entries)
                    entries.clear()

                if self._open_count.get(key, 0) < max_per_user:
                    self._open_count[key] = self._open_count.get(key, 0) + 1
                    break
                await self._condition.wait()

        for entry in stale:
            asyncio.create_task(entry.connection.logout())  # noqa: RUF006 - fire-and-forget cleanup

        try:
            return await ImapConnection.connect_and_login(host, port, tls, username, password)
        except Exception:
            async with self._condition:
                self._open_count[key] = self._open_count.get(key, 1) - 1
                self._condition.notify_all()
            raise

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            await self._sweep_once()

    async def _sweep_once(self) -> None:
        """Evict idle connections older than their key's idle-eviction
        threshold. Split out from `_sweep_loop` so tests can trigger a
        sweep deterministically instead of waiting on a real clock.
        """
        async with self._condition:
            now = time.monotonic()
            expired: list[_Entry] = []
            for key, entries in list(self._idle.items()):
                threshold = self._idle_eviction_seconds.get(key, DEFAULT_IDLE_EVICTION_SECONDS)
                keep = []
                for entry in entries:
                    if now - entry.last_used > threshold:
                        expired.append(entry)
                        self._open_count[key] = self._open_count.get(key, 1) - 1
                    else:
                        keep.append(entry)
                self._idle[key] = keep
            self._condition.notify_all()
        for entry in expired:
            await entry.connection.logout()
