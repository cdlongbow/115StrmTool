"""
目录选择接口测试：验证正常返回、卡死超时回退与 tkinter 异常回退
"""
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

_HEAVY_DEPS = {
    "config_manager": MagicMock(),
    "database": MagicMock(),
    "logger": MagicMock(),
    "p115_client_wrapper": MagicMock(),
}


@pytest.fixture
def api_routes():
    # patch.dict 退出时自动还原 sys.modules，避免污染其他测试
    with patch.dict(sys.modules, _HEAVY_DEPS):
        import api_routes as mod
        yield mod


def _fake_tkinter(askdirectory=None, root=None):
    root = root if root is not None else MagicMock()
    filedialog = MagicMock()
    if askdirectory is not None:
        filedialog.askdirectory = askdirectory
    tk = MagicMock()
    tk.Tk.return_value = root
    tk.filedialog = filedialog
    return tk, root


class TestSelectDirectory:

    def test_returns_selected_path(self, api_routes):
        root = MagicMock()
        tk, _ = _fake_tkinter(
            askdirectory=MagicMock(return_value="D:/movies/科幻"), root=root
        )
        with patch.dict(
            sys.modules, {"tkinter": tk, "tkinter.filedialog": tk.filedialog}
        ):
            result = api_routes._select_directory_sync()
        assert result == "D:/movies/科幻"
        root.destroy.assert_called_once()

    def test_timeout_returns_empty(self, api_routes):
        def _hang(*a, **k):
            time.sleep(2)
            return "不应返回"

        tk, _ = _fake_tkinter(askdirectory=_hang)
        with patch.object(api_routes, "SELECT_DIRECTORY_TIMEOUT", 0.2):
            with patch.dict(
                sys.modules, {"tkinter": tk, "tkinter.filedialog": tk.filedialog}
            ):
                result = api_routes._select_directory_sync()
        assert result == "", "卡死时应超时返回空串，触发前端手动输入兜底"

    def test_tkinter_failure_returns_empty(self, api_routes):
        broken_tk = MagicMock()
        broken_tk.Tk.side_effect = RuntimeError("Tk init failed")
        with patch.dict(
            sys.modules,
            {"tkinter": broken_tk, "tkinter.filedialog": broken_tk.filedialog},
        ):
            result = api_routes._select_directory_sync()
        assert result == "", "tkinter 异常时应返回空串，触发前端手动输入兜底"

    def test_cancel_returns_empty(self, api_routes):
        tk, _ = _fake_tkinter(askdirectory=MagicMock(return_value=""))
        with patch.dict(
            sys.modules, {"tkinter": tk, "tkinter.filedialog": tk.filedialog}
        ):
            result = api_routes._select_directory_sync()
        assert result == "", "用户取消选择时应返回空串"
