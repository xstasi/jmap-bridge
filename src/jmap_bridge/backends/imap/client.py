"""Thin async wrapper around `imapclient.IMAPClient`.

`imapclient` is a synchronous/blocking library, so every call here runs the
underlying blocking call via `asyncio.to_thread`. A single `ImapConnection`
wraps one authenticated `IMAPClient` and is not safe for concurrent use
from multiple coroutines at once (IMAP itself is stateful — one mailbox
SELECTed at a time) — `pool.py` is responsible for exclusive checkout.

CONDSTORE (RFC 7162) is fully supported here: `select()` returns
HIGHESTMODSEQ/UIDVALIDITY, and `fetch_changed_since()` uses the
CHANGEDSINCE fetch modifier, both via `imapclient`'s public API (confirmed
against its own test suite). QRESYNC's VANISHED response, however, is not
parsed by `imapclient` at all (no support in its response parser) — rather
than hand-roll untagged-response parsing against a library that has zero
support for it (real risk of corrupting connection state on a malformed
parse), `supports_qresync_vanished()` always reports unavailable for now.
Callers must treat "destroyed" detection as unavailable and fall back to
`CannotCalculateChanges` until this is implemented properly, tested against
a real QRESYNC server, and it's worth a small `IMAPClient` subclass or a
dedicated protocol library.
"""

from __future__ import annotations

import asyncio
import re
import ssl as ssl_module
from dataclasses import dataclass

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError


class ImapError(Exception):
    """Wraps any imapclient/IMAP protocol failure."""


@dataclass(frozen=True, slots=True)
class MailboxStatus:
    uidvalidity: int
    highestmodseq: int | None  # None if server doesn't support CONDSTORE
    uidnext: int
    exists: int
    unseen: int


DEFAULT_SOCKET_TIMEOUT_SECONDS = 30.0


