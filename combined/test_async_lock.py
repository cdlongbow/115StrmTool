"""
按 key 异步互斥锁与 TTL 缓存单元测试
"""
import asyncio

from utils import AsyncKeyLock, AsyncTtlCache


def test_key_lock_serializes_same_key():
    async def _run():
        lock = AsyncKeyLock()
        in_flight = 0
        peak = 0
        order = []

        async def worker(i):
            nonlocal in_flight, peak
            async with lock.acquire("k"):
                in_flight += 1
                peak = max(peak, in_flight)
                order.append(i)
                await asyncio.sleep(0.01)
                in_flight -= 1

        await asyncio.gather(*(worker(i) for i in range(5)))
        return peak, order

    peak, order = asyncio.run(_run())
    assert peak == 1, "同一 key 的并发段应串行执行"
    assert order == [0, 1, 2, 3, 4]


def test_key_lock_independent_keys_parallel():
    async def _run():
        lock = AsyncKeyLock()
        in_flight = 0
        peak = 0

        async def worker(k):
            nonlocal in_flight, peak
            async with lock.acquire(k):
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0.05)
                in_flight -= 1

        await asyncio.gather(*(worker(f"k{i}") for i in range(5)))
        return peak

    peak = asyncio.run(_run())
    assert peak == 5, "不同 key 的并发段应并行执行"


def test_key_lock_cleanup_empty():
    async def _run():
        lock = AsyncKeyLock()
        async with lock.acquire("a"):
            pass
        async with lock.acquire("b"):
            pass
        return len(lock._locks)

    assert asyncio.run(_run()) == 0, "无使用者后锁应被清理"


def test_cache_put_per_entry_ttl():
    cache = AsyncTtlCache(ttl=90)

    async def _run():
        async with cache.lock:
            cache.put("short", 1, ttl=0.1)
            cache.put("long", 2, ttl=5)
        await asyncio.sleep(0.15)
        async with cache.lock:
            assert cache.get("short") is None, "短 ttl 条目应已过期"
            assert cache.get("long") == 2, "长 ttl 条目应仍有效"
            assert cache.get("missing") is None

    asyncio.run(_run())
