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

    def list_files(self, cid: str = "0") -> list:
        if not self._client:
            return []
        try:
            resp = self._client.fs_files({"cid": cid, "limit": 1000})
            if isinstance(resp, dict) and "data" in resp:
                return resp.get("data", [])
            return []
        except Exception as e:
            logger.error("获取文件列表失败 cid=%s: %s", cid, e, exc_info=True)
            return []

    def get_filesystem(self) -> Optional[Any]:
        if not self._client:
            return None
        try:
            return self._client.get_fs()
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

    def get_qrcode(self, app: str = "alipaymini") -> Optional[Dict]:
        try:
            from p115client import P115Client
            # 获取 QR code token，使用默认 app="web" 匹配原始实现
            token_resp = P115Client.login_qrcode_token()
            if not token_resp or not token_resp.get("data"):
                return None
            payload = token_resp["data"]
            uid = str(payload["uid"])
            # 下载二维码图片，使用默认 app="web"
            qr_bytes = P115Client.login_qrcode(uid)
            if not isinstance(qr_bytes, (bytes, bytearray)):
                return None
            return {
                "uid": uid,
                "payload": payload,
                "qrcode": f"data:image/png;base64,{b64encode(qr_bytes).decode()}",
                "client_type": app,
            }
        except Exception as e:
            logger.error("获取二维码失败: %s", e, exc_info=True)
            return None

    def check_qrcode(self, payload: dict) -> Optional[Dict]:
        try:
            from p115client import P115Client
            resp = P115Client.login_qrcode_scan_status(payload)
            data = resp.get("data", {})
            status = data.get("status")

            if status == 0:
                return {"status": "waiting", "msg": "等待扫码"}
            if status == 1:
                return {"status": "scanned", "msg": "已扫码，等待确认"}
            if status in (-1, None):
                return {"status": "expired", "msg": "二维码已过期"}
            if status == -2:
                return {"status": "expired", "msg": "用户取消登录"}

            # status == 2: 已确认登录，获取 cookie
            if status == 2:
                client_type = payload.get("client_type", "alipaymini")
                uid = str(payload.get("uid", ""))
                result_resp = P115Client.login_qrcode_scan_result(uid, app=client_type)
                if result_resp.get("state") and result_resp.get("data"):
                    cookie_data = result_resp["data"]
                    cookie_dict = cookie_data.get("cookie", {})
                    if isinstance(cookie_dict, dict):
                        cookie_str = "; ".join(
                            f"{k}={v}" for k, v in cookie_dict.items() if k and v
                        )
                    else:
                        cookie_str = str(cookie_dict)
                    if cookie_str:
                        self._cookie = cookie_str
                        self._init_client()
                        return {"status": "success", "cookie": cookie_str}
                return {"status": "error", "msg": "获取登录结果失败"}

            return {"status": "waiting"}
        except Exception as e:
            logger.error("检查二维码状态失败: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}
