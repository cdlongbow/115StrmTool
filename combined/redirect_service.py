from json import dumps as json_dumps
from time import monotonic
from typing import Dict, Optional
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse, Response

from logger import logger
from p115_client_wrapper import P115ClientWrapper

CACHE_TTL = 90

REDIRECT_API_PATH = "/api/v1/plugin/P115StrmHelper/redirect_url"


class RedirectService:
    def __init__(self, client: P115ClientWrapper):
        self._client = client
        self._cache: Dict[str, tuple] = {}

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

    async def _do_redirect(self, pickcode: str, request: Request) -> Response:
        if not pickcode or len(pickcode) != 17 or not pickcode.isalnum():
            return JSONResponse(
                status_code=400,
                content={"code": -1, "msg": "Missing or invalid pickcode", "data": None},
            )

        cached = self._get_cached(pickcode)
        if cached:
            logger.debug("302 缓存命中: pickcode=%s", pickcode)
            return self._build_302(cached, pickcode)

        download_url = self._client.get_download_url(pickcode)
        if not download_url:
            return JSONResponse(
                status_code=502,
                content={"code": -1, "msg": "Failed to resolve download URL", "data": None},
            )

        self._set_cache(pickcode, download_url)
        logger.info("302 跳转: pickcode=%s -> %s", pickcode, download_url)
        return self._build_302(download_url, pickcode)

    def _build_302(self, url: str, pickcode: str) -> Response:
        return Response(
            status_code=302,
            headers={
                "Location": url,
                "Content-Disposition": f'attachment; filename="{pickcode}"',
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

    def _get_cached(self, key: str) -> Optional[str]:
        entry = self._cache.get(key)
        if entry:
            url, expiry = entry
            if monotonic() < expiry:
                return url
            del self._cache[key]
        return None

    def _set_cache(self, key: str, url: str):
        self._cache[key] = (url, monotonic() + CACHE_TTL)

    def clear_cache(self):
        self._cache.clear()
        logger.info("302 缓存已清理")
