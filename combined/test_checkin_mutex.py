"""
签到互斥测试：定时与手动触发共用同一把锁，重复触发直接跳过
"""
import threading

import pytest

import checkin_scheduler
from checkin_scheduler import CheckinScheduler, _CHECKIN_LOCK


class _FakeConfig:
    """config_manager 替身"""

    def __init__(self, data):
        self._data = data

    def get(self):
        return self._data


class _StubClient:
    """记录签到调用次数的客户端替身"""

    def __init__(self):
        self.calls = 0

    def user_points_sign(self):
        self.calls += 1
        return {"data": {"is_sign_today": 1}}


class _BlockingClient(_StubClient):
    """首次调用阻塞在事件上，用于制造并发窗口"""

    def __init__(self, entered: threading.Event, release: threading.Event):
        super().__init__()
        self._entered = entered
        self._release = release

    def user_points_sign(self):
        self._entered.set()
        self._release.wait(timeout=5)
        return super().user_points_sign()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """隔离状态文件并确保每个用例开始时互斥锁可用"""
    monkeypatch.setattr(
        checkin_scheduler, "CHECKIN_STATE_FILE", tmp_path / "checkin_state.json"
    )
    if _CHECKIN_LOCK.locked():
        _CHECKIN_LOCK.release()
    yield
    if _CHECKIN_LOCK.locked():
        _CHECKIN_LOCK.release()


def test_manual_checkin_skips_when_lock_held():
    client = _StubClient()
    scheduler = CheckinScheduler()
    scheduler.set_client(client)

    assert _CHECKIN_LOCK.acquire(blocking=False) is True
    try:
        ok, detail = scheduler.manual_checkin()
    finally:
        _CHECKIN_LOCK.release()

    assert ok is False
    assert "正在执行" in detail
    assert client.calls == 0, "抢不到锁时不应调用签到接口"


def test_manual_checkin_releases_lock_after_success():
    client = _StubClient()
    scheduler = CheckinScheduler()
    scheduler.set_client(client)

    ok, detail = scheduler.manual_checkin()

    assert ok is True
    assert client.calls == 1
    assert not _CHECKIN_LOCK.locked(), "签到结束后必须释放互斥锁"
    assert scheduler.manual_checkin()[0] is True
    assert client.calls == 2, "锁释放后应允许再次签到"


def test_manual_checkin_requires_client():
    scheduler = CheckinScheduler()
    ok, detail = scheduler.manual_checkin()
    assert ok is False
    assert detail == "客户端未初始化"


def test_concurrent_manual_triggers_only_one_runs():
    entered = threading.Event()
    release = threading.Event()
    client = _BlockingClient(entered, release)
    scheduler = CheckinScheduler()
    scheduler.set_client(client)

    results = []

    def _first():
        results.append(scheduler.manual_checkin())

    first = threading.Thread(target=_first)
    first.start()
    assert entered.wait(timeout=5), "首次签到应已进入客户端调用"

    second_result = scheduler.manual_checkin()
    release.set()
    first.join(timeout=5)

    assert second_result[0] is False
    assert "正在执行" in second_result[1]
    assert results and results[0][0] is True
    assert client.calls == 1, "并发触发只应有一次真正签到"


def test_tick_skips_when_lock_held(monkeypatch):
    """自动触发抢不到锁时本轮跳过，不改写状态，下一轮会重新尝试"""
    client = _StubClient()
    scheduler = CheckinScheduler()
    scheduler.set_client(client)
    monkeypatch.setattr(
        "config_manager.config_manager",
        _FakeConfig({"checkin": {"enabled": True, "time_range": "00:00-23:59"}}),
    )
    scheduler._state = {"last_done_date": "", "next_run_ts": 0}

    assert _CHECKIN_LOCK.acquire(blocking=False) is True
    try:
        scheduler._tick()
    finally:
        _CHECKIN_LOCK.release()

    assert client.calls == 0
    assert scheduler._state.get("next_run_ts") == 0, "跳过后不应推进下次执行时间"
