"""
速率控制器、异常捕获装饰器与缓存前缀统计单元测试
"""
import asyncio
from time import monotonic

from utils import AsyncTtlCache, RateLimiter, capture_exceptions


def test_rate_limiter_enforces_interval():
    limiter = RateLimiter(qps=50)
    start = monotonic()
    for _ in range(4):
        limiter.acquire()
    elapsed = monotonic() - start
    # 4 次调用至少间隔 3 个周期（每周期 0.02s），留调度抖动余量
    assert elapsed >= 0.03, elapsed


def test_rate_limiter_zero_qps_never_blocks():
    limiter = RateLimiter(qps=0)
    start = monotonic()
    for _ in range(20):
        limiter.acquire()
    assert monotonic() - start < 0.05


def test_capture_exceptions_sync_returns_default():
    @capture_exceptions(default=-1, log_message="同步失败")
    def boom(x):
        raise ValueError(x)

    assert boom("bad") == -1


def test_capture_exceptions_preserves_result():
    @capture_exceptions(default=None)
    def ok():
        return 42

    assert ok() == 42


def test_capture_exceptions_async_returns_default():
    @capture_exceptions(default={"fallback": True}, log_message="异步失败")
    async def boom():
        raise RuntimeError("async bad")

    async def _run():
        return await boom()

    assert asyncio.run(_run()) == {"fallback": True}


def test_capture_exceptions_async_preserves_result():
    @capture_exceptions(default=None)
    async def ok():
        return 7

    async def _run():
        return await ok()

    assert asyncio.run(_run()) == 7


def test_count_prefix_counts_valid_only():
    cache = AsyncTtlCache(ttl=60)
    cache.put("AAA:one", 1)
    cache.put("AAA:two", 2)
    cache.put("BBB:one", 3)
    cache.put("AAA:expired", 4, ttl=-1)
    assert cache.count_prefix("AAA:") == 2
    assert "AAA:expired" not in cache._data, "过期条目应在统计时被顺带清理"


def test_count_prefix_empty():
    cache = AsyncTtlCache(ttl=60)
    assert cache.count_prefix("ZZZ:") == 0
