"""
302 跳转服务缓存 TTL 语义测试

缓存 TTL 不得超过下载地址的剩余有效期（expires_time 已含 300 秒过期余量），
也不得超过默认上限；即将/已经过期的地址不进入缓存，
避免客户端拿到过期 URL 导致播放失败
"""
import asyncio
import time
from unittest.mock import MagicMock, patch

import httpx

API_PATH = "/api/v1/plugin/P115StrmHelper/redirect_url"
PICKCODE = "2" * 17
UA = "ttl-test-ua"


def _build_service(expires_time: int):
    wrapper = MagicMock()
    wrapper.get_download_url_with_ua.return_value = (
        "https://cdn.115.com/file/movie.mp4?t=1800000000",
        "movie.mp4",
        expires_time,
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
        headers={"user-agent": UA},
    )


def _request_twice(svc):
    async def _run():
        async with _async_client(svc) as client:
            r1 = await client.get(API_PATH, params={"pickcode": PICKCODE})
            r2 = await client.get(API_PATH, params={"pickcode": PICKCODE})
            return r1, r2

    return asyncio.run(_run())


def test_valid_url_cached():
    svc = _build_service(int(time.time()) + 3600)
    wrapper = svc._client
    wrapper.get_download_url_with_ua.reset_mock()
    r1, r2 = _request_twice(svc)
    assert r1.status_code == 302 and r2.status_code == 302
    assert wrapper.get_download_url_with_ua.call_count == 1, (
        "有效期内第二次请求应命中缓存"
    )


def test_expired_url_not_cached():
    svc = _build_service(int(time.time()) - 5)
    wrapper = svc._client
    wrapper.get_download_url_with_ua.reset_mock()
    r1, r2 = _request_twice(svc)
    assert r1.status_code == 302 and r2.status_code == 302
    assert wrapper.get_download_url_with_ua.call_count == 2, (
        "已过期地址不得进入缓存，第二次请求应重新回源"
    )


def test_ttl_capped_at_default():
    svc = _build_service(int(time.time()) + 86400)
    r1, _ = _request_twice(svc)
    assert r1.status_code == 302
    ckey = svc._cache_key(PICKCODE, UA)
    entry = svc._cache._data.get(ckey)
    assert entry is not None, "长有效期地址应进入缓存"
    _, expiry = entry
    remaining = expiry - time.monotonic()
    assert remaining <= 90, f"TTL 不得超过默认上限 90 秒，实际 {remaining}"
    assert remaining > 0
