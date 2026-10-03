"""
测试共享桩模块工具：集中定义 sys.modules 替身依赖，避免各测试文件重复定义后漂移
"""
import sys
from typing import Dict, Tuple
from unittest.mock import MagicMock

# 115 生态底层库：开发环境通常未安装，导入封装层前需要替身
P115_NATIVE_MODULES: Tuple[str, ...] = ("p115client", "p115cipher", "p115pickcode")

# API 路由层测试的重依赖组合
API_HEAVY_DEPS: Tuple[str, ...] = (
    "config_manager",
    "database",
    "logger",
    "p115_client_wrapper",
)

# database / strm_generator 模块导入所需的重依赖组合（需要连带替身 database 时在调用处追加）
DB_HEAVY_DEPS: Tuple[str, ...] = (
    "app_ver",
    "config_manager",
    "httpx",
    "logger",
    "p115cipher",
    "p115client",
    "p115_client_wrapper",
)

# redirect_service 导入所需的重依赖组合
REDIRECT_HEAVY_DEPS: Tuple[str, ...] = ("p115_client_wrapper",)


def stub_modules(names: Tuple[str, ...]) -> Dict[str, MagicMock]:
    """
    生成用于 patch.dict 的空白替身模块字典，每次调用返回全新的 MagicMock 实例

    :param names (Tuple): 需要替身的模块名列表

    :return Dict: 模块名到 MagicMock 替身的映射
    """
    return {name: MagicMock() for name in names}


def stub_missing(names: Tuple[str, ...]) -> None:
    """
    仅为当前环境未安装的模块注入空白替身，已安装的真实模块保持不变

    :param names (Tuple): 需要按需替身的模块名列表

    :return None:
    """
    for name in names:
        sys.modules.setdefault(name, MagicMock())
