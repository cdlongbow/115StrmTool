from threading import Lock
from time import monotonic, sleep
from typing import Callable


class ApiEndpointCooldown:
    """
    独立冷却时间和线程锁的 API 端点
    """

    def __init__(self, api_callable: Callable, cooldown: float | int):
        self.api_callable = api_callable
        self.cooldown = cooldown
        self.lock = Lock()
        self.last_call_time = monotonic() - cooldown

    def __call__(self, payload: dict) -> dict:
        if self.cooldown > 0:
            sleep_duration = 0
            with self.lock:
                now = monotonic()
                elapsed = now - self.last_call_time
                if elapsed < self.cooldown:
                    sleep_duration = self.cooldown - elapsed
            if sleep_duration > 0:
                sleep(sleep_duration)
            with self.lock:
                self.last_call_time = monotonic()
        return self.api_callable(payload)
