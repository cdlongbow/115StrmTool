from base64 import b64encode
from json import loads as json_loads
from time import monotonic, sleep
from threading import Lock
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from httpx import Client, Limits, Timeout
from p115cipher import rsa_decrypt, rsa_encrypt

from app_ver import generate_u115_ios
from logger import logger


P115_DOWNLOAD_API = "http://proapi.115.com/android/2.0/ufile/download"
DEFAULT_ENDPOINT_COOLDOWNS = {
    "fs_files": 0.5,
    "fs_dir_getid": 0.5,
    "download_url": 1.0,
    "user_points_sign": 0.5,
    "user_points_sign_post": 0.5,
    "share_snap": 1.5,
}


class P115ClientWrapper:
    def __init__(self, cookie: str = ""):
        self._cookie = cookie
        self._client = None
        self._http_client: Optional[Client] = None
        self._cooldown_lock = Lock()
        self._last_call: Dict[str, float] = {}
        self._cooldowns = dict(DEFAULT_ENDPOINT_COOLDOWNS)
        self._init_client()

    def _wait_cooling(self, endpoint: str):
        cooldown = self._cooldowns.get(endpoint, 0)
        if cooldown <= 0:
            return
        with self._cooldown_lock:
            now = monotonic()
            last = self._last_call.get(endpoint, 0)
            remaining = cooldown - (now - last)
        if remaining > 0:
            sleep(remaining)
        with self._cooldown_lock:
            self._last_call[endpoint] = monotonic()

    def set_endpoint_cooldown(self, endpoint: str, cooldown: float):
        self._cooldowns[endpoint] = cooldown

    def _init_client(self):
        if not self._cookie:
            logger.warning("115 Cookie 未配置")
            return
        try:
            from p115client import P115Client
            from p115client.tool.fs_files import get_webapi_origin

            self._client = P115Client(cookies=self._cookie)

            _orig_fs_files = self._client.fs_files
            _orig_fs_dir_getid = self._client.fs_dir_getid

            def _fs_files_rotating(
                payload=None,
                base_url=None,
                *,
                async_=False,
                **kwargs,
            ):
                if base_url is None:
                    base_url = get_webapi_origin()
                return _orig_fs_files(
                    payload, base_url=base_url, async_=async_, **kwargs,
                )

            def _fs_dir_getid_rotating(
                payload,
                base_url=None,
                *,
                async_=False,
                **kwargs,
            ):
                if base_url is None:
                    base_url = get_webapi_origin()
                return _orig_fs_dir_getid(
                    payload, base_url=base_url, async_=async_, **kwargs,
                )

            self._client.fs_files = _fs_files_rotating
            self._client.fs_dir_getid = _fs_dir_getid_rotating

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
            self._wait_cooling("download_url")
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
            user_agent = generate_u115_ios()
        try:
            payload = rsa_encrypt(
                f'{{"pick_code":"{pickcode}"}}'.encode("utf-8")
            ).decode("utf-8")
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
            decrypted = rsa_decrypt(json["data"]).decode("utf-8")
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
            self._wait_cooling("fs_files")
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
            self._wait_cooling("user_points_sign")
            return self._client.user_points_sign()
        except Exception as e:
            logger.error("查询签到状态失败: %s", e, exc_info=True)
            return None

    def user_points_sign_post(self) -> Optional[Dict]:
        if not self._client:
            return None
        try:
            self._wait_cooling("user_points_sign_post")
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
            from io import BytesIO
            import qrcode as qrcode_lib

            token_resp = P115Client.login_qrcode_token()
            if not token_resp or not token_resp.get("data"):
                return None
            payload = token_resp["data"]
            _uid = str(payload.get("uid") or "")
            _time = payload.get("time")
            _sign = payload.get("sign")
            if not _uid or not _time or not _sign:
                logger.error("获取二维码失败: 返回登录参数不完整")
                return None

            qrcode_content = f"https://115.com/scan/dg-{_uid}"
            img = qrcode_lib.make(qrcode_content)
            buffered = BytesIO()
            img.save(buffered, format="PNG")

            return {
                "uid": _uid,
                "payload": payload,
                "qrcode": f"data:image/png;base64,{b64encode(buffered.getvalue()).decode()}",
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
