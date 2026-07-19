from hashlib import sha256
from json import dumps as json_dumps
from time import time
from typing import Dict, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse, Response

from logger import logger
from p115_client_wrapper import P115ClientWrapper
from utils import AsyncTtlCache, retry_with_backoff

CACHE_TTL_DEFAULT = 90
DOWNLOAD_API_PATH = "/api/v1/plugin/P115StrmHelper/redirect_url"

import asyncio


class RedirectService:
    def __init__(self, client: P115ClientWrapper):
        self._client = client
        self._cache = AsyncTtlCache(ttl=CACHE_TTL_DEFAULT, max_size=1000)

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
        async with self._cache.lock:
            cached = self._cache.get(ckey)
        if cached:
            cached_url, cached_fname = cached
            logger.info(
                "【302跳转服务】缓存命中: pickcode=%s file_name=%s ip=%s",
                pickcode, cached_fname, client_ip,
            )
            return self._build_302(cached_url, pickcode, cached_fname)

        result = await retry_with_backoff(
            lambda: asyncio.to_thread(
                self._client.get_download_url_with_ua, pickcode, user_agent
            ),
            max_retries=2,
            base_delay=0.5,
        )
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
        ttl = max(CACHE_TTL_DEFAULT, expires_time - int(time()))
        async with self._cache.lock:
            self._cache.ttl = ttl
            self._cache.put(ckey, (download_url, file_name))
            self._cache.ttl = CACHE_TTL_DEFAULT
        logger.info(
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

    def clear_cache(self):
        self._cache.clear()
        logger.info("302 缓存已清理")