from typing import Optional

from p115client import P115Client, check_response

from logger import logger

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