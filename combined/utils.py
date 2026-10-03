"""
通用工具模块：异步 TTL 缓存、按 key 互斥锁、速率控制、异常捕获装饰器
"""
from asyncio import Lock, shield
from collections import OrderedDict
from contextlib import asynccontextmanager
from functools import wraps
from inspect import iscoroutinefunction
from threading import Lock as ThreadLock
from time import monotonic, sleep
from typing import Any, AsyncIterator, Callable, Dict, Optional, Tuple

from logger import logger


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
        self._data: "OrderedDict[Any, Tuple[Any, float]]" = OrderedDict()
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
            self._data.move_to_end(key)
            return val
        del self._data[key]
        return None

    def put(self, key: Any, value: Any, ttl: Optional[float] = None):
        now = monotonic()
        expiry = now + (ttl if ttl is not None else self._ttl)
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (value, expiry)
        self._evict_expired(now)
        while len(self._data) > self._max_size:
            self._data.popitem(last=False)

    def remove(self, key: Any):
        self._data.pop(key, None)

    def count_prefix(self, prefix: str) -> int:
        """
        统计以指定前缀开头的有效缓存条目数量，顺带清理过期项

        :param prefix (str): 键前缀

        :return int: 匹配的有效条目数量
        """
        now = monotonic()
        count = 0
        expired = []
        for k, (_, expiry) in self._data.items():
            if expiry < now:
                expired.append(k)
                continue
            if isinstance(k, str) and k.startswith(prefix):
                count += 1
        for k in expired:
            del self._data[k]
        return count

    def clear(self):
        self._data.clear()

    def _evict_expired(self, now: float):
        expired = [k for k, (_, expiry) in self._data.items() if expiry < now]
        for k in expired:
            del self._data[k]


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

            async def _cleanup() -> None:
                async with self._guard:
                    current_lock, users = self._locks[key]
                    if users == 1:
                        self._locks.pop(key)
                    else:
                        self._locks[key] = (current_lock, users - 1)

            # shield 保证调用方被取消时引用计数清理仍完整执行，避免锁泄漏
            await shield(_cleanup())


class RateLimiter:
    """
    速率控制器，按固定间隔精确控制同步调用的频率

    传入 qps 小于等于 0 时表示不限制。``acquire`` 会阻塞直到满足与
    上一次调用之间的最小间隔，可用于给 115 API 限速或实现端点冷却
    """

    def __init__(self, qps: float):
        """
        初始化速率控制器

        :param qps (float): 每秒允许的调用次数，小于等于 0 表示不限制
        """
        if qps <= 0:
            qps = float("inf")
        self.interval = 1.0 / qps
        self.lock = ThreadLock()
        self.next_call_time = monotonic()

    def acquire(self):
        """
        获取调用许可，阻塞直到满足速率限制
        """
        with self.lock:
            now = monotonic()
            sleep_duration = self.next_call_time - now
            self.next_call_time = max(now, self.next_call_time) + self.interval
        if sleep_duration > 0:
            sleep(sleep_duration)


def capture_exceptions(default: Any = None, log_message: str = "操作失败"):
    """
    异常捕获装饰器，统一捕获被装饰函数的异常并记录日志

    同时支持同步函数与协程函数：捕获异常后返回 ``default``，
    用于替换散落的 try/except 样板，保持原有「失败不抛出」的语义

    :param default (Any): 捕获异常时返回的默认值
    :param log_message (str): 日志前缀描述

    :return Callable: 装饰器
    """

    def decorator(func: Callable) -> Callable:
        if iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    logger.error("%s: %s", log_message, e, exc_info=True)
                    return default

            return async_wrapper

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.error("%s: %s", log_message, e, exc_info=True)
                return default

        return sync_wrapper

    return decorator
