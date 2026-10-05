"""
危险操作确认门测试：令牌签发消费、过期失效、端点门禁
"""
from unittest.mock import patch

import pytest

from confirm_gate import ConfirmGate, confirm_gate


class TestConfirmGate:
    def test_issue_and_consume_once(self):
        gate = ConfirmGate()
        token = gate.issue("logs.clear")
        assert gate.consume("logs.clear", token) is True
        assert gate.consume("logs.clear", token) is False

    def test_wrong_op_rejects(self):
        gate = ConfirmGate()
        token = gate.issue("logs.clear")
        assert gate.consume("sync.reset-baseline", token) is False

    def test_expired_rejects(self, monkeypatch):
        import confirm_gate as cg
        monkeypatch.setattr(cg, "CONFIRM_TTL_SECONDS", -1.0)
        gate = ConfirmGate()
        token = gate.issue("logs.clear")
        assert gate.consume("logs.clear", token) is False

    def test_empty_token_rejects(self):
        assert confirm_gate.consume("logs.clear", "") is False


class TestEndpointGates:
    def _api_routes(self):
        from conftest import API_HEAVY_DEPS, stub_modules
        with patch.dict("sys.modules", stub_modules(API_HEAVY_DEPS)):
            import api_routes as mod
        return mod

    def test_sync_gates_reject_missing_token(self):
        from exceptions import ServiceError
        mod = self._api_routes()
        with pytest.raises(ServiceError):
            mod.clear_sync_history()
        with pytest.raises(ServiceError):
            mod.reset_sync_baseline()

    def test_reset_baseline_accepts_valid_token(self):
        mod = self._api_routes()
        token = confirm_gate.issue("sync.reset-baseline")
        assert mod.reset_sync_baseline(x_confirm_token=token) == {"success": True}

    def test_issue_unknown_op_rejects(self):
        from admin_api import ConfirmOpsRequest, request_confirm
        from exceptions import ServiceError
        with pytest.raises(ServiceError):
            request_confirm(ConfirmOpsRequest(op="bogus"))

    def test_admin_logs_gate_rejects_missing_token(self):
        from admin_api import clear_logs
        from exceptions import ServiceError
        with pytest.raises(ServiceError):
            clear_logs()
