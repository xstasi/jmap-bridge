import base64

import pytest
from starlette.testclient import TestClient

from jmap_bridge.app import create_app
from jmap_bridge.config import load_config
from jmap_bridge.push import DEFAULT_PING_SECONDS, event_stream, parse_ping_seconds

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
    events = [chunk async for chunk in event_stream(ping_seconds=30, close_after_state=True)]
    assert events == []


async def test_event_stream_sends_bounded_pings(monkeypatch):
    import jmap_bridge.push as push_module

    slept_for = []

    async def instant_sleep(seconds):
        slept_for.append(seconds)

    monkeypatch.setattr(push_module.asyncio, "sleep", instant_sleep)

    events = [
        chunk
        async for chunk in event_stream(ping_seconds=7, close_after_state=False, max_events=3)
    ]
    assert len(events) == 3
    assert slept_for == [7, 7, 7]
    for event in events:
        assert event == 'event: ping\ndata: {"interval": 7}\n\n'
