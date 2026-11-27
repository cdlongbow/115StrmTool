import copy
import threading
from types import SimpleNamespace

import pytest

from conftest import P115_NATIVE_MODULES, stub_missing

stub_missing(P115_NATIVE_MODULES)

import admin_api
import api_routes
import main
from exceptions import ClientNotReadyError, ServiceError


class _FakeClient:
    def __init__(self, ready=True):
        self.ready = ready
        self._client = object() if ready else None
        self.closed = False

    def is_ready(self):
        return self._client is not None

    def close(self):
        self.closed = True
        self._http_client = None


class _FakeGenerator:
    def __init__(self, syncing=False):
        self.syncing = syncing
        self.cancel_calls = 0
        self.sync_calls = 0
        self.progress = {"phase": "scanning", "current": 1, "total": 2, "message": ""}

    def is_syncing(self):
        return self.syncing

    def get_progress(self):
        return dict(self.progress)

    def cancel(self):
        self.cancel_calls += 1
        self._set_progress("cancelling", message="正在取消同步...")

    def _set_progress(self, phase, current=0, total=0, message=""):
        self.progress.update(
            {"phase": phase, "current": current, "total": total, "message": message}
        )

    def set_config(self, **kwargs):
        return None

    def full_sync(self, mappings):
        self.sync_calls += 1
        return {"total": 0}


class _FakeConfigManager:
    def __init__(self, data=None, save_ok=True):
        self.data = copy.deepcopy(data or {})
        self.save_ok = save_ok
        self.update_calls = []

    def get(self):
        return copy.deepcopy(self.data)

    def update(self, updates):
        self.update_calls.append(copy.deepcopy(updates))
        if not self.save_ok:
            return False
        self._merge(self.data, updates)
        return True

    @classmethod
    def _merge(cls, target, updates):
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                cls._merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)


def test_p115_teardown_clears_refs_and_invalidates_wrapper(monkeypatch):
    wrapper = _FakeClient()
    refs = []
    scheduler = SimpleNamespace(stop=lambda: refs.append("stop"))
    monkeypatch.setattr(main, "P115_REDIRECT_SERVER", None)
    monkeypatch.setattr(main, "P115_REDIRECT_THREAD", None)
    monkeypatch.setattr(main, "P115_CLIENT_WRAPPER", wrapper)
    monkeypatch.setattr(main, "set_p115_client_ref", lambda client: refs.append(client))
    monkeypatch.setattr(api_routes, "set_client", lambda client: refs.append(client))
    monkeypatch.setattr(main, "set_p115_status", lambda running: None)
    import checkin_scheduler

    monkeypatch.setattr(checkin_scheduler, "checkin_scheduler", scheduler)

    assert main._stop_p115_redirect() is True
    assert wrapper.closed is True
    assert wrapper.is_ready() is False
    assert refs == ["stop", None, None]
    assert main.P115_CLIENT_WRAPPER is None


def test_disabled_p115_rejects_stale_client(monkeypatch):
    monkeypatch.setattr(api_routes, "_client", _FakeClient())
    monkeypatch.setattr(
        api_routes.config_manager,
        "get",
        lambda: {"p115": {"enabled": False, "cookie": "old-cookie"}},
    )

    with pytest.raises(ClientNotReadyError):
        api_routes.get_client()


def test_get_client_rejects_cookie_mismatch(monkeypatch):
    client = _FakeClient()
    client._cookie = "old-cookie"
    monkeypatch.setattr(api_routes, "_client", client)
    monkeypatch.setattr(api_routes, "_client_cookie", "old-cookie")
    monkeypatch.setattr(
        api_routes.config_manager,
        "get",
        lambda: {"p115": {"enabled": True, "cookie": "new-cookie"}},
    )

    with pytest.raises(ClientNotReadyError, match="不一致"):
        api_routes.get_client()


def test_p115_restart_failure_invalidates_client_and_preserves_handles(monkeypatch):
    wrapper = _FakeClient()
    wrapper._cookie = "old-cookie"
    server = SimpleNamespace(should_exit=False)

    class _Thread:
        def is_alive(self):
            return True

    thread = _Thread()
    refs = []
    monkeypatch.setattr(main, "P115_REDIRECT_SERVER", server)
    monkeypatch.setattr(main, "P115_REDIRECT_THREAD", thread)
    monkeypatch.setattr(main, "P115_CLIENT_WRAPPER", wrapper)
    monkeypatch.setattr(main, "_stop_server", lambda *args: False)
    monkeypatch.setattr(main, "set_p115_client_ref", lambda client: refs.append(client))
    monkeypatch.setattr(api_routes, "set_client", lambda client: refs.append(client))
    import checkin_scheduler

    monkeypatch.setattr(
        checkin_scheduler,
        "checkin_scheduler",
        SimpleNamespace(stop=lambda: refs.append("stop")),
    )

    assert main._restart_p115() is False
    assert main.P115_REDIRECT_SERVER is server
    assert main.P115_REDIRECT_THREAD is thread
    assert wrapper.is_ready() is False
    assert refs == ["stop", None, None]


