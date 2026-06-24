from typing import Any, Dict, Optional
from base64 import b64encode

from logger import logger


class P115ClientWrapper:
    def __init__(self, cookie: str = ""):
        self._cookie = cookie
        self._client = None
        self._init_client()

    def _init_client(self):
        if not self._cookie:
            logger.warning("115 Cookie 未配置")
            return
        try:
            from p115client import P115Client
            self._client = P115Client(cookies=self._cookie)
            logger.info("115 客户端初始化成功")
        except ImportError:
            logger.error("p115client 库未安装，请执行: pip install p115client==0.0.8.10")
            self._client = None
        except Exception as e:
            logger.error("115 客户端初始化失败: %s", e, exc_info=True)
            self._client = None

    def update_cookie(self, cookie: str):
        self._cookie = cookie
        self._init_client()

    @property
    def client(self):
        return self._client

    def is_ready(self) -> bool:
        return self._client is not None

    def get_download_url(self, pickcode: str) -> Optional[str]:
        if not self._client:
            logger.warning("115 客户端未初始化")
            return None
        try:
            result = self._client.download_url(pickcode)
            if isinstance(result, dict):
                url = result.get("url") or result.get("file_url")
                if url:
                    return url
            elif isinstance(result, str):
                return result
            return None
        except Exception as e:
            logger.error("获取下载地址失败 pickcode=%s: %s", pickcode, e, exc_info=True)
            return None

    def list_files(self, pid: str = "0") -> list:
        if not self._client:
            return []
        try:
            return self._client.files(pid) or []
        except Exception as e:
            logger.error("获取文件列表失败 pid=%s: %s", pid, e, exc_info=True)
            return []

    def get_filesystem(self) -> Optional[Any]:
        if not self._client:
            return None
        try:
            return self._client.fs()
        except Exception as e:
            logger.error("获取文件系统失败: %s", e, exc_info=True)
            return None

    def get_user_info(self) -> Optional[Dict]:
        if not self._client:
            return None
        try:
            return self._client.user_info()
        except Exception as e:
            logger.error("获取用户信息失败: %s", e, exc_info=True)
            return None

    def get_storage_info(self) -> Optional[Dict]:
        if not self._client:
            return None
        try:
            return self._client.fs_storage_info()
        except Exception as e:
            logger.error("获取存储信息失败: %s", e, exc_info=True)
            return None

    def get_qrcode(self) -> Optional[Dict]:
        try:
            from p115client import P115Client
            token_resp = P115Client.login_qrcode_token()
            if not token_resp or not token_resp.get("data"):
                return None
            uid = str(token_resp["data"]["uid"])
            qr_bytes = P115Client.login_qrcode(uid)
            if not isinstance(qr_bytes, (bytes, bytearray)):
                return None
            return {
                "uid": uid,
                "qrcode": f"data:image/png;base64,{b64encode(qr_bytes).decode()}",
            }
        except Exception as e:
            logger.error("获取二维码失败: %s", e, exc_info=True)
            return None

    def check_qrcode(self, uid: str) -> Optional[Dict]:
        try:
            if not self._client:
                return {"status": "waiting"}
            from p115client import check_response
            resp = self._client.login_qrcode_scan_status(uid)
            data = resp.get("data", {})
            if data.get("status") == 1 and "cookie" in data:
                cookie_dict = data["cookie"]
                if isinstance(cookie_dict, dict):
                    cookie_str = "; ".join(
                        f"{k}={v}" for k, v in cookie_dict.items() if k and v
                    )
                else:
                    cookie_str = str(cookie_dict)
                self._cookie = cookie_str
                self._init_client()
                return {"status": "success", "cookie": cookie_str}
            if data.get("status") == 2:
                return {"status": "expired"}
            return {"status": "waiting"}
        except Exception as e:
            logger.error("检查二维码状态失败: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}
