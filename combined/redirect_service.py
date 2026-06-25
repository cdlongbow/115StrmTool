from json import dumps as json_dumps
from time import monotonic
from typing import Dict, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse, Response

from logger import logger
from p115_client_wrapper import P115ClientWrapper

CACHE_TTL = 90

REDIRECT_API_PATH = "/api/v1/plugin/P115StrmHelper/redirect_url"


class RedirectService:
    def __init__(self, client: P115ClientWrapper):
        self._client = client
        self._cache: Dict[str, Tuple[str, str, float]] = {}

    def create_app(self) -> FastAPI:
        app = FastAPI(title="115 STRM 302 跳转服务")

        @app.api_route(REDIRECT_API_PATH, methods=["GET", "HEAD", "POST"])
        async def redirect_url_endpoint(request: Request):
            pickcode = request.query_params.get("pickcode") or ""
            return await self._do_redirect(pickcode, request)

        @app.api_route(REDIRECT_API_PATH + "/{file_id}", methods=["GET", "HEAD", "POST"])
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
        user_agent = (request.headers.get("user-agent") or "")[:64]
        logger.debug("【302跳转服务】获取到客户端UA: %s", user_agent)

        if not pickcode or len(pickcode) != 17 or not pickcode.isalnum():
            logger.debug("【302跳转服务】Missing or bad pickcode: %s", pickcode)
            return JSONResponse(
                status_code=400,
                content={"code": -1, "msg": "Missing or invalid pickcode", "data": None},
            )

        cached = self._get_cached(pickcode)
        if cached:
            cached_url, cached_fname = cached
            logger.info("【302跳转服务】缓存命中: pickcode=%s file_name=%s ip=%s", pickcode, cached_fname, client_ip)
            return self._build_302(cached_url, pickcode, cached_fname)

        download_url = self._client.get_download_url(pickcode)
        if not download_url:
            logger.error("【302跳转服务】获取 115 下载地址失败: pickcode=%s ip=%s", pickcode, client_ip)
            return JSONResponse(
                status_code=502,
                content={"code": -1, "msg": "Failed to resolve download URL", "data": None},
            )

        file_name = self._extract_file_name(download_url)
        self._set_cache(pickcode, download_url, file_name)
        logger.info("【302跳转服务】获取 115 下载地址成功: pickcode=%s file_name=%s url=%s ip=%s", pickcode, file_name, download_url, client_ip)
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

    def _get_cached(self, key: str) -> Optional[Tuple[str, str]]:
        entry = self._cache.get(key)
        if entry:
            url, fname, expiry = entry
            if monotonic() < expiry:
                return (url, fname)
            del self._cache[key]
        return None

    def _set_cache(self, key: str, url: str, file_name: str):
        self._cache[key] = (url, file_name, monotonic() + CACHE_TTL)

    def clear_cache(self):
        self._cache.clear()
        logger.info("302 缓存已清理")
