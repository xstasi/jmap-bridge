from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from jmap_bridge.backends.imap.client import MailboxStatus
from jmap_bridge.backends.imap.modseq_state import encode_email_id
from jmap_bridge.errors import CannotCalculateChanges, InvalidArguments
from jmap_bridge.types import thread as thread_types

ROOT_MSG = b"""\
From: Alice <alice@example.com>
To: Bob <bob@example.com>
Subject: Original
Date: Mon, 1 Jan 2024 12:00:00 +0000
Message-Id: <root@example.com>
Content-Type: text/plain; charset=utf-8

Hello.
"""

REPLY_MSG = b"""\
From: Bob <bob@example.com>
To: Alice <alice@example.com>
Subject: Re: Original
Date: Mon, 1 Jan 2024 13:00:00 +0000
Message-Id: <reply@example.com>
In-Reply-To: <root@example.com>
References: <root@example.com>
Content-Type: text/plain; charset=utf-8

Hi.
"""

UNRELATED_MSG = b"""\
From: Carol <carol@example.com>
To: Alice <alice@example.com>
Subject: Unrelated
Date: Mon, 1 Jan 2024 14:00:00 +0000
Message-Id: <unrelated@example.com>
Content-Type: text/plain; charset=utf-8

Hey.
"""


def _header_block(raw: bytes) -> bytes:
    return raw.split(b"\n\n", 1)[0] + b"\n\n"


class FakeConn:
    def __init__(self):
        self._mailboxes = {}
        self._selected = None

    def add_mailbox(self, name, uidvalidity=1, highestmodseq=1):
        self._mailboxes[name] = {
            "uidvalidity": uidvalidity, "highestmodseq": highestmodseq, "next_uid": 1, "messages": {}
        }

    def add_message(self, mailbox, raw):
        mb = self._mailboxes[mailbox]
        uid = mb["next_uid"]
        mb["next_uid"] += 1
        mb["messages"][uid] = {"raw": raw, "flags": set(), "internaldate": datetime(2024, 1, 1, tzinfo=timezone.utc)}
        return uid

    async def list_mailboxes(self):
        return [(frozenset(), "/", name) for name in self._mailboxes]

    async def status(self, mailbox):
        mb = self._mailboxes[mailbox]
        return MailboxStatus(
            uidvalidity=mb["uidvalidity"], highestmodseq=mb["highestmodseq"],
            uidnext=mb["next_uid"], exists=len(mb["messages"]), unseen=len(mb["messages"]),
        )

    async def select(self, mailbox, readonly=True):
        self._selected = mailbox
        return await self.status(mailbox)

    async def search(self, criteria="ALL"):
        return sorted(self._mailboxes[self._selected]["messages"].keys())

    async def fetch(self, uids, data_items):
        mb = self._mailboxes[self._selected]
        result = {}
        for uid in uids:
            if uid not in mb["messages"]:
                continue
            raw = mb["messages"][uid]["raw"]
            result[uid] = {b"RFC822.HEADER": _header_block(raw)}
        return result


class FakeContext:
    account_id = "Aalice"

    def __init__(self, conn: FakeConn):
        self._conn = conn

    def require_account(self, account_id):
        assert account_id == self.account_id

    def imap(self):
        @asynccontextmanager
        async def _cm():
            yield self._conn

        return _cm()


async def test_thread_get_groups_by_reference_chain():
    conn = FakeConn()
    conn.add_mailbox("INBOX", uidvalidity=1)
    root_uid = conn.add_message("INBOX", ROOT_MSG)
    reply_uid = conn.add_message("INBOX", REPLY_MSG)
    conn.add_message("INBOX", UNRELATED_MSG)
    ctx = FakeContext(conn)

    # Discover the thread id the same way Email/get would: derive it from
    # the root message's own headers (its threadId == itself, no References).
    from jmap_bridge.backends.imap.email_map import derive_thread_id_from_headers

    thread_id = derive_thread_id_from_headers(_header_block(ROOT_MSG), fallback="unused")

    result = await thread_types.thread_get(ctx, {"ids": [thread_id]})
    assert result["notFound"] == []
    assert len(result["list"]) == 1
    email_ids = set(result["list"][0]["emailIds"])
    assert email_ids == {
        encode_email_id("INBOX", 1, root_uid),
        encode_email_id("INBOX", 1, reply_uid),
    }


async def test_thread_get_not_found():
    conn = FakeConn()
    conn.add_mailbox("INBOX", uidvalidity=1)
    conn.add_message("INBOX", ROOT_MSG)
    ctx = FakeContext(conn)

    result = await thread_types.thread_get(ctx, {"ids": ["Tnonexistent"]})
    assert result["list"] == []
    assert result["notFound"] == ["Tnonexistent"]


async def test_thread_get_requires_ids():
    conn = FakeConn()
    ctx = FakeContext(conn)
    with pytest.raises(InvalidArguments):
        await thread_types.thread_get(ctx, {})


async def test_thread_changes_always_cannot_calculate():
    conn = FakeConn()
    ctx = FakeContext(conn)
    with pytest.raises(CannotCalculateChanges):
        await thread_types.thread_changes(ctx, {"sinceState": "x"})