def test_checkin_start_recovers_after_stop_timeout(monkeypatch):
    import checkin_scheduler

    monkeypatch.setattr(checkin_scheduler, "CHECKIN_STOP_TIMEOUT", 0.01)
    scheduler = checkin_scheduler.CheckinScheduler()
    entered = threading.Event()
    release = threading.Event()

    def blocking_tick():
        entered.set()
        release.wait(timeout=5)

    monkeypatch.setattr(scheduler, "_tick", blocking_tick)
    scheduler.set_client(_FakeClient())
    assert scheduler.start() is True
    assert entered.wait(timeout=1)
    old_thread = scheduler._thread

    assert scheduler.stop() is False
    assert old_thread.is_alive()
    scheduler.set_client(_FakeClient())
    assert scheduler.start() is True
    new_thread = scheduler._thread
    assert new_thread is not old_thread
    assert new_thread.is_alive()

    release.set()
    old_thread.join(timeout=1)
    assert old_thread.is_alive() is False
    assert scheduler.stop() is True


def test_progress_and_cancel_do_not_refresh_generator(monkeypatch):
    generator = _FakeGenerator(syncing=True)
    monkeypatch.setattr(api_routes, "_sync_generator", generator)
    monkeypatch.setattr(api_routes, "_sync_pending", False)

    import strm_generator

    def _unexpected_refresh(*args, **kwargs):
        raise AssertionError("进度或取消接口不应刷新生成器依赖")

    monkeypatch.setattr(strm_generator, "get_strm_generator", _unexpected_refresh)

    assert api_routes.get_sync_progress()["phase"] == "scanning"
    assert api_routes.cancel_sync() == {"status": "cancelled"}
    assert generator.cancel_calls == 1


def test_pending_cancel_prevents_sync_start(monkeypatch):
    client = _FakeClient()
    config = _FakeConfigManager(
        {"p115": {"enabled": True, "cookie": "c", "strm_url_prefix": "http://127.0.0.1:3333", "paths": []}}
    )
    generator = _FakeGenerator()
    monkeypatch.setattr(api_routes, "_client", client)
    monkeypatch.setattr(api_routes.config_manager, "get", config.get)
    monkeypatch.setattr(api_routes, "_sync_generator", None)
    monkeypatch.setattr(api_routes, "_sync_pending", False)
    monkeypatch.setattr(api_routes, "_sync_generation", 0)
    monkeypatch.setattr(api_routes, "_sync_cancel_requested", False)

    import strm_generator

    monkeypatch.setattr(strm_generator, "get_strm_generator", lambda *args: generator)
    threads = []

    class _DeferredThread:
        def __init__(self, target, daemon=None, name=None):
            self.target = target
            threads.append(self)

        def start(self):
            return None

    monkeypatch.setattr(api_routes.threading, "Thread", _DeferredThread)

    assert api_routes._launch_sync("full")["status"] == "started"
    assert api_routes._sync_pending is True
    assert api_routes.cancel_sync() == {"status": "cancelled"}
    threads[0].target()

    assert generator.sync_calls == 0
    assert api_routes._sync_pending is False
    assert generator.get_progress()["phase"] == "cancelled"


def test_history_cleanup_holds_sync_lock(monkeypatch):
    monkeypatch.setattr(api_routes, "_sync_generator", None)
    monkeypatch.setattr(api_routes, "_sync_pending", False)
    lock_states = []

    def clear_history():
        lock_states.append(api_routes._sync_lock._is_owned())

    monkeypatch.setattr(api_routes.db, "clear_sync_history", clear_history)
    monkeypatch.setattr(api_routes.db, "clear_all_files", lambda: None)

    assert api_routes.clear_sync_history() == {"success": True}
    assert api_routes.reset_sync_baseline() == {"success": True}
    assert lock_states == [True, True]


