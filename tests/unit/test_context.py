import pytest

from jmap_bridge.context import _RequestCache


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
