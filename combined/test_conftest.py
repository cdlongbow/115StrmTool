"""
测试共享桩模块工具测试：验证已导入与已安装模块不会被误替身
"""
import sys
from importlib.metadata import PackageNotFoundError
from unittest.mock import MagicMock

import conftest
from conftest import stub_missing


def test_stub_missing_keeps_loaded_module(monkeypatch):
    """已导入模块应保持原对象"""
    name = "_test_loaded_stub_module"
    module = MagicMock()
    monkeypatch.setitem(sys.modules, name, module)
    stub_missing((name,))
    assert sys.modules[name] is module


def test_stub_missing_keeps_installed_distribution(monkeypatch):
    """已安装但尚未导入的模块应保留真实导入机会"""
    name = "_test_installed_stub_module"
    monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(conftest, "distribution", lambda _name: object())
    stub_missing((name,))
    assert name not in sys.modules


def test_stub_missing_stubs_absent_distribution(monkeypatch):
    """未安装模块应注入空白替身"""
    name = "_test_absent_stub_module"
    monkeypatch.delitem(sys.modules, name, raising=False)

    def _missing(_name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(conftest, "distribution", _missing)
    stub_missing((name,))
    assert isinstance(sys.modules[name], MagicMock)
