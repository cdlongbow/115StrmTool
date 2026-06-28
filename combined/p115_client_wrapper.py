from base64 import b64encode
from json import loads as json_loads
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from httpx import Client, Limits, Timeout
from p115rsacipher import encrypt, decrypt

from logger import logger


P115_DOWNLOAD_API = "http://proapi.115.com/android/2.0/ufile/download"
P115_UA_IOS = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148"


class P115ClientWrapper:
    def __init__(self, cookie: str = ""):
        self._cookie = cookie
        self._client = None
        self._http_client: Optional[Client] = None
        self._init_client()

    def _init_client(self):
        if not self._cookie:
            logger.warning("115 Cookie 未配置")
            return
        try:
            from p115client import P115Client
            self._client = P115Client(cookies=self._cookie)
            self._http_client = Client(
                cookies=self._parse_cookie(self._cookie),
                follow_redirects=True,
                timeout=Timeout(10.0, connect=5.0),
                limits=Limits(max_connections=200, max_keepalive_connections=100),
            )
            logger.info("115 客户端初始化成功")
        except ImportError:
            logger.error("p115client 库未安装，请执行: pip install p115client==0.0.8.10")
            self._client = None
        except Exception as e:
            logger.error("115 客户端初始化失败: %s", e, exc_info=True)
            self._client = None

    @staticmethod
    def _parse_cookie(cookie_str: str) -> Dict[str, str]:
        result = {}
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                result[k.strip()] = v.strip()
        return result

    def update_cookie(self, cookie: str):
        self._cookie = cookie
        if self._http_client:
            self._http_client.close()
            self._http_client = None
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

    def get_download_url_with_ua(
        self, pickcode: str, user_agent: str = ""
    ) -> Optional[Tuple[str, str, int]]:
        """
        使用加密 API 获取 115 下载地址，URL 绑定指定的 User-Agent

        :param pickcode (str): 文件 pickcode
        :param user_agent (str): 客户端 User-Agent，URL 将绑定此 UA

        :return Tuple: (下载 URL, 文件名, 过期时间戳), 失败返回 None
        """
        if not self._http_client:
            logger.warning("115 HTTP 客户端未初始化")
            return None
        if not user_agent:
            user_agent = P115_UA_IOS
        try:
            payload = encrypt(f'{{"pick_code":"{pickcode}"}}').decode("utf-8")
            resp = self._http_client.post(
                P115_DOWNLOAD_API,
                data={"data": payload},
                headers={"User-Agent": user_agent},
            )
            if resp.status_code != 200:
                logger.error(
                    "115 下载 API 返回非 200: status=%s pickcode=%s",
                    resp.status_code,
                    pickcode,
                )
                return None
            json = resp.json()
            if not json.get("state"):
                logger.error(
                    "115 下载 API 返回失败: pickcode=%s resp=%s",
                    pickcode,
                    json,
                )
                return None
            decrypted = decrypt(json["data"])
            data = json_loads(decrypted)
            url = data.get("url") or ""
            if not url:
                logger.error(
                    "115 下载 API 返回无 URL: pickcode=%s data=%s",
                    pickcode,
                    data,
                )
                return None
            file_name = unquote(urlsplit(url).path.rpartition("/")[-1])
            t = int(
                next(
                    (v[0] for k, v in parse_qs(urlsplit(url).query).items() if k == "t"),
                    0,
                )
            )
            expires_time = t - 300
            return (url, file_name, expires_time)
        except Exception as e:
            logger.error(
                "加密获取下载地址失败 pickcode=%s: %s",
                pickcode,
                e,
                exc_info=True,
            )
            return None

    def close(self):
        if self._http_client:
            self._http_client.close()
            self._http_client = None

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

    def user_points_sign(self) -> Optional[Dict]:
        if not self._client:
            return None
        try:
            return self._client.user_points_sign()
        except Exception as e:
            logger.error("查询签到状态失败: %s", e, exc_info=True)
            return None

    def user_points_sign_post(self) -> Optional[Dict]:
        if not self._client:
            return None
        try:
            return self._client.user_points_sign_post()
        except Exception as e:
            logger.error("执行签到失败: %s", e, exc_info=True)
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
            token_resp = P115Client.login_qrcode_token()
            if not token_resp or not token_resp.get("data"):
                return None
            payload = token_resp["data"]
            uid = str(payload["uid"])
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
