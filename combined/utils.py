"""
通用工具模块：重试机制、异步 TTL 缓存
"""
import asyncio
from asyncio import Lock
from time import monotonic
from typing import Any, Callable, Dict, List, Optional, Tuple

from logger import logger


async def retry_with_backoff(
    fn: Callable,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff: float = 2.0,
    exc_types: tuple = (Exception,),
) -> Any:
    """
    带指数退避的异步重试

    :param fn: 异步可调用对象
    :param max_retries: 最大重试次数
    :param base_delay: 初始延迟（秒）
    :param max_delay: 最大延迟（秒）
    :param backoff: 退避倍数
    :param exc_types: 可捕获的异常类型元组

    :return: fn 的返回值

    :raises: 最后一次异常
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except exc_types as e:
            last_exc = e
            if attempt < max_retries:
                delay = min(base_delay * (backoff ** attempt), max_delay)
                logger.warning(
                    "重试 %s/%s (%s) 失败: %s，%.1fs 后重试",
                    attempt + 1, max_retries + 1,
                    getattr(fn, "__name__", "task"), e, delay,
                )
                await asyncio.sleep(delay)
    raise last_exc


class AsyncTtlCache:
    """
    异步安全的 TTL 缓存，支持 LRU 淘汰

    用法:
        cache = AsyncTtlCache[str, str](ttl=90, max_size=500)
        async with cache.lock:
            val = cache.get("key")
            if val is None:
                val = await fetch_data()
                cache.put("key", val)
    """

    def __init__(self, ttl: float = 90, max_size: int = 500):
        self._ttl = ttl
        self._max_size = max_size
        self._data: Dict[Any, Tuple[Any, float]] = {}
        self._order: List[Any] = []
        self._lock = Lock()

    @property
    def lock(self) -> Lock:
        return self._lock

    @property
    def ttl(self) -> float:
        return self._ttl

    @ttl.setter
    def ttl(self, value: float):
        self._ttl = value

    def get(self, key: Any) -> Optional[Any]:
        entry = self._data.get(key)
        if entry is None:
            return None
        val, expiry = entry
        if monotonic() < expiry:
            return val
        self._evict_key(key)
        return None

    def put(self, key: Any, value: Any):
        now = monotonic()
        expiry = now + self._ttl
        if key not in self._data:
            self._order.append(key)
        self._data[key] = (value, expiry)
        self._evict_expired(now)
        while len(self._data) > self._max_size and self._order:
            oldest = self._order.pop(0)
            self._data.pop(oldest, None)

    def remove(self, key: Any):
        self._data.pop(key, None)
        try:
            self._order.remove(key)
        except ValueError:
            pass

    def clear(self):
        self._data.clear()
        self._order.clear()

    def _evict_key(self, key: Any):
        self._data.pop(key, None)
        try:
            self._order.remove(key)
        except ValueError:
            pass

    def _evict_expired(self, now: float):
        expired = [k for k in self._order if k in self._data and self._data[k][1] < now]
        for k in expired:
            self._data.pop(k, None)
        self._order[:] = [k for k in self._order if k not in frozenset(expired)]