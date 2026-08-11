import pytest

from jmap_bridge.auth import Credentials
from jmap_bridge.backends.imap.client import ImapConnection
from jmap_bridge.config import load_config
from jmap_bridge.context import RequestContext, _RequestCache
from jmap_bridge.pool import ImapConnectionPool

EXAMPLE_CONFIG = "/home/sonne/local/lab/jmap/config/domains.example.yaml"


async def test_request_cache_computes_once_per_key():
    calls = []

    async def compute():
        calls.append(1)
        return "value"

    cache = _RequestCache()
    assert await cache.get_or_compute("k", compute) == "value"
    assert await cache.get_or_compute("k", compute) == "value"
    assert len(calls) == 1


async def test_request_cache_computes_separately_per_key():
    cache = _RequestCache()
    assert await cache.get_or_compute("a", _const("va")) == "va"
    assert await cache.get_or_compute("b", _const("vb")) == "vb"


async def test_request_cache_clear_forces_recompute():
    calls = []

    async def compute():
        calls.append(1)
        return len(calls)

    cache = _RequestCache()
    assert await cache.get_or_compute("k", compute) == 1
    assert await cache.get_or_compute("k", compute) == 1  # still cached
    cache.clear()
    assert await cache.get_or_compute("k", compute) == 2  # recomputed


def _const(value):
    async def compute():
        return value

    return compute


async def test_request_cache_concurrent_calls_compute_only_once():
    import asyncio

    calls = []

    async def compute():
        calls.append(1)
        await asyncio.sleep(0)  # yield, so both callers are in-flight together
        return "value"

    cache = _RequestCache()
    results = await asyncio.gather(
        cache.get_or_compute("k", compute), cache.get_or_compute("k", compute)
    )
    assert results == ["value", "value"]
    assert len(calls) == 1


class FakeConnection:
    _next_id = 0

    def __init__(self):
        FakeConnection._next_id += 1
        self.id = FakeConnection._next_id

    async def logout(self):
        pass


@pytest.fixture(autouse=True)
def reset_fake_connection_counter():
    FakeConnection._next_id = 0
    yield


async def _fake_connect_and_login(host, port, tls, username, password):
    return FakeConnection()


def _make_ctx(monkeypatch, *, max_per_user: int = 4) -> RequestContext:
    monkeypatch.setattr(ImapConnection, "connect_and_login", staticmethod(_fake_connect_and_login))
    config = load_config(EXAMPLE_CONFIG)
    domain_config = config.domains["example.com"].model_copy(
        update={"connection_pool_max_per_user": max_per_user}
    )
    creds = Credentials(
        email="alice@example.com", password="pw", domain="example.com",
        domain_config=domain_config,
    )
    return RequestContext(credentials=creds, config=config, pool=ImapConnectionPool())


async def test_imap_parallel_map_calls_work_for_every_entry(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    entries = ["a", "b", "c", "d", "e"]

    async def work(conn, entry):
        return entry

    results = await ctx.imap_parallel_map(entries, work)
    assert sorted(results) == entries


async def test_imap_parallel_map_empty_entries_returns_empty(monkeypatch):
    ctx = _make_ctx(monkeypatch)

    async def work(conn, entry):
        raise AssertionError("should never be called")

    assert await ctx.imap_parallel_map([], work) == []


async def test_imap_parallel_map_uses_multiple_connections_up_to_explicit_cap(monkeypatch):
    import asyncio

    ctx = _make_ctx(monkeypatch)
    entries = list(range(10))

    async def work(conn, entry):
        # A real await point, so the chunks' tasks genuinely interleave
        # instead of each running to completion (and returning its
        # connection to idle) before the next one even starts its own
        # checkout - without this, the pool would just reuse one
        # connection sequentially and this test couldn't tell the
        # difference from no concurrency at all.
        await asyncio.sleep(0)
        return conn.id

    results = await ctx.imap_parallel_map(entries, work, max_concurrency=3)
    assert len(set(results)) == 3  # exactly 3 distinct connections used


async def test_imap_parallel_map_never_exceeds_entry_count(monkeypatch):
    ctx = _make_ctx(monkeypatch)
    entries = ["only-one"]

    async def work(conn, entry):
        return conn.id

    results = await ctx.imap_parallel_map(entries, work, max_concurrency=5)
    assert len(set(results)) == 1  # capped by entry count, not max_concurrency


async def test_imap_parallel_map_default_concurrency_leaves_pool_headroom(monkeypatch):
    """Default cap is connection_pool_max_per_user - 2 (capped at 2): -1
    for the caller's own connection (held for the LIST call and kept
    checked out for the sweep's whole duration), -1 more so a sweep
    never claims the rest of the budget either - a concurrent request
    sharing the same account's pool budget should never be the one left
    waiting because a sweep took everything."""
    import asyncio

    ctx = _make_ctx(monkeypatch, max_per_user=4)
    entries = list(range(10))

    async def work(conn, entry):
        await asyncio.sleep(0)  # see the explicit-cap test for why
        return conn.id

    results = await ctx.imap_parallel_map(entries, work)
    assert len(set(results)) == 2  # min(2, 4-2) = 2, not the full pool max of 4


async def test_imap_parallel_map_default_concurrency_floors_at_one(monkeypatch):
    ctx = _make_ctx(monkeypatch, max_per_user=1)
    entries = list(range(5))

    async def work(conn, entry):
        return conn.id

    results = await ctx.imap_parallel_map(entries, work)
    assert len(set(results)) == 1  # max(1, min(2, 1-2)) == max(1, -1) == 1