def test_reset_and_clear_history_reject_active_sync(monkeypatch):
    generator = _FakeGenerator(syncing=True)
    monkeypatch.setattr(api_routes, "_sync_generator", generator)
    monkeypatch.setattr(api_routes, "_sync_pending", False)
    monkeypatch.setattr(api_routes.db, "clear_all_files", lambda: pytest.fail("不应清空文件基线"))
    monkeypatch.setattr(api_routes.db, "clear_sync_history", lambda: pytest.fail("不应清空历史"))

    with pytest.raises(ServiceError) as reset_error:
        api_routes.reset_sync_baseline()
    with pytest.raises(ServiceError) as clear_error:
        api_routes.clear_sync_history()

    assert reset_error.value.status_code == 409
    assert clear_error.value.status_code == 409


def test_qrcode_success_requires_persisted_cookie(monkeypatch):
    class _QrcodeClient:
        def check_qrcode(self, payload):
            return {"status": "success", "cookie": "new-cookie"}

    monkeypatch.setattr(api_routes, "get_client", lambda: _QrcodeClient())
    monkeypatch.setattr(
        api_routes.config_manager,
        "get",
        lambda: {"p115": {"cookie": "old-cookie"}},
    )

    result = api_routes.check_qrcode({})

    assert result["status"] == "error"
    assert "保存失败" in result["message"]


def test_config_save_failure_blocks_restart(monkeypatch):
    config = _FakeConfigManager(
        {"p115": {"enabled": False, "cookie": "old"}, "emby": {}},
        save_ok=False,
    )
    monkeypatch.setattr(admin_api, "config_manager", config)
    restarts = []
    monkeypatch.setattr(admin_api, "_restart_p115_callback", lambda: restarts.append("p115"))
    monkeypatch.setattr(admin_api, "_restart_emby_callback", lambda: restarts.append("emby"))

    with pytest.raises(ServiceError, match="保存失败"):
        admin_api.update_config(
            admin_api.ConfigUpdateRequest(
                p115={"enabled": True, "redirect_host": "127.0.0.1"},
                emby={"external_player_url": True},
            )
        )

    assert restarts == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("enabled", True),
        ("redirect_host", "127.0.0.1"),
        ("redirect_port", 4444),
    ],
)
def test_p115_runtime_fields_trigger_one_restart(monkeypatch, field, value):
    config = _FakeConfigManager(
        {"p115": {"enabled": False, "cookie": "c", "redirect_host": "0.0.0.0", "redirect_port": 3333}}
    )
    monkeypatch.setattr(admin_api, "config_manager", config)
    restarts = []
    monkeypatch.setattr(admin_api, "_restart_p115_callback", lambda: restarts.append("p115"))

    admin_api.update_config(admin_api.ConfigUpdateRequest(p115={field: value}))

    assert restarts == ["p115"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("external_player_url", True),
        ("external_player_list", ["potplayer"]),
        ("emby_host", "http://emby.new:8096"),
        ("proxy_port", 9000),
    ],
)
def test_emby_runtime_fields_trigger_one_restart(monkeypatch, field, value):
    config = _FakeConfigManager(
        {"emby": {"external_player_url": False, "external_player_list": [], "proxy_port": 8097}}
    )
    monkeypatch.setattr(admin_api, "config_manager", config)
    restarts = []
    monkeypatch.setattr(admin_api, "_restart_emby_callback", lambda: restarts.append("emby"))

    admin_api.update_emby_config(admin_api.EmbyConfigRequest(**{field: value}))

    assert restarts == ["emby"]


def test_emby_restart_reports_callback_failure(monkeypatch):
    monkeypatch.setattr(admin_api, "_restart_emby_callback", lambda: False)

    result = admin_api.restart_emby()

    assert result["status"] == "error"


def test_stop_timeout_preserves_service_handles(monkeypatch):
    class _Thread:
        def is_alive(self):
            return True

        def join(self, timeout=None):
            return None

    server = SimpleNamespace(should_exit=False)
    thread = _Thread()
    monkeypatch.setattr(main, "EMBY_SERVER", server)
    monkeypatch.setattr(main, "EMBY_THREAD", thread)
    monkeypatch.setattr(main, "_SERVER_STOP_TIMEOUT", 0.01)
    monkeypatch.setattr(main, "set_emby_status", lambda running: None)

    assert main._stop_emby() is False
    assert main.EMBY_SERVER is server
    assert main.EMBY_THREAD is thread


def test_wait_for_started_returns_false_when_thread_exits():
    class _Thread:
        def is_alive(self):
            return False

    assert main._wait_for_server_started(SimpleNamespace(started=False), _Thread(), timeout=0.01) is False
