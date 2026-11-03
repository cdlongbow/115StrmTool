"""
目录选择接口测试：验证正常返回、卡死超时回退与 tkinter 异常回退
"""
import sys
import threading
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from conftest import API_HEAVY_DEPS, stub_modules

_api_routes_cache: List = []


@pytest.fixture
def api_routes():
    # CPython 3.14 下每个用例反复重装 api_routes/fastapi 导入链会在 GC 阶段段
    # 错误，首次带桩导入后缓存模块对象复用；tkinter 等仍由各用例运行时查
    # sys.modules，桩隔离语义不变
    if not _api_routes_cache:
        with patch.dict(sys.modules, stub_modules(API_HEAVY_DEPS)):
            import api_routes as mod
        _api_routes_cache.append(mod)
    yield _api_routes_cache[0]


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
        unblock = threading.Event()

        def _hang(*a, **k):
            unblock.wait(5)
            return "不应返回"

        tk, _ = _fake_tkinter(askdirectory=_hang)
        with patch.object(api_routes, "SELECT_DIRECTORY_TIMEOUT", 0.2):
            with patch.dict(
                sys.modules, {"tkinter": tk, "tkinter.filedialog": tk.filedialog}
            ):
                result = api_routes._select_directory_sync()
                assert result == "", "卡死时应超时返回空串，触发前端手动输入兜底"
                # 超时路径会遗留仍在运行的 daemon worker，须在桩模块还原前收口，
                # 否则解释器 finalize 时撞上仍持有桩的线程（CPython 3.14 下会段错误）
                unblock.set()
                for t in threading.enumerate():
                    if t.name == "select-directory":
                        t.join(5)

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
