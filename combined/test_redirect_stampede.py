"""
302 跳转服务缓存击穿（stampede）防护测试

并发请求相同 pickcode+UA 时，回源获取下载地址只执行一次，其余请求复用缓存结果
"""
import asyncio
from unittest.mock import MagicMock, patch

import httpx
import pytest

API_PATH = "/api/v1/plugin/P115StrmHelper/redirect_url"
PICKCODE = "1" * 17


def _build_service():
    wrapper = MagicMock()
    wrapper.get_download_url_with_ua.return_value = (
        "https://cdn.115.com/file/movie.mp4?t=1800000000",
        "movie.mp4",
        1800000000,
    )
    with patch.dict("sys.modules", {
        "p115_client_wrapper": MagicMock(),
    }):
        from redirect_service import RedirectService
        svc = RedirectService(wrapper)
    return svc


def _async_client(svc):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=svc.create_app()),
        base_url="http://test",
    )


def test_concurrent_same_pickcode_fetch_once():
    svc = _build_service()
    wrapper = svc._client
    wrapper.get_download_url_with_ua.reset_mock()

    async def _run():
        async with _async_client(svc) as client:
            responses = await asyncio.gather(*(
                client.get(API_PATH, params={"pickcode": PICKCODE})
                for _ in range(10)
            ))
            return responses

    responses = asyncio.run(_run())
    assert all(r.status_code == 302 for r in responses)
    assert wrapper.get_download_url_with_ua.call_count == 1, (
        f"期望回源 1 次，实际 {wrapper.get_download_url_with_ua.call_count} 次"
    )


def test_distinct_pickcodes_fetch_each():
    svc = _build_service()
    wrapper = svc._client
    wrapper.get_download_url_with_ua.reset_mock()

    async def _run():
        async with _async_client(svc) as client:
            responses = await asyncio.gather(*(
                client.get(API_PATH, params={"pickcode": f"1{i:016d}"})
                for i in range(5)
            ))
            return responses

    responses = asyncio.run(_run())
    assert all(r.status_code == 302 for r in responses)
    assert wrapper.get_download_url_with_ua.call_count == 5, (
        f"不同 pickcode 应各自回源，实际 {wrapper.get_download_url_with_ua.call_count} 次"
    )
