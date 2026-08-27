"""
通用工具模块：异步 TTL 缓存、按 key 互斥锁
"""
from asyncio import Lock
from contextlib import asynccontextmanager
from time import monotonic
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple


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

    def put(self, key: Any, value: Any, ttl: Optional[float] = None):
        now = monotonic()
        expiry = now + (ttl if ttl is not None else self._ttl)
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


class AsyncKeyLock:
    """
    按 key 隔离的异步互斥锁，无使用者时自动清理

    用于防止缓存击穿：同一 key 的并发请求只允许一个执行耗时的回源逻辑，
    其余请求在锁内二次检查缓存后直接复用结果

    用法:
        lock = AsyncKeyLock()
        async with lock.acquire("some-key"):
            ...
    """

    def __init__(self):
        self._locks: Dict[Any, Tuple[Lock, int]] = {}
        self._guard = Lock()

    @asynccontextmanager
    async def acquire(self, key: Any) -> AsyncIterator[None]:
        """
        获取指定 key 的互斥锁，并在无使用者时清理该锁

        :param key (Any): 锁的隔离键

        :yields None: 获取互斥锁后的执行上下文
        """
        async with self._guard:
            lock_info = self._locks.get(key)
            if lock_info:
                lock, users = lock_info
            else:
                lock, users = Lock(), 0
            self._locks[key] = (lock, users + 1)

        acquired = False
        try:
            await lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            async with self._guard:
                current_lock, users = self._locks[key]
                if users == 1:
                    self._locks.pop(key)
                else:
                    self._locks[key] = (current_lock, users - 1)
