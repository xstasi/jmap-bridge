import asyncio

import pytest

from jmap_bridge.backends.imap.client import ImapConnection, ImapError
from jmap_bridge.pool import ImapConnectionPool


class FakeConnection:
    _next_id = 0

    def __init__(self):
        FakeConnection._next_id += 1
        self.id = FakeConnection._next_id
        self.logged_out = False

    async def logout(self):
        self.logged_out = True


@pytest.fixture(autouse=True)
def reset_fake_connection_counter():
    FakeConnection._next_id = 0
    yield


async def _fake_connect_and_login(host, port, tls, username, password):
    return FakeConnection()


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setattr(ImapConnection, "connect_and_login", staticmethod(_fake_connect_and_login))
    p = ImapConnectionPool()
    yield p


async def test_checkout_creates_and_returns_connection():
    calls = []

    async def fake_connect(host, port, tls, username, password):
        calls.append((host, port, tls, username, password))
        return FakeConnection()

    p = ImapConnectionPool()
    import jmap_bridge.pool as pool_mod

    monkey_target = ImapConnection.connect_and_login
    ImapConnection.connect_and_login = staticmethod(fake_connect)
    try:
        async with p.checkout(
            domain="example.com", username="alice@example.com", password="pw",
            host="imap.example.com", port=993, tls="implicit",
        ) as conn:
            assert isinstance(conn, FakeConnection)
        assert calls == [("imap.example.com", 993, "implicit", "alice@example.com", "pw")]
    finally:
        ImapConnection.connect_and_login = monkey_target


async def test_connection_reused_after_release(pool):
    async with pool.checkout(
        domain="example.com", username="alice@example.com", password="pw",
        host="h", port=993, tls="implicit",
    ) as conn1:
        first_id = conn1.id

    async with pool.checkout(
        domain="example.com", username="alice@example.com", password="pw",
        host="h", port=993, tls="implicit",
    ) as conn2:
        assert conn2.id == first_id  # same underlying connection reused


async def test_password_rotation_evicts_stale_connection(pool):
    async with pool.checkout(
        domain="example.com", username="alice@example.com", password="old-pw",
        host="h", port=993, tls="implicit",
    ) as conn1:
        old_conn = conn1

    async with pool.checkout(
        domain="example.com", username="alice@example.com", password="new-pw",
        host="h", port=993, tls="implicit",
    ) as conn2:
        assert conn2.id != old_conn.id  # stale entry not reused

    await asyncio.sleep(0)  # let the fire-and-forget logout task run
    assert old_conn.logged_out is True


async def test_exception_inside_checkout_is_not_returned_to_pool(pool):
    with pytest.raises(ImapError):
        async with pool.checkout(
            domain="example.com", username="alice@example.com", password="pw",
            host="h", port=993, tls="implicit",
        ) as conn1:
            first_id = conn1.id
            raise ImapError("simulated protocol failure")

    async with pool.checkout(
        domain="example.com", username="alice@example.com", password="pw",
        host="h", port=993, tls="implicit",
    ) as conn2:
        assert conn2.id != first_id  # a fresh connection was opened


async def test_max_per_user_limits_concurrent_connections(pool):
    entered = asyncio.Event()
    release = asyncio.Event()
    seen_ids = []

    async def hold_one():
        async with pool.checkout(
            domain="example.com", username="alice@example.com", password="pw",
            host="h", port=993, tls="implicit", max_per_user=1,
        ) as conn:
            seen_ids.append(conn.id)
            entered.set()
            await release.wait()

    async def try_second():
        await entered.wait()
        async with pool.checkout(
            domain="example.com", username="alice@example.com", password="pw",
            host="h", port=993, tls="implicit", max_per_user=1,
        ) as conn:
            seen_ids.append(conn.id)

    task1 = asyncio.create_task(hold_one())
    task2 = asyncio.create_task(try_second())
    await entered.wait()
    await asyncio.sleep(0.05)
    assert len(seen_ids) == 1  # second checkout is still blocked waiting
    release.set()
    await asyncio.wait_for(asyncio.gather(task1, task2), timeout=1)
    assert seen_ids == [seen_ids[0], seen_ids[0]]  # second reused the released connection


async def test_isolated_by_domain_and_username(pool):
    async with pool.checkout(
        domain="a.com", username="alice@a.com", password="pw", host="h", port=993, tls="implicit"
    ) as conn_a:
        id_a = conn_a.id
    async with pool.checkout(
        domain="b.com", username="alice@b.com", password="pw", host="h", port=993, tls="implicit"
    ) as conn_b:
        assert conn_b.id != id_a


async def test_sweep_evicts_idle_past_threshold(pool):
    async with pool.checkout(
        domain="example.com", username="alice@example.com", password="pw",
        host="h", port=993, tls="implicit", idle_eviction_seconds=0,
    ) as conn:
        first_id = conn.id

    await asyncio.sleep(0.01)
    await pool._sweep_once()

    async with pool.checkout(
        domain="example.com", username="alice@example.com", password="pw",
        host="h", port=993, tls="implicit", idle_eviction_seconds=0,
    ) as conn2:
        assert conn2.id != first_id
