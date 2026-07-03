from functools import wraps
from typing import Optional

from p115client import P115Client, check_response
import p115client.client as _p115_client_mod

from logger import logger

PLACEHOLDER_APP_VER = "99.99.99.99"

_real_app_ver: Optional[str] = None


def get_real_app_ver() -> str:
    global _real_app_ver
    if _real_app_ver:
        return _real_app_ver
    try:
        resp = P115Client.app_version_list2()
        check_response(resp)
        _real_app_ver = resp["data"]["Android"]["version_code"]
        logger.info("获取 115 真实版本号: %s", _real_app_ver)
    except Exception:
        _real_app_ver = "37.2.5"
        logger.warning("获取 115 版本号失败，使用回退版本: %s", _real_app_ver)
    return _real_app_ver


_MARKER = "__app_ver_patched__"


def apply_patch():
    original = _p115_client_mod.get_request
    if getattr(original, _MARKER, False):
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
    logger.info("app_ver 补丁已应用")