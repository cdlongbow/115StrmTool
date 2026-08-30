"""
admin_api 配置更新测试：P115 Cookie 变更时触发客户端重建回调
"""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from admin_api import update_config, set_p115_restart_callback
from admin_api import ConfigUpdateRequest


class TestP115CookieRestart:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self._callback_called = 0

        def _cb():
            self._callback_called += 1

        set_p115_restart_callback(_cb)
        yield
        set_p115_restart_callback(None)

    @contextmanager
    def _patch_config_manager(self, current_cookie="old-cookie"):
        _cm = MagicMock()
        _cm.get.return_value = {"p115": {"cookie": current_cookie}}
        patcher = patch("admin_api.config_manager", _cm)
        patcher.start()
        try:
            self._cm = _cm
            yield
        finally:
            patcher.stop()

    def test_cookie_change_triggers_restart(self):
        with self._patch_config_manager():
            req = ConfigUpdateRequest(p115={"cookie": "new-cookie"})
            update_config(req)
        assert self._callback_called == 1
        self._cm.update.assert_called_once()

    def test_no_cookie_no_restart(self):
        with self._patch_config_manager():
            req = ConfigUpdateRequest(p115={"enabled": True})
            update_config(req)
        assert self._callback_called == 0

    def test_no_callback_registered(self):
        set_p115_restart_callback(None)
        with self._patch_config_manager():
            req = ConfigUpdateRequest(p115={"cookie": "new-cookie"})
            update_config(req)
        assert self._callback_called == 0
