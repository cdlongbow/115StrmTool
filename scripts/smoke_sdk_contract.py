"""
真实 115 SDK 契约冒烟：验证仓库锁包安装后的关键符号与调用签名
"""
import inspect
import sys
from importlib import import_module
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
COMBINED = ROOT / "combined"
if str(COMBINED) not in sys.path:
    sys.path.insert(0, str(COMBINED))


def _real_module(name: str) -> ModuleType:
    """
    导入并确认模块是真实安装对象

    :param name: 模块名
    :return ModuleType: 已导入模块
    """
    module = import_module(name)
    if isinstance(module, Mock):
        raise RuntimeError(f"模块被测试桩替换: {name}")
    return module


def main() -> int:
    """
    执行 SDK 契约检查

    :return int: 进程退出码
    """
    p115client = _real_module("p115client")
    _real_module("p115cipher")
    _real_module("p115pickcode")

    iter_files = _real_module("p115client.tool.iterdir").iter_files
    if "with_path" not in inspect.signature(iter_files).parameters:
        raise RuntimeError("iter_files 缺少 with_path 参数")

    const = _real_module("p115client.const")
    if not hasattr(const, "ID_TO_DIRNODE_CACHE") or not hasattr(const.ID_TO_DIRNODE_CACHE, "clear"):
        raise RuntimeError("ID_TO_DIRNODE_CACHE 契约不完整")

    fs_files = _real_module("p115client.tool.fs_files")
    if not callable(getattr(fs_files, "get_webapi_origin", None)):
        raise RuntimeError("get_webapi_origin 不可用")
    if not callable(getattr(p115client, "check_response", None)):
        raise RuntimeError("check_response 不可用")

    p115_url = _real_module("p115client.type").P115URL
    if not isinstance(p115_url, type):
        raise RuntimeError("P115URL 不是类型")

    from p115_client_wrapper import P115ClientWrapper

    wrapper = P115ClientWrapper("UID=1;CID=1;SEID=1")
    try:
        if not wrapper.is_ready():
            raise RuntimeError("P115ClientWrapper 未能初始化真实 SDK")
        for method_name in ("fs_files", "fs_dir_getid", "download_url"):
            if not callable(getattr(wrapper.client, method_name, None)):
                raise RuntimeError(f"SDK 客户端缺少 {method_name}")
    finally:
        wrapper.close()

    version = getattr(p115client, "__version__", "unknown")
    print(f"SDK contract OK: p115client={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
