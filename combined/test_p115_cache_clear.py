"""
admin_api 115 目录节点缓存清理端点测试：覆盖正常清理、空缓存、部分失败与 SDK 缺符号
"""
import sys
from collections import defaultdict
from types import ModuleType
from unittest.mock import patch

import pytest

from admin_api import clear_p115_node_cache
from exceptions import ServiceError


class _BrokenClearCache(dict):
    """clear 时抛异常的缓存桶替身"""

    def clear(self):
        raise RuntimeError("cache busy")


class _BrokenLenCache(dict):
    """len 时抛异常的缓存桶替身"""

    def __len__(self):
        raise TypeError("no length")


def _patch_const(cache_map: dict):
    """
    构造 p115client.const 替身 patcher，使端点内的局部 import 命中替身

    :param cache_map (dict): 注入到 ID_TO_DIRNODE_CACHE 的缓存内容

    :return _patch_dict: 注入 sys.modules 的 patcher，供 with 使用
    """
    const_mod = ModuleType("p115client.const")
    const_mod.ID_TO_DIRNODE_CACHE = cache_map
    pkg = ModuleType("p115client")
    pkg.const = const_mod
    return patch.dict(
        sys.modules, {"p115client": pkg, "p115client.const": const_mod}
    )


class TestClearP115NodeCache:
    def test_clears_all_buckets_in_place(self):
        bucket_a = {100: ("电影", 0), 200: ("剧集", 100)}
        bucket_b = {300: ("动漫", 0)}
        cache_map = {"uid-a": bucket_a, "uid-b": bucket_b}
        with _patch_const(cache_map):
            result = clear_p115_node_cache()
        assert result["status"] == "ok"
        assert result["buckets"] == 2
        assert result["entries"] == 3
        assert bucket_a == {} and bucket_b == {}

    def test_empty_cache_reports_noop(self):
        with _patch_const(defaultdict(dict)):
            result = clear_p115_node_cache()
        assert result["status"] == "ok"
        assert result["buckets"] == 0
        assert result["entries"] == 0
        assert "已是空的" in result["message"]

    def test_broken_len_counted_as_zero_still_clears(self):
        broken = _BrokenLenCache({1: ("x", 0)})
        normal = {2: ("y", 0)}
        with _patch_const({"uid-a": broken, "uid-b": normal}):
            result = clear_p115_node_cache()
        assert result["entries"] == 1
        assert broken == {} and normal == {}

    def test_partial_failure_raises_after_clearing_rest(self):
        broken = _BrokenClearCache({1: ("x", 0)})
        normal = {2: ("y", 0)}
        with _patch_const({"uid-a": broken, "uid-b": normal}):
            with pytest.raises(ServiceError, match="清理不完整"):
                clear_p115_node_cache()
        assert normal == {}

    def test_missing_symbol_returns_400(self):
        const_mod = ModuleType("p115client.const")
        pkg = ModuleType("p115client")
        pkg.const = const_mod
        with patch.dict(
            sys.modules, {"p115client": pkg, "p115client.const": const_mod}
        ):
            with pytest.raises(ServiceError) as exc:
                clear_p115_node_cache()
        assert exc.value.status_code == 400
