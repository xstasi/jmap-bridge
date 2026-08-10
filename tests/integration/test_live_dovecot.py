"""End-to-end smoke test against a *running* bridge + Dovecot, per
tests/fixtures/docker-compose.test.yml. Not part of the `tests/unit` suite
(pyproject.toml's testpaths excludes this directory) and not run
automatically - bring the stack up yourself first:

    docker compose -f tests/fixtures/docker-compose.test.yml up --build -d

then:

    JMAP_BRIDGE_TEST_URL=http://localhost:8080 pytest tests/integration

This file was written but not executed in the session that authored it
(no Docker access in that sandbox) - treat it as a starting point to
verify, not a proven-passing suite.
"""

from __future__ import annotations

import base64
import os

import httpx
import imapclient
import pytest

BASE_URL = os.environ.get("JMAP_BRIDGE_TEST_URL", "http://localhost:8080")
DOVECOT_HOST = os.environ.get("JMAP_BRIDGE_TEST_DOVECOT_HOST", "localhost")
DOVECOT_PORT = int(os.environ.get("JMAP_BRIDGE_TEST_DOVECOT_PORT", "1143"))

TEST_USER = "alice@example.com"
TEST_PASSWORD = "testpass123"

SEED_MESSAGE = b"""\
From: Alice <alice@example.com>
To: Bob <bob@example.com>
Subject: Integration test message
Date: Mon, 1 Jan 2024 12:00:00 +0000
Message-Id: <integration-test-1@example.com>
Content-Type: text/plain; charset=utf-8

Hello from the integration test.
"""


def _auth_header() -> dict:
    token = base64.b64encode(f"{TEST_USER}:{TEST_PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def dovecot():
    """Direct IMAP connection, bypassing the bridge - used to seed/verify
    mail state independently of the code under test.
    """
    client = imapclient.IMAPClient(DOVECOT_HOST, port=DOVECOT_PORT, ssl=False)
    client.login(TEST_USER, TEST_PASSWORD)
    yield client
    client.logout()


@pytest.fixture
def seeded_message(dovecot):
    dovecot.select_folder("INBOX")
    dovecot.append("INBOX", SEED_MESSAGE, flags=())
    yield


def test_session_endpoint():
    response = httpx.get(f"{BASE_URL}/session", headers=_auth_header())
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == TEST_USER
    assert "urn:ietf:params:jmap:mail" in body["capabilities"]


def test_mailbox_get_sees_inbox(dovecot):
    response = httpx.post(
        f"{BASE_URL}/api",
        headers=_auth_header(),
        json={"methodCalls": [["Mailbox/get", {}, "t0"]]},
    )
    assert response.status_code == 200
    result = response.json()["methodResponses"][0]
    assert result[0] == "Mailbox/get"
    names = [m["name"] for m in result[1]["list"]]
    assert "INBOX" in names


def test_email_query_and_get_round_trip(seeded_message):
    query_response = httpx.post(
        f"{BASE_URL}/api",
        headers=_auth_header(),
        json={
            "methodCalls": [
                [
                    "Email/query",
                    {"filter": {"inMailbox": _inbox_id()}},
                    "q0",
                ]
            ]
        },
    )
    query_result = query_response.json()["methodResponses"][0][1]
    assert query_result["total"] >= 1
    email_id = query_result["ids"][0]

    get_response = httpx.post(
        f"{BASE_URL}/api",
        headers=_auth_header(),
        json={"methodCalls": [["Email/get", {"ids": [email_id]}, "g0"]]},
    )
    get_result = get_response.json()["methodResponses"][0][1]
    assert get_result["list"][0]["subject"] == "Integration test message"


def _inbox_id() -> str:
    response = httpx.post(
        f"{BASE_URL}/api",
        headers=_auth_header(),
        json={"methodCalls": [["Mailbox/get", {"properties": ["name"]}, "t0"]]},
    )
    for mailbox in response.json()["methodResponses"][0][1]["list"]:
        if mailbox["name"] == "INBOX":
            return mailbox["id"]
    raise AssertionError("INBOX not found")


def test_mailbox_changes_reflects_new_message(dovecot):
    before = httpx.post(
        f"{BASE_URL}/api",
        headers=_auth_header(),
        json={"methodCalls": [["Mailbox/get", {}, "t0"]]},
    ).json()["methodResponses"][0][1]
    since_state = before["state"]

    dovecot.select_folder("INBOX")
    dovecot.append("INBOX", SEED_MESSAGE, flags=())

    changes = httpx.post(
        f"{BASE_URL}/api",
        headers=_auth_header(),
        json={"methodCalls": [["Mailbox/changes", {"sinceState": since_state}, "c0"]]},
    ).json()["methodResponses"][0]
    assert changes[0] == "Mailbox/changes"
    assert _inbox_id() in changes[1]["updated"]
