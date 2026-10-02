from hashlib import sha256
from json import dumps as json_dumps
from time import time
from typing import Optional, Tuple
from urllib.parse import quote, unquote, urlsplit

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from config_manager import config_manager
from logger import logger
from p115_client_wrapper import P115ClientWrapper
from utils import AsyncKeyLock, AsyncTtlCache

CACHE_TTL_DEFAULT = 90
DOWNLOAD_API_PATH = "/api/v1/plugin/P115StrmHelper/redirect_url"
COPY_CLEANUP_DELAY = 5.0
COPY_DIR_DEFAULT = "/多端播放"


class RedirectService:
    def __init__(
        self,
        client: P115ClientWrapper,
        same_playback: Optional[bool] = None,
        same_playback_dir: Optional[str] = None,
    ):
        self._client = client
        self._cache = AsyncTtlCache(ttl=CACHE_TTL_DEFAULT, max_size=1000)
        self._key_lock = AsyncKeyLock()
        self._same_playback_override = same_playback
        self._same_playback_dir_override = same_playback_dir
        self._copy_dir_pid: Optional[int] = None

    def _get_same_playback(self) -> Tuple[bool, str]:
        """
        读取多端播放配置

        :return Tuple: (是否开启多端播放, 副本目录)
        """
        if self._same_playback_override is not None:
            return (
                self._same_playback_override,
                self._same_playback_dir_override or COPY_DIR_DEFAULT,
            )
        cfg = config_manager.get().get("p115", {})
        return (
            bool(cfg.get("same_playback", False)),
            cfg.get("same_playback_dir") or COPY_DIR_DEFAULT,
        )

    async def _resolve_copy_dir_pid(self, copy_dir: str) -> Optional[int]:
        """
        解析副本目录 ID，首次调用时创建目录并缓存

        :param copy_dir (str): 副本目录网盘路径

        :return int: 目录 ID，失败返回 None
        """
        if self._copy_dir_pid is not None:
            return self._copy_dir_pid
        pid = await asyncio.to_thread(self._client.ensure_folder, copy_dir)
        if pid:
            self._copy_dir_pid = pid
        return pid

    async def _create_copy(self, pickcode: str, copy_dir: str) -> Optional[str]:
        """
        为多端播放创建文件副本

        :param pickcode (str): 源文件 pickcode
        :param copy_dir (str): 副本目录网盘路径

        :return str: 副本 pickcode，失败返回 None
        """
        pid = await self._resolve_copy_dir_pid(copy_dir)
        if not pid:
            logger.warning("【302跳转服务】无法解析多端播放目录: %s", copy_dir)
            return None
        return await asyncio.to_thread(self._client.copy_pickcode, pickcode, pid)

    def _schedule_copy_cleanup(self, pickcode: str) -> None:
        """
        延迟清理多端播放副本，避免长期占用网盘空间

        :param pickcode (str): 副本 pickcode
        """

        async def _cleanup():
            await asyncio.sleep(COPY_CLEANUP_DELAY)
            await asyncio.to_thread(self._client.delete_file, pickcode)

        try:
            asyncio.create_task(_cleanup())
        except RuntimeError:
            logger.warning("【302跳转服务】无法调度副本清理: %s", pickcode)

    def create_app(self) -> FastAPI:
        app = FastAPI(title="115 STRM 302 跳转服务")

        @app.api_route(DOWNLOAD_API_PATH, methods=["GET", "HEAD", "POST"])
        async def redirect_url_endpoint(request: Request):
            pickcode = request.query_params.get("pickcode") or ""
            return await self._do_redirect(pickcode, request)

        @app.api_route(DOWNLOAD_API_PATH + "/{file_id}", methods=["GET", "HEAD", "POST"])
        async def redirect_url_with_id(request: Request, file_id: str):
            pickcode = request.query_params.get("pickcode") or file_id
            return await self._do_redirect(pickcode, request)

        @app.api_route("/redirect_url", methods=["GET", "HEAD", "POST"])
        async def redirect_url_short(request: Request):
            pickcode = request.query_params.get("pickcode") or ""
            return await self._do_redirect(pickcode, request)

        @app.api_route("/{path:path}", methods=["GET", "HEAD"])
        async def fallback_redirect(request: Request, path: str):
            pickcode = self._extract_pickcode_from_path(path, request)
            return await self._do_redirect(pickcode, request)

        return app

    @staticmethod
    def _ua_hash(user_agent: str) -> str:
        return sha256(user_agent.encode("utf-8")).hexdigest()[:16]

    def _cache_key(self, pickcode: str, ua: str) -> str:
        return f"{pickcode}:{self._ua_hash(ua)}"

    @staticmethod
    def _real_client_ip(request: Request) -> str:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",", 1)[0].strip()
        xri = request.headers.get("x-real-ip")
        if xri:
            return xri.strip()
        return request.client.host if request.client else ""

    @staticmethod
    def _extract_file_name(url: str) -> str:
        return unquote(urlsplit(url).path.rpartition("/")[-1])

    async def _do_redirect(self, pickcode: str, request: Request) -> Response:
        client_ip = self._real_client_ip(request)
        user_agent = (request.headers.get("user-agent") or "")[:256]
        logger.debug("【302跳转服务】UA: %s", user_agent[:64])

        if not pickcode or len(pickcode) != 17 or not pickcode.isalnum():
            logger.debug("【302跳转服务】无效 pickcode: %s", pickcode)
            return JSONResponse(
                status_code=400,
                content={"code": -1, "msg": "Missing or invalid pickcode", "data": None},
            )

        ckey = self._cache_key(pickcode, user_agent)
        # 缓存击穿防护：同一 pickcode+UA 并发回源只执行一次，
        # 其余请求在锁内二次检查缓存后直接复用结果
        async with self._key_lock.acquire(ckey):
            async with self._cache.lock:
                cached = self._cache.get(ckey)
            if cached:
                cached_url, cached_fname = cached
                logger.debug(
                    "【302跳转服务】缓存命中: pickcode=%s file_name=%s ip=%s",
                    pickcode, cached_fname, client_ip,
                )
                return self._build_302(cached_url, pickcode, cached_fname)

            post_pickcode = pickcode
            same_playback, copy_dir = self._get_same_playback()
            if same_playback:
                async with self._cache.lock:
                    other_ua = self._cache.count_prefix(f"{pickcode}:")
                if other_ua > 0:
                    copied = await self._create_copy(pickcode, copy_dir)
                    if copied:
                        post_pickcode = copied
                        logger.info(
                            "【302跳转服务】多端播放创建副本: pickcode=%s copy=%s ip=%s",
                            pickcode, copied, client_ip,
                        )
                    else:
                        logger.warning(
                            "【302跳转服务】多端播放复制失败，回退原文件: pickcode=%s",
                            pickcode,
                        )

            try:
                result = await asyncio.to_thread(
                    self._client.get_download_url_with_ua, post_pickcode, user_agent
                )
            finally:
                if post_pickcode != pickcode:
                    self._schedule_copy_cleanup(post_pickcode)
            if not result:
                logger.error(
                    "【302跳转服务】获取 115 下载地址失败: pickcode=%s ip=%s",
                    pickcode, client_ip,
                )
                return JSONResponse(
                    status_code=502,
                    content={"code": -1, "msg": "Failed to resolve download URL", "data": None},
                )

            download_url, file_name, expires_time = result
            # expires_time 已预留 300 秒过期余量；缓存时间不超过剩余有效期，
            # 也不超过默认上限，避免把即将/已经过期的 URL 继续分发给客户端
            ttl = min(CACHE_TTL_DEFAULT, expires_time - int(time()))
            if ttl > 0:
                async with self._cache.lock:
                    self._cache.put(ckey, (download_url, file_name), ttl=ttl)
            else:
                logger.warning(
                    "【302跳转服务】下载地址剩余有效期过短（可能已过期），仍按原样返回且不缓存: "
                    "pickcode=%s expires_in=%ss",
                    pickcode, expires_time - int(time()),
                )
            logger.debug(
                "【302跳转服务】获取 115 下载地址成功: pickcode=%s file_name=%s ttl=%ss ip=%s",
                pickcode, file_name, ttl, client_ip,
            )
            return self._build_302(download_url, pickcode, file_name)

    def _build_302(self, url: str, pickcode: str, file_name: str = "") -> Response:
        if not file_name:
            file_name = pickcode
        try:
            file_name.encode("ascii")
            content_disposition = f'attachment; filename="{file_name}"'
        except UnicodeEncodeError:
            encoded_filename = quote(file_name, safe="")
            content_disposition = f"attachment; filename*=UTF-8\'\'{encoded_filename}"
        return Response(
            status_code=302,
            headers={
                "Location": url,
                "Content-Disposition": content_disposition,
            },
            media_type="application/json; charset=utf-8",
            content=json_dumps({"status": "redirecting", "url": url}),
        )

    def _extract_pickcode_from_path(self, path: str, request: Request) -> Optional[str]:
        pickcode = request.query_params.get("pickcode")
        if pickcode:
            return pickcode
        path = path.lstrip("/").split("/")[-1]
        if path and len(path) == 17 and path.isalnum():
            return path
        return None
