import asyncio
import base64

import pytest
from starlette.testclient import TestClient

from jmap_bridge.app import create_app
from jmap_bridge.auth import Credentials
from jmap_bridge.backends.imap.client import ImapError
from jmap_bridge.config import load_config
from jmap_bridge.push import (
    DEFAULT_PING_SECONDS,
    AccountWatcher,
    AccountWatcherRegistry,
    event_stream,
    parse_ping_seconds,
)
from jmap_bridge.session import encode_account_id

EXAMPLE_CONFIG = "/home/sonne/local/lab/jmap/config/domains.example.yaml"


def _basic_header(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client():
    config = load_config(EXAMPLE_CONFIG)
    app = create_app(config, "https://bridge.example")
    with TestClient(app) as c:
        yield c


def _credentials():
    config = load_config(EXAMPLE_CONFIG)
    return Credentials(
        email="alice@example.com", password="pw", domain="example.com",
        domain_config=config.domains["example.com"],
    )


def test_events_requires_auth(client):
    response = client.get("/events")
    assert response.status_code == 401


def test_events_closeafter_state_closes_immediately(client):
    headers = _basic_header("alice@example.com", "pw")
    with client.stream("GET", "/events?closeafter=state", headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = b"".join(response.iter_bytes())
    assert body == b""


def test_parse_ping_seconds_default():
    assert parse_ping_seconds(None) == DEFAULT_PING_SECONDS
    assert parse_ping_seconds("0") == DEFAULT_PING_SECONDS


def test_parse_ping_seconds_explicit():
    assert parse_ping_seconds("5") == 5


def test_parse_ping_seconds_clamped_and_invalid():
    assert parse_ping_seconds("999999999") == 3600
    assert parse_ping_seconds("not-a-number") == DEFAULT_PING_SECONDS
    assert parse_ping_seconds("-5") == DEFAULT_PING_SECONDS


async def test_event_stream_close_after_state_yields_nothing():
    events = [
        chunk async for chunk in event_stream(asyncio.Queue(), ping_seconds=30, close_after_state=True)
    ]
    assert events == []


async def test_event_stream_sends_pings_when_queue_is_idle():
    queue = asyncio.Queue()
    events = [
        chunk
        async for chunk in event_stream(queue, ping_seconds=0.01, close_after_state=False, max_events=3)
    ]
    assert len(events) == 3
    for event in events:
        assert event == 'event: ping\ndata: {"interval": 0.01}\n\n'


async def test_event_stream_sends_state_event_from_queue():
    queue = asyncio.Queue()
    payload = {"@type": "StateChange", "changed": {"Aalice": {"Email": "somestate"}}}
    await queue.put(payload)
    events = [
        chunk
        async for chunk in event_stream(queue, ping_seconds=10, close_after_state=False, max_events=1)
    ]
    assert len(events) == 1
    assert events[0].startswith("event: state\n")
    assert '"somestate"' in events[0]


async def test_event_stream_mixes_state_and_ping_events():
    queue = asyncio.Queue()
    await queue.put({"@type": "StateChange", "changed": {}})
    events = [
        chunk
        async for chunk in event_stream(queue, ping_seconds=0.01, close_after_state=False, max_events=2)
    ]
    assert events[0].startswith("event: state\n")
    assert events[1].startswith("event: ping\n")


async def test_account_watcher_subscribe_starts_task_unsubscribe_stops_it():
    watcher = AccountWatcher(_credentials(), poll_interval_seconds=1000)
    queue = watcher.subscribe()
    assert watcher._task is not None
    is_empty = watcher.unsubscribe(queue)
    assert is_empty is True
    assert watcher._task is None


async def test_account_watcher_registry_shares_watcher_for_same_account():
    registry = AccountWatcherRegistry()
    creds = _credentials()
    watcher1, queue1 = registry.subscribe(creds)
    watcher2, queue2 = registry.subscribe(creds)
    assert watcher1 is watcher2
    assert queue1 is not queue2
    registry.unsubscribe(creds, watcher1, queue1)
    registry.unsubscribe(creds, watcher2, queue2)


async def test_account_watcher_registry_separates_different_accounts():
    registry = AccountWatcherRegistry()
    config = load_config(EXAMPLE_CONFIG)
    creds_a = Credentials(
        email="alice@example.com", password="pw", domain="example.com",
        domain_config=config.domains["example.com"],
    )
    creds_b = Credentials(
        email="bob@example.com", password="pw", domain="example.com",
        domain_config=config.domains["example.com"],
    )
    watcher_a, _ = registry.subscribe(creds_a)
    watcher_b, _ = registry.subscribe(creds_b)
    assert watcher_a is not watcher_b


async def test_account_watcher_notifies_on_cursor_change(monkeypatch):
    import jmap_bridge.push as push_module
    from jmap_bridge.backends.imap.client import MailboxStatus

    class FakeConn:
        def __init__(self):
            self.exists = 1

        async def list_mailboxes(self):
            return [(frozenset(), "/", "INBOX")]

        async def select(self, mailbox, readonly=True):
            return MailboxStatus(
                uidvalidity=1, highestmodseq=self.exists, uidnext=self.exists + 1,
                exists=self.exists, unseen=0,
            )

        async def logout(self):
            pass

    fake_conn = FakeConn()

    async def fake_connect_and_login(*args, **kwargs):
        return fake_conn

    monkeypatch.setattr(push_module.ImapConnection, "connect_and_login", fake_connect_and_login)

    sleep_calls = []

    async def instant_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 3:
            fake_conn.exists = 2  # simulate a new message arriving before poll #3
        if len(sleep_calls) > 4:
            raise asyncio.CancelledError()  # stop the watcher loop after a few polls

    monkeypatch.setattr(push_module.asyncio, "sleep", instant_sleep)

    watcher = AccountWatcher(_credentials(), poll_interval_seconds=0.01)
    queue = watcher.subscribe()

    # _run's own `except asyncio.CancelledError: pass` catches our
    # simulated cancellation (matching how it handles a real
    # unsubscribe-triggered task.cancel()), so the task completes
    # normally rather than raising.
    await watcher._task

    assert queue.qsize() >= 1
    payload = queue.get_nowait()
    assert payload["@type"] == "StateChange"
    account_id = encode_account_id("alice@example.com")
    changed_types = payload["changed"][account_id]
    assert set(changed_types) == {"Mailbox", "Email", "Thread"}
    assert changed_types["Mailbox"] == changed_types["Email"] == changed_types["Thread"]


async def test_account_watcher_survives_connect_failure(monkeypatch):
    import jmap_bridge.push as push_module

    async def failing_connect(*args, **kwargs):
        raise ImapError("connection refused")

    monkeypatch.setattr(push_module.ImapConnection, "connect_and_login", failing_connect)

    call_count = [0]

    async def counting_sleep(seconds):
        call_count[0] += 1
        if call_count[0] > 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(push_module.asyncio, "sleep", counting_sleep)

    watcher = AccountWatcher(_credentials(), poll_interval_seconds=0.01)
    queue = watcher.subscribe()
    await watcher._task

    assert queue.empty()  # never got a connection, so never notified - and didn't crash