class ImapConnection:
    def __init__(self, client: IMAPClient):
        self._client = client
        self._selected_mailbox: str | None = None

    @classmethod
    async def connect_and_login(
        cls,
        host: str,
        port: int,
        tls: str,
        username: str,
        password: str,
        timeout: float = DEFAULT_SOCKET_TIMEOUT_SECONDS,
    ) -> "ImapConnection":
        def _connect() -> IMAPClient:
            # Without a timeout, a hung server (bad SORT support, a
            # dropped connection that never sends a RST, ...) blocks the
            # to_thread worker forever - discovered live: a stuck IMAP
            # call kept a bridge process alive through SIGTERM, needing
            # SIGKILL. imapclient applies this to *every* socket op on
            # the connection (connect and each subsequent read/write),
            # not just the initial connect.
            if tls == "implicit":
                client = IMAPClient(host, port=port, ssl=True, timeout=timeout)
            elif tls == "starttls":
                client = IMAPClient(host, port=port, ssl=False, timeout=timeout)
                client.starttls(ssl_context=ssl_module.create_default_context())
            elif tls == "plain":
                client = IMAPClient(host, port=port, ssl=False, timeout=timeout)
            else:
                raise ImapError(f"unknown tls mode: {tls!r}")
            client.login(username, password)
            return client

        try:
            client = await asyncio.to_thread(_connect)
        except (IMAPClientError, OSError) as exc:
            raise ImapError(f"IMAP connect/login to {host}:{port} failed: {exc}") from exc
        return cls(client)

    async def logout(self) -> None:
        try:
            await asyncio.to_thread(self._client.logout)
        except (IMAPClientError, OSError):
            pass  # best-effort on teardown

    async def capabilities(self) -> frozenset[str]:
        caps = await asyncio.to_thread(self._client.capabilities)
        return frozenset(c.decode("ascii").upper() if isinstance(c, bytes) else c.upper() for c in caps)

    async def supports_condstore(self) -> bool:
        return "CONDSTORE" in await self.capabilities()

    async def supports_qresync_vanished(self) -> bool:
        # See module docstring: not implemented, always False for now.
        return False

    async def list_mailboxes(self) -> list[tuple[frozenset[str], str, str]]:
        """Returns (flags, delimiter, name) tuples, RFC 3501 LIST semantics."""
        raw = await asyncio.to_thread(self._client.list_folders)
        result = []
        for flags, delimiter, name in raw:
            decoded_flags = frozenset(
                f.decode("ascii") if isinstance(f, bytes) else f for f in flags
            )
            delim = delimiter.decode("ascii") if isinstance(delimiter, bytes) else delimiter
            result.append((decoded_flags, delim, name))
        return result

    async def status(self, mailbox: str) -> MailboxStatus:
        """STATUS without SELECTing — cheap, used for Mailbox/changes to
        check every mailbox's cursor without disturbing a selected state.
        """
        try:
            raw = await asyncio.to_thread(
                self._client.folder_status,
                mailbox,
                ["UIDVALIDITY", "UIDNEXT", "MESSAGES", "UNSEEN", "HIGHESTMODSEQ"],
            )
        except (IMAPClientError, OSError) as exc:
            raise ImapError(f"STATUS {mailbox!r} failed: {exc}") from exc
        return MailboxStatus(
            uidvalidity=int(raw[b"UIDVALIDITY"]),
            highestmodseq=int(raw[b"HIGHESTMODSEQ"]) if b"HIGHESTMODSEQ" in raw else None,
            uidnext=int(raw[b"UIDNEXT"]),
            exists=int(raw[b"MESSAGES"]),
            unseen=int(raw[b"UNSEEN"]),
        )

    async def select(self, mailbox: str, readonly: bool = True) -> MailboxStatus:
        try:
            raw = await asyncio.to_thread(self._client.select_folder, mailbox, readonly)
        except (IMAPClientError, OSError) as exc:
            raise ImapError(f"SELECT {mailbox!r} failed: {exc}") from exc
        self._selected_mailbox = mailbox
        return MailboxStatus(
            uidvalidity=int(raw[b"UIDVALIDITY"]),
            highestmodseq=int(raw[b"HIGHESTMODSEQ"]) if b"HIGHESTMODSEQ" in raw else None,
            uidnext=int(raw[b"UIDNEXT"]),
            exists=int(raw.get(b"EXISTS", 0)),
            # SELECT's untagged UNSEEN response (RFC 3501 SS7.3.2) is the
            # *sequence number* of the first unseen message, not a count -
            # unlike STATUS UNSEEN (status(), below), which is a real
            # count. Parsing it as a count was simply wrong; nothing reads
            # select()'s .unseen, so don't populate it from a mismatched field.
            unseen=0,
        )

    async def search(self, criteria: list | str = "ALL") -> list[int]:
        try:
            return list(await asyncio.to_thread(self._client.search, criteria))
        except (IMAPClientError, OSError) as exc:
            raise ImapError(f"SEARCH failed: {exc}") from exc

    async def sort(self, sort_criteria: list[str], criteria: list | str = "ALL") -> list[int]:
        """SORT (RFC 5256) - filtered *and* server-side sorted UIDs in one
        round trip, so Email/query never needs to pull message bodies
        just to order results. Not all servers support this extension
        (check `capabilities()` for "SORT" first); raises ImapError if
        the server rejects it, which callers should treat as a signal to
        fall back to SEARCH + a lightweight per-key FETCH.
        """
        try:
            return list(await asyncio.to_thread(self._client.sort, sort_criteria, criteria))
        except (IMAPClientError, OSError) as exc:
            raise ImapError(f"SORT failed: {exc}") from exc

    async def fetch(self, uids: list[int] | str, data_items: list[str]) -> dict[int, dict]:
        try:
            return await asyncio.to_thread(self._client.fetch, uids, data_items)
        except (IMAPClientError, OSError) as exc:
            raise ImapError(f"FETCH failed: {exc}") from exc

    async def fetch_changed_since(
        self, modseq: int, data_items: list[str] | None = None
    ) -> dict[int, dict]:
        """Messages with MODSEQ > `modseq` in the currently-selected
        mailbox, via CONDSTORE's CHANGEDSINCE fetch modifier (RFC 7162).
        Requires CONDSTORE support (checked by the caller beforehand).
        """
        items = data_items or ["FLAGS", "UID"]
        try:
            return await asyncio.to_thread(
                self._client.fetch, "1:*", items, [f"CHANGEDSINCE {modseq}"]
            )
        except (IMAPClientError, OSError) as exc:
            raise ImapError(f"FETCH CHANGEDSINCE {modseq} failed: {exc}") from exc

    async def append(self, mailbox: str, message: bytes, flags: tuple[str, ...] = ()) -> int | None:
        """APPEND `message` to `mailbox`. Returns the new message's UID if
        the server supports UIDPLUS (RFC 4315, near-universal - Dovecot
        enables it by default) and reports it via an APPENDUID response;
        otherwise None, and the caller must locate the message another way
        (e.g. searching by Message-Id).
        """
        try:
            response = await asyncio.to_thread(self._client.append, mailbox, message, flags)
        except (IMAPClientError, OSError) as exc:
            raise ImapError(f"APPEND to {mailbox!r} failed: {exc}") from exc
        if isinstance(response, bytes):
            response = response.decode("ascii", errors="replace")
        match = re.search(r"\[APPENDUID\s+\d+\s+(\d+)\]", response or "")
        return int(match.group(1)) if match else None

    async def copy(self, uids: list[int], destination: str) -> None:
        try:
            await asyncio.to_thread(self._client.copy, uids, destination)
        except (IMAPClientError, OSError) as exc:
            raise ImapError(f"COPY to {destination!r} failed: {exc}") from exc

    async def move(self, uids: list[int], destination: str) -> None:
        try:
            await asyncio.to_thread(self._client.move, uids, destination)
        except (IMAPClientError, OSError) as exc:
            raise ImapError(f"MOVE to {destination!r} failed: {exc}") from exc

    async def set_flags(self, uids: list[int], flags: list[str]) -> None:
        try:
            await asyncio.to_thread(self._client.set_flags, uids, flags)
        except (IMAPClientError, OSError) as exc:
            raise ImapError(f"STORE FLAGS failed: {exc}") from exc

    async def add_flags(self, uids: list[int], flags: list[str]) -> None:
        try:
            await asyncio.to_thread(self._client.add_flags, uids, flags)
        except (IMAPClientError, OSError) as exc:
            raise ImapError(f"STORE +FLAGS failed: {exc}") from exc

    async def remove_flags(self, uids: list[int], flags: list[str]) -> None:
        try:
            await asyncio.to_thread(self._client.remove_flags, uids, flags)
        except (IMAPClientError, OSError) as exc:
            raise ImapError(f"STORE -FLAGS failed: {exc}") from exc

    async def expunge(self, uids: list[int] | None = None) -> None:
        try:
            await asyncio.to_thread(self._client.expunge, uids)
        except (IMAPClientError, OSError) as exc:
            raise ImapError(f"EXPUNGE failed: {exc}") from exc

    async def create_folder(self, mailbox: str) -> None:
        try:
            await asyncio.to_thread(self._client.create_folder, mailbox)
        except (IMAPClientError, OSError) as exc:
            raise ImapError(f"CREATE {mailbox!r} failed: {exc}") from exc

    async def delete_folder(self, mailbox: str) -> None:
        try:
            await asyncio.to_thread(self._client.delete_folder, mailbox)
        except (IMAPClientError, OSError) as exc:
            raise ImapError(f"DELETE {mailbox!r} failed: {exc}") from exc

    async def rename_folder(self, old_name: str, new_name: str) -> None:
        try:
            await asyncio.to_thread(self._client.rename_folder, old_name, new_name)
        except (IMAPClientError, OSError) as exc:
            raise ImapError(f"RENAME {old_name!r} -> {new_name!r} failed: {exc}") from exc
