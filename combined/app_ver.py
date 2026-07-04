import threading
from functools import wraps
from random import choice, randint
from typing import Optional

from p115client import P115Client, check_response
import p115client.client as _p115_client_mod

from logger import logger

PLACEHOLDER_APP_VER = "99.99.99.99"
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


_MARKER = "__app_ver_patched__"


def apply_patch():
    original = _p115_client_mod.get_request
    if getattr(original, _MARKER, False):
        logger.debug("app_ver 补丁已存在，跳过")
        return

    @wraps(original)
    def patched(*args, **kwargs):
        request, request_kwargs = original(*args, **kwargs)
        params = request_kwargs.get("params")
        if (
            isinstance(params, dict)
            and params.get("app_ver") == PLACEHOLDER_APP_VER
        ):
            params["app_ver"] = get_real_app_ver()
        return request, request_kwargs

    setattr(patched, _MARKER, True)
    _p115_client_mod.get_request = patched
    logger.info("app_ver 全局补丁已应用")