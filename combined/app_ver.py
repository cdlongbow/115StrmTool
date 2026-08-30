import threading
from random import choice, randint
from time import monotonic
from typing import Dict, Optional

from logger import logger

_APP_VERSION_ATTR = "_app_version"
FALLBACK_ANDROID_VER = "37.2.5"
FALLBACK_UDOWN_VER = "37.0.7"
FALLBACK_WANGPAN_IOS_VER = "36.2.20"

_VERSIONS_TTL = 3600.0
_VERSIONS_FAIL_TTL = 600.0

_versions_cache: Optional[Dict[str, str]] = None
_versions_cached_at: float = 0.0
_versions_lock = threading.Lock()


def _fetch_app_versions() -> Optional[Dict[str, str]]:
    """
    获取 115 各端版本号，带进程内缓存

    成功结果缓存 1 小时；失败缓存 10 分钟，避免每次调用都请求 115 API

    :return Dict: 各端版本号字典（Android / iOS-iPhone / 115wangpan_iOS），
    缓存期内失败时返回 None
    """
    global _versions_cache, _versions_cached_at
    with _versions_lock:
        if _versions_cache is not None:
            if monotonic() - _versions_cached_at < _VERSIONS_TTL:
                return _versions_cache
        elif monotonic() - _versions_cached_at < _VERSIONS_FAIL_TTL:
            return None
        try:
            from p115client import P115Client, check_response

            resp = P115Client.app_version_list2()
            check_response(resp)
            data = resp["data"]
            versions = {
                "Android": str(data["Android"]["version_code"]),
                "iOS-iPhone": str(data["iOS-iPhone"]["version_code"]),
                "115wangpan_iOS": str(data["115wangpan_iOS"]["version_code"]),
            }
            _versions_cache = versions
            _versions_cached_at = monotonic()
            logger.info("获取 115 版本号成功: %s", versions)
            return versions
        except Exception:
            _versions_cache = None
            _versions_cached_at = monotonic()
            logger.warning("获取 115 版本号失败，暂用回退版本", exc_info=True)
            return None


def get_real_app_ver() -> str:
    versions = _fetch_app_versions()
    if versions and versions.get("Android"):
        return versions["Android"]
    return FALLBACK_ANDROID_VER


def generate_u115_ios() -> str:
    versions = _fetch_app_versions()
    udown_version = (
        versions.get("iOS-iPhone") if versions else None
    ) or FALLBACK_UDOWN_VER
    wangpan_version = (
        versions.get("115wangpan_iOS") if versions else None
    ) or FALLBACK_WANGPAN_IOS_VER
    ios_versions = [
        "15_0", "15_1", "15_2", "15_3", "15_4",
        "15_5", "15_6", "15_7", "15_8",
        "16_0", "16_1", "16_2", "16_3", "16_4",
        "16_5", "16_6", "16_7",
        "17_0", "17_1", "17_2", "17_3", "17_4", "17_5",
        "18_0", "18_1",
    ]
    build_num = randint(15, 21)
    build_letter = choice("ABCDE")
    build_tail = randint(100, 999)
    build = f"{build_num}{build_letter}{build_tail}"
    webkit = "605.1.15"
    os_ver = choice(ios_versions)
    client = choice([
        f"115wangpan_ios/{wangpan_version}",
        f"UDown/{udown_version}",
    ])
    return (
        f"Mozilla/5.0 (iPhone; CPU iPhone OS {os_ver} like Mac OS X) "
        f"AppleWebKit/{webkit} (KHTML, like Gecko) Mobile/{build} {client}"
    )


class AppVerPatcher:
    """
    app_ver 补丁
    """

    _active: bool = False

    @classmethod
    def enable(cls) -> None:
        if cls._active:
            return
        try:
            import p115client.client as _p115_client_mod

            if not hasattr(_p115_client_mod, _APP_VERSION_ATTR):
                logger.warning(
                    "p115client 版本不兼容，未找到 %s 属性", _APP_VERSION_ATTR
                )
                return

            real = get_real_app_ver()
            setattr(_p115_client_mod, _APP_VERSION_ATTR, real)
            cls._active = True
            logger.info("app_ver 补丁已应用: %s", real)
        except ImportError:
            logger.warning("p115client 未安装，跳过 app_ver 补丁")


def apply_app_ver_patch():
    """
    兼容旧接口，等同于 AppVerPatcher.enable()
    """
    AppVerPatcher.enable()
