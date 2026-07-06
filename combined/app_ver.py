import threading
from random import choice, randint
from typing import Optional

from p115client import P115Client, check_response
import p115client.client as _p115_client_mod

from logger import logger

_APP_VERSION_ATTR = "_app_version"
FALLBACK_APP_VER = "37.2.5"

_real_app_ver: Optional[str] = None
_real_app_ver_lock = threading.Lock()


def get_real_app_ver() -> str:
    global _real_app_ver
    if _real_app_ver is not None:
        return _real_app_ver
    with _real_app_ver_lock:
        if _real_app_ver is not None:
            return _real_app_ver
        try:
            resp = P115Client.app_version_list2()
            check_response(resp)
            _real_app_ver = resp["data"]["Android"]["version_code"]
            logger.info("获取 115 真实版本号: %s", _real_app_ver)
        except Exception:
            _real_app_ver = FALLBACK_APP_VER
            logger.warning("获取 115 版本号失败，使用回退版本: %s", _real_app_ver)
    return _real_app_ver


def _real_ua(real: str) -> str:
    return f"Mozilla/5.0 115disk/{real} 115Browser/{real} 115wangpan_android/{real}"


def generate_u115_ios() -> str:
    try:
        resp = P115Client.app_version_list2()
        check_response(resp)
        udown_version = resp["data"]["iOS-iPhone"]["version_code"]
        wangpan_version = resp["data"]["115wangpan_iOS"]["version_code"]
    except Exception:
        udown_version = "37.0.7"
        wangpan_version = "36.2.20"
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


def apply_app_ver_patch():
    if not hasattr(_p115_client_mod, _APP_VERSION_ATTR):
        logger.warning("p115client 版本不兼容，未找到 %s 属性", _APP_VERSION_ATTR)
        return
    real = get_real_app_ver()
    setattr(_p115_client_mod, _APP_VERSION_ATTR, real)
    logger.info("app_ver 补丁已应用: %s", real)