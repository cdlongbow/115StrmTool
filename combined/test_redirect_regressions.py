"""
跳转服务并发回归测试

覆盖 same_playback 冷启动下不同 UA 的并发识别与同 UA singleflight
"""
import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import httpx

from conftest import P115_NATIVE_MODULES, REDIRECT_HEAVY_DEPS, stub_missing, stub_modules

stub_missing(P115_NATIVE_MODULES)

API_PATH = "/api/v1/plugin/P115StrmHelper/redirect_url"
PICKCODE = "3" * 17
COPY_PC = "9" * 17
UA_FIRST = "Emby/iOS"
UA_SECOND = "Emby/Android"


def _build_service():
    wrapper = MagicMock()
    wrapper.get_download_url_with_ua.return_value = (
        "https://cdn.115.com/file/movie.mp4?t=1800000000",
        "movie.mp4",
        int(time.time()) + 3600,
    )
    wrapper.copy_pickcode.return_value = COPY_PC
    with patch.dict("sys.modules", stub_modules(REDIRECT_HEAVY_DEPS)):
        from redirect_service import RedirectService

        service = RedirectService(
            wrapper,
            same_playback=True,
            same_playback_dir="/多端播放",
        )
    service._copy_dir_pids["/多端播放"] = 123
    return service


def _blocking_fetch(started: threading.Event, release: threading.Event):
    def _fetch(pickcode: str, user_agent: str):
        if pickcode == PICKCODE:
            started.set()
            if not release.wait(2):
                raise RuntimeError("test release timed out")
        return (
            "https://cdn.115.com/file/movie.mp4?t=1800000000",
            "movie.mp4",
            int(time.time()) + 3600,
        )

    return _fetch


def test_cold_start_different_ua_detects_inflight_and_copies():
    service = _build_service()
    started = threading.Event()
    release = threading.Event()
    service._client.get_download_url_with_ua.side_effect = _blocking_fetch(
        started, release
    )

    async def _run():
        transport = httpx.ASGITransport(app=service.create_app())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            with patch("redirect_service.COPY_CLEANUP_DELAY", 10):
                first = asyncio.create_task(
                    client.get(
                        API_PATH,
                        params={"pickcode": PICKCODE},
                        headers={"user-agent": UA_FIRST},
                    )
                )
                assert await asyncio.to_thread(started.wait, 1)
                second = asyncio.create_task(
                    client.get(
                        API_PATH,
                        params={"pickcode": PICKCODE},
                        headers={"user-agent": UA_SECOND},
                    )
                )
                for _ in range(100):
                    if service._client.copy_pickcode.called:
                        break
                    await asyncio.sleep(0.005)
                release.set()
                return await asyncio.gather(first, second)

    responses = asyncio.run(_run())
    assert [response.status_code for response in responses] == [302, 302]
    service._client.copy_pickcode.assert_called_once()
    fetched_pickcodes = [
        call.args[0]
        for call in service._client.get_download_url_with_ua.call_args_list
    ]
    assert fetched_pickcodes == [PICKCODE, COPY_PC]
    assert service._inflight_uas == {}


def test_cold_start_same_ua_keeps_singleflight():
    service = _build_service()
    started = threading.Event()
    release = threading.Event()
    service._client.get_download_url_with_ua.side_effect = _blocking_fetch(
        started, release
    )

    async def _run():
        transport = httpx.ASGITransport(app=service.create_app())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            first = asyncio.create_task(
                client.get(
                    API_PATH,
                    params={"pickcode": PICKCODE},
                    headers={"user-agent": UA_FIRST},
                )
            )
            assert await asyncio.to_thread(started.wait, 1)
            second = asyncio.create_task(
                client.get(
                    API_PATH,
                    params={"pickcode": PICKCODE},
                    headers={"user-agent": UA_FIRST},
                )
            )
            await asyncio.sleep(0.05)
            assert service._inflight_uas[PICKCODE][service._ua_hash(UA_FIRST)] == 2
            release.set()
            return await asyncio.gather(first, second)

    responses = asyncio.run(_run())
    assert [response.status_code for response in responses] == [302, 302]
    service._client.copy_pickcode.assert_not_called()
    assert service._client.get_download_url_with_ua.call_count == 1
    assert service._inflight_uas == {}
