"""
应用元信息：对外暴露本程序版本号，供启动日志、托盘提示与管理面板展示

版本号在发布构建时由 CI 依据 tag 写成 ``_build_version`` 模块并随包分发，
本地开发未注入时回退为 ``dev``，因此源码树无需手工维护版本常量
"""
try:
    from _build_version import __version__ as _build_version
except ImportError:
    _build_version = "dev"

APP_VERSION: str = _build_version
