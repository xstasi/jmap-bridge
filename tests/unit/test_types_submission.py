from contextlib import asynccontextmanager

import pytest

from jmap_bridge.auth import Credentials
from jmap_bridge.backends.imap.client import MailboxStatus
from jmap_bridge.backends.imap.modseq_state import encode_email_id
from jmap_bridge.backends.smtp.client import SmtpError
from jmap_bridge.config import load_config
from jmap_bridge.context import RequestContext
from jmap_bridge.pool import ImapConnectionPool
from jmap_bridge.types import submission as submission_types

EXAMPLE_CONFIG = "/home/sonne/local/lab/jmap/config/domains.example.yaml"

MSG = b"""\
From: Alice <alice@example.com>
To: Bob <bob@example.com>
Cc: carol@example.com
Subject: Hello
Date: Mon, 1 Jan 2024 12:00:00 +0000
Message-Id: <msg1@example.com>
Content-Type: text/plain; charset=utf-8

Hi Bob.
"""


class FakeConn:
    def __init__(self, mailbox_name, uidvalidity, uid, raw):
        self.mailbox_name = mailbox_name
        self.uidvalidity = uidvalidity
        self.uid = uid
        self.raw = raw
        self._selected = None

    async def select(self, mailbox, readonly=True):
        self._selected = mailbox
        return MailboxStatus(
            uidvalidity=self.uidvalidity, highestmodseq=1, uidnext=self.uid + 1, exists=1, unseen=0
        )

    async def fetch(self, uids, data_items):
        if self.uid in uids:
            return {self.uid: {b"RFC822": self.raw}}
        return {}


class FakeContext:
    def __init__(self, conn, credentials):
        self._conn = conn
        self.credentials = credentials
        self.account_id = "Aalice"

    def require_account(self, account_id):
        assert account_id == self.account_id

    def imap(self):
        @asynccontextmanager
        async def _cm():
            yield self._conn

        return _cm()


def _ctx(conn):
    config = load_config(EXAMPLE_CONFIG)
    creds = Credentials(
        email="alice@example.com", password="pw", domain="example.com",
        domain_config=config.domains["example.com"],
    )
    return FakeContext(conn, creds)


async def test_email_submission_set_derives_recipients_from_headers(monkeypatch):
    sent = {}

    async def fake_send_message(**kwargs):
        sent.update(kwargs)

    monkeypatch.setattr(submission_types, "send_message", fake_send_message)

    email_id = encode_email_id("INBOX", 1, 42)
    conn = FakeConn("INBOX", 1, 42, MSG)
    ctx = _ctx(conn)

    result = await submission_types.email_submission_set(ctx, {"create": {"s1": {"emailId": email_id}}})
    assert "s1" in result["created"]
    assert result["notCreated"] == {}
    assert sent["sender"] == "alice@example.com"
    assert set(sent["recipients"]) == {"bob@example.com", "carol@example.com"}


async def test_email_submission_set_uses_explicit_envelope(monkeypatch):
    sent = {}

    async def fake_send_message(**kwargs):
        sent.update(kwargs)

    monkeypatch.setattr(submission_types, "send_message", fake_send_message)

    email_id = encode_email_id("INBOX", 1, 42)
    conn = FakeConn("INBOX", 1, 42, MSG)
    ctx = _ctx(conn)

    result = await submission_types.email_submission_set(
        ctx,
        {
            "create": {
                "s1": {
                    "emailId": email_id,
                    "envelope": {
                        "mailFrom": {"email": "custom@example.com"},
                        "rcptTo": [{"email": "override@example.com"}],
                    },
                }
            }
        },
    )
    assert "s1" in result["created"]
    assert sent["sender"] == "custom@example.com"
    assert sent["recipients"] == ["override@example.com"]


async def test_email_submission_set_requires_email_id():
    conn = FakeConn("INBOX", 1, 42, MSG)
    ctx = _ctx(conn)
    result = await submission_types.email_submission_set(ctx, {"create": {"s1": {}}})
    assert result["notCreated"]["s1"]["type"] == "invalidArguments"


async def test_email_submission_set_stale_email_id():
    email_id = encode_email_id("INBOX", 999, 42)  # wrong uidvalidity
    conn = FakeConn("INBOX", 1, 42, MSG)
    ctx = _ctx(conn)
    result = await submission_types.email_submission_set(ctx, {"create": {"s1": {"emailId": email_id}}})
    assert result["notCreated"]["s1"]["type"] == "invalidArguments"


async def test_email_submission_set_smtp_failure(monkeypatch):
    async def fake_send_message(**kwargs):
        raise SmtpError("connection refused")

    monkeypatch.setattr(submission_types, "send_message", fake_send_message)

    email_id = encode_email_id("INBOX", 1, 42)
    conn = FakeConn("INBOX", 1, 42, MSG)
    ctx = _ctx(conn)
    result = await submission_types.email_submission_set(ctx, {"create": {"s1": {"emailId": email_id}}})
    assert result["notCreated"]["s1"]["type"] == "serverFail"
