"""
危险操作确认门：面板二次确认后先换取短时一次性令牌，破坏性接口核验并
消费令牌；令牌过期或重放都要求重新确认，防止误触与陈旧确认窗口执行
"""
import secrets
import threading
from time import monotonic
from typing import Dict, Tuple

CONFIRM_TTL_SECONDS = 90.0

DANGEROUS_OPS = frozenset({
    "sync.reset-baseline",
    "sync.history-clear",
    "logs.clear",
})


class ConfirmGate:
    """
    一次性确认令牌的签发与消费，进程内存储
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: Dict[str, Tuple[str, float]] = {}

    def issue(self, op: str) -> str:
        """
        为指定危险操作签发一次性确认令牌，并顺带清理已过期条目

        :param op (str): 危险操作标识，须属于 DANGEROUS_OPS

        :return str: 令牌字符串
        """
        token = secrets.token_urlsafe(16)
        expire_at = monotonic() + CONFIRM_TTL_SECONDS
        with self._lock:
            now = monotonic()
            expired = [key for key, (_, e) in self._pending.items() if e < now]
            for key in expired:
                self._pending.pop(key, None)
            self._pending[token] = (op, expire_at)
        return token

    def consume(self, op: str, token: str) -> bool:
        """
        校验并消费令牌：操作匹配、未过期且首次使用才放行

        :param op (str): 危险操作标识

        :param token (str): 前端回传的令牌

        :return bool: 校验通过返回 True
        """
        if not token:
            return False
        with self._lock:
            item = self._pending.pop(token, None)
        return item is not None and item[0] == op and item[1] > monotonic()


confirm_gate = ConfirmGate()
