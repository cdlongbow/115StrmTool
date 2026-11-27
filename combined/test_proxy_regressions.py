"""
流代理与 Emby 前置代理回归测试

覆盖有界 Range 续传、显式 MediaSource、客户端会话隔离、凭据剥离、
响应头超时、错误正文上限、304 透传、Items 校验器与日志脱敏
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx
import pytest

from proxy_app import _media_url_cache_ttl, create_app

ENTRY_URL = "http://entry.test/redirect_url?pickcode=" + "1" * 17
ENTRY_URL_2 = "http://entry.test/redirect_url?pickcode=" + "2" * 17
CDN_PREFIX = "http://cdn.test/"
ITEM_ID = "item1"
MEDIA_URL = f"/videos/{ITEM_ID}/movie.mp4?MediaSourceId=ms1"
LAN_HEADERS = {"x-forwarded-for": "192.168.1.50"}


class FlakyStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        raise httpx.RemoteProtocolError("peer closed connection")

    async def aclose(self):
        self.closed = True


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, count=2000, chunk_size=1024, hang=False):
        self.count = count
        self.chunk_size = chunk_size
        self.hang = hang
        self.read = 0
        self.closed = False

    async def __aiter__(self):
        chunk = b"x" * self.chunk_size
        for _ in range(self.count):
            self.read += len(chunk)
            yield chunk
        if self.hang:
            await asyncio.sleep(60)

    async def aclose(self):
        self.closed = True


class ScriptedUpstream:
    def __init__(
        self,
        cdn_script=None,
        playback_sources=None,
        emby_body=b"EMBY",
        items_response=None,
    ):
        self.cdn_script = cdn_script or [
            (
                206,
                {"content-range": "bytes 0-3/4", "content-length": "4"},
                b"BODY",
            )
        ]
        self.playback_sources = playback_sources or []
        self.emby_body = emby_body
        self.items_response = items_response
        self.head_count = 0
        self.get_count = 0
        self.entry_requests = []
        self.cdn_requests = []
        self.emby_requests = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        path = url.split("?", 1)[0].lower()
        if (
            url.startswith("http://emby.test")
            and request.method == "POST"
            and path.endswith("/playbackinfo")
        ):
            self.emby_requests.append(request)
            return httpx.Response(
                200, json={"MediaSources": self.playback_sources}
            )
        if url.startswith("http://entry.test/redirect_url") and request.method == "HEAD":
            self.head_count += 1
            self.entry_requests.append(request)
            return httpx.Response(
                302, headers={"location": f"{CDN_PREFIX}f{self.head_count}"}
            )
        if url.startswith(CDN_PREFIX) and request.method == "GET":
            index = min(self.get_count, len(self.cdn_script) - 1)
            self.get_count += 1
            self.cdn_requests.append(request)
            status, headers, body = self.cdn_script[index]
            if isinstance(body, httpx.AsyncByteStream):
                return httpx.Response(status, headers=headers, stream=body)
            return httpx.Response(status, headers=headers, content=body)
        if url.startswith("http://emby.test"):
            self.emby_requests.append(request)
            if self.items_response is not None and "/items/" in path:
                status, headers, payload = self.items_response
                return httpx.Response(status, headers=headers, json=payload)
            return httpx.Response(200, content=self.emby_body)
        raise AssertionError(f"unexpected upstream request: {request.method} {url}")


@asynccontextmanager
async def _running_app(
    upstream: ScriptedUpstream,
    mode: str = "stream_proxy",
    client_ip: str = "127.0.0.1",
    app_kwargs=None,
):
    app = create_app(
        emby_host="http://emby.test",
        playback_mode=mode,
        **(app_kwargs or {}),
    )
    async with app.router.lifespan_context(app):
        app.state.strm_source_cache.put(
            ITEM_ID, {"ms1": ENTRY_URL, "ms2": ENTRY_URL_2}
        )
        await app.state.http_client_follow.aclose()
        await app.state.http_client_no_follow.aclose()
        app.state.http_client_follow = httpx.AsyncClient(
            transport=httpx.MockTransport(upstream.handler)
        )
        app.state.http_client_no_follow = httpx.AsyncClient(
            transport=httpx.MockTransport(upstream.handler)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app, client=(client_ip, 12345)
            ),
            base_url="http://proxy.test",
        ) as client:
            yield app, client


def test_resume_preserves_range_end_and_limits_output():
    first = FlakyStream([b"A" * 65536])
    upstream = ScriptedUpstream(
        cdn_script=[
            (
                206,
                {
                    "content-range": "bytes 100-70000/80000",
                    "content-length": "69901",
                },
                first,
            ),
            (
                206,
                {
                    "content-range": "bytes 65636-79999/80000",
                    "content-length": "14364",
                },
                b"B" * 14364,
            ),
        ]
    )

    async def _run():
        async with _running_app(upstream) as (_, client):
            with patch("proxy_app.sleep", new=lambda *a, **k: asyncio.sleep(0)):
                return await client.get(
                    MEDIA_URL,
                    headers={**LAN_HEADERS, "range": "bytes=100-70000"},
                )

    response = asyncio.run(_run())
    assert response.status_code == 206
    assert len(response.content) == 69901
    assert upstream.cdn_requests[1].headers["range"] == "bytes=65636-70000"


@pytest.mark.parametrize(
    ("range_header", "content_type"),
    [
        ("bytes=0-1,3-4", "multipart/byteranges; boundary=x"),
        ("bytes=-100", "video/mp4"),
    ],
)
def test_unsupported_range_stops_without_resume(range_header, content_type):
    upstream = ScriptedUpstream(
        cdn_script=[
            (
                206,
                {
                    "content-range": "bytes 0-65535/65536",
                    "content-length": "65536",
                    "content-type": content_type,
                },
                FlakyStream([b"A" * 65536]),
            )
        ]
    )

    async def _run():
        async with _running_app(upstream) as (_, client):
            with patch("proxy_app.sleep", new=lambda *a, **k: asyncio.sleep(0)):
                return await client.get(
                    MEDIA_URL, headers={**LAN_HEADERS, "range": range_header}
                )

    response = asyncio.run(_run())
    assert response.status_code == 206
    assert response.content == b"A" * 65536
    assert upstream.get_count == 1


@pytest.mark.parametrize("range_header", ["bytes=100-199", "bytes=0-1023"])
def test_final_200_for_bounded_range_forwards_full_body(range_header, monkeypatch):
    body = b"FULL" * 400
    upstream = ScriptedUpstream(
        cdn_script=[
            (200, {"content-length": str(len(body))}, body),
            (200, {"content-length": str(len(body))}, body),
        ]
    )
    monkeypatch.setattr("proxy_app.STREAM_RETRY_MAX", 1)

    async def _run():
        async with _running_app(upstream) as (_, client):
            with patch("proxy_app.sleep", new=lambda *a, **k: asyncio.sleep(0)):
                return await client.get(
                    MEDIA_URL, headers={**LAN_HEADERS, "range": range_header}
                )

    response = asyncio.run(_run())
    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-length"] == str(len(body))
    assert upstream.get_count == 2


def test_redirect_playback_info_external_path_uses_safe_upstream_headers():
    upstream = ScriptedUpstream(
        playback_sources=[
            {
                "Id": "ms1",
                "Path": ENTRY_URL,
                "IsRemote": True,
                "Protocol": "Http",
            }
        ]
    )
    headers = {
        "authorization": "Bearer secret",
        "cookie": "session=secret",
        "x-emby-token": "token-secret",
        "x-emby-authorization": 'MediaBrowser Token="secret"',
        "user-agent": "Emby/Test",
    }

    async def _run():
        async with _running_app(upstream, mode="redirect") as (_, client):
            return await client.post(
                "/Items/item1/PlaybackInfo?api_key=token",
                headers=headers,
                json={},
            )

    response = asyncio.run(_run())
    assert response.status_code == 200
    assert response.json()["MediaSources"][0]["Path"].startswith(CDN_PREFIX)
    assert len(upstream.entry_requests) == 1
    entry_headers = {
        key.lower(): value
        for key, value in upstream.entry_requests[0].headers.items()
    }
    for name in (
        "authorization",
        "cookie",
        "x-emby-token",
        "x-emby-authorization",
        "x-emby-user-id",
    ):
        assert name not in entry_headers
    emby_headers = {
        key.lower(): value for key, value in upstream.emby_requests[0].headers.items()
    }
    assert emby_headers["authorization"] == "Bearer secret"
    assert emby_headers["cookie"] == "session=secret"
    assert emby_headers["x-emby-token"] == "token-secret"


def test_explicit_media_source_miss_falls_back_to_emby():
    upstream = ScriptedUpstream(
        playback_sources=[{"Id": "ms1", "Path": ENTRY_URL}]
    )

    async def _run():
        async with _running_app(upstream) as (_, client):
            return await client.get(
                f"/videos/{ITEM_ID}/movie.mp4?MediaSourceId=missing&api_key=token",
                headers=LAN_HEADERS,
            )

    response = asyncio.run(_run())
    assert response.status_code == 200
    assert response.content == b"EMBY"
    assert upstream.head_count == 0


def test_explicit_media_source_exact_match_uses_requested_source():
    upstream = ScriptedUpstream()

    async def _run():
        async with _running_app(upstream) as (_, client):
            return await client.get(
                f"/videos/{ITEM_ID}/movie.mp4?MediaSourceId=ms2",
                headers=LAN_HEADERS,
            )

    response = asyncio.run(_run())
    assert response.status_code == 206
    assert upstream.head_count == 1
    assert upstream.entry_requests[0].url == httpx.URL(ENTRY_URL_2)


def test_media_url_cache_ttl_clips_to_expiry():
    with patch("proxy_app.time", return_value=1000):
        assert _media_url_cache_ttl("http://cdn.test/v?t=1600", 1800) == 300
        assert _media_url_cache_ttl("http://cdn.test/v?t=1200", 1800) == 0
        assert _media_url_cache_ttl("http://cdn.test/v", 1800) == 1800
        assert _media_url_cache_ttl("http://cdn.test/v?t=bad", 90) == 90


def test_playback_session_cache_separates_full_client_identity():
    upstream = ScriptedUpstream()

    async def _run():
        async with _running_app(upstream) as (app, client):
            await client.get(
                MEDIA_URL,
                headers={
                    **LAN_HEADERS,
                    "user-agent": "UA1",
                    "x-emby-device-id": "device1",
                },
            )
            await client.get(
                MEDIA_URL,
                headers={
                    **LAN_HEADERS,
                    "user-agent": "UA2",
                    "x-emby-device-id": "device1",
                },
            )
            await client.get(
                MEDIA_URL + "&api_key=token1",
                headers={**LAN_HEADERS, "user-agent": "UA1"},
            )
            await client.get(
                MEDIA_URL + "&api_key=token2",
                headers={**LAN_HEADERS, "user-agent": "UA1"},
            )
            return len(app.state.playback_session_cache._data)

    cache_entries = asyncio.run(_run())
    assert cache_entries == 4
    assert upstream.head_count == 4


def test_media_upstream_strips_emby_credentials_but_generic_proxy_keeps_them():
    upstream = ScriptedUpstream(
        playback_sources=[{"Id": "ms1", "Path": ENTRY_URL}]
    )
    headers = {
        **LAN_HEADERS,
        "authorization": "Bearer secret",
        "cookie": "session=secret",
        "x-emby-token": "token-secret",
        "x-emby-authorization": 'MediaBrowser Token="secret"',
        "user-agent": "Emby/Test",
    }

    async def _run_stream():
        async with _running_app(upstream) as (_, client):
            return await client.get(MEDIA_URL, headers=headers)

    response = asyncio.run(_run_stream())
    assert response.status_code == 206
    for request in upstream.entry_requests + upstream.cdn_requests:
        lowered = {key.lower(): value for key, value in request.headers.items()}
        for name in (
            "authorization",
            "cookie",
            "x-emby-token",
            "x-emby-authorization",
            "x-emby-user-id",
        ):
            assert name not in lowered

    generic_upstream = ScriptedUpstream()

    async def _run_generic():
        async with _running_app(generic_upstream, mode="disabled") as (_, client):
            return await client.get(MEDIA_URL, headers=headers)

    generic_response = asyncio.run(_run_generic())
    assert generic_response.status_code == 200
    generic_headers = {
        key.lower(): value
        for key, value in generic_upstream.emby_requests[-1].headers.items()
    }
    assert generic_headers["authorization"] == "Bearer secret"
    assert generic_headers["cookie"] == "session=secret"
    assert generic_headers["x-emby-token"] == "token-secret"


def test_initial_header_timeout_is_bounded(monkeypatch):
    class HangingUpstream(ScriptedUpstream):
        def __init__(self):
            super().__init__()
            self.hanging_gets = 0

        async def handler(self, request):
            if str(request.url).startswith(CDN_PREFIX) and request.method == "GET":
                self.hanging_gets += 1
                await asyncio.sleep(60)
            return await super().handler(request)

    upstream = HangingUpstream()
    monkeypatch.setattr("proxy_app.STREAM_HEADER_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("proxy_app.STREAM_RETRY_MAX", 1)

    async def _run():
        async with _running_app(upstream) as (_, client):
            with patch("proxy_app.sleep", new=lambda *a, **k: asyncio.sleep(0)):
                return await client.get(MEDIA_URL, headers=LAN_HEADERS)

    response = asyncio.run(_run())
    assert response.status_code == 502
    assert upstream.hanging_gets == 2


def test_resume_header_timeout_is_bounded(monkeypatch):
    class ResumeHangingUpstream(ScriptedUpstream):
        def __init__(self):
            super().__init__(
                cdn_script=[
                    (
                        206,
                        {
                            "content-range": "bytes 0-65535/65536",
                            "content-length": "65536",
                        },
                        FlakyStream([b"A" * 65536]),
                    )
                ]
            )
            self.hanging_gets = 0

        async def handler(self, request):
            if str(request.url).startswith(CDN_PREFIX) and request.method == "GET":
                range_header = request.headers.get("range") or ""
                if range_header and not range_header.startswith("bytes=0-"):
                    self.hanging_gets += 1
                    await asyncio.sleep(60)
            return await super().handler(request)

    upstream = ResumeHangingUpstream()
    monkeypatch.setattr("proxy_app.STREAM_HEADER_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("proxy_app.STREAM_RESUME_MAX", 2)

    async def _run():
        async with _running_app(upstream) as (_, client):
            with patch("proxy_app.sleep", new=lambda *a, **k: asyncio.sleep(0)):
                return await client.get(
                    MEDIA_URL, headers={**LAN_HEADERS, "range": "bytes=0-"}
                )

    response = asyncio.run(_run())
    assert response.status_code == 206
    assert response.content == b"A" * 65536
    assert upstream.hanging_gets == 2


def test_retryable_error_body_is_bounded_and_closed(monkeypatch):
    stream = CountingStream(count=2000)
    upstream = ScriptedUpstream(cdn_script=[(403, {}, stream)])
    monkeypatch.setattr("proxy_app.STREAM_RETRY_MAX", 0)
    monkeypatch.setattr("proxy_app.STREAM_ERROR_BODY_MAX_BYTES", 4096)

    async def _run():
        async with _running_app(upstream) as (_, client):
            return await client.get(MEDIA_URL, headers=LAN_HEADERS)

    response = asyncio.run(_run())
    assert response.status_code == 502
    assert 0 < stream.read <= 4096
    assert stream.closed is True


def test_retryable_200_is_closed_without_reading(monkeypatch):
    first = CountingStream(count=2000)
    upstream = ScriptedUpstream(
        cdn_script=[
            (200, {"content-length": "2048000"}, first),
            (
                206,
                {"content-range": "bytes 5-8/9", "content-length": "4"},
                b"PART",
            ),
        ]
    )
    monkeypatch.setattr("proxy_app.STREAM_RETRY_MAX", 1)

    async def _run():
        async with _running_app(upstream) as (_, client):
            with patch("proxy_app.sleep", new=lambda *a, **k: asyncio.sleep(0)):
                return await client.get(
                    MEDIA_URL, headers={**LAN_HEADERS, "range": "bytes=5-8"}
                )

    response = asyncio.run(_run())
    assert response.status_code == 206
    assert response.content == b"PART"
    assert first.read == 0
    assert first.closed is True


def test_error_body_read_timeout_closes_response(monkeypatch):
    stream = CountingStream(count=1, hang=True)
    upstream = ScriptedUpstream(cdn_script=[(403, {}, stream)])
    monkeypatch.setattr("proxy_app.STREAM_RETRY_MAX", 0)
    monkeypatch.setattr("proxy_app.STREAM_ERROR_BODY_TIMEOUT_SECONDS", 0.01)

    async def _run():
        async with _running_app(upstream) as (_, client):
            return await client.get(MEDIA_URL, headers=LAN_HEADERS)

    response = asyncio.run(_run())
    assert response.status_code == 502
    assert stream.closed is True


def test_304_is_passed_through_without_retry():
    upstream = ScriptedUpstream(cdn_script=[(304, {"etag": "abc"}, b"")])

    async def _run():
        async with _running_app(upstream) as (_, client):
            return await client.get(MEDIA_URL, headers=LAN_HEADERS)

    response = asyncio.run(_run())
    assert response.status_code == 304
    assert response.headers["etag"] == "abc"
    assert upstream.get_count == 1
    assert upstream.head_count == 1


def test_items_injection_strips_validators_and_sets_private_cache():
    payload = {
        "Name": "Movie",
        "MediaSources": [
            {
                "Id": "ms1",
                "Container": "mkv",
                "Type": "Video",
                "Path": "/movie.mkv",
            }
        ],
    }
    upstream = ScriptedUpstream(
        items_response=(
            200,
            {
                "content-type": "application/json",
                "etag": '"abc"',
                "last-modified": "Wed, 21 Oct 2026 07:28:00 GMT",
                "cache-control": "public, max-age=3600",
            },
            payload,
        )
    )

    async def _run():
        async with _running_app(
            upstream,
            mode="disabled",
            app_kwargs={
                "external_player_url": True,
                "external_player_list": ["potplayer"],
            },
        ) as (_, client):
            return await client.get(
                "/users/user/items/item?api_key=secret",
                headers={"user-agent": "Emby/Test"},
            )

    response = asyncio.run(_run())
    assert response.status_code == 200
    assert response.json()["ExternalUrls"]
    assert "etag" not in response.headers
    assert "last-modified" not in response.headers
    cache_control = response.headers["cache-control"]
    assert "private" in cache_control
    assert "no-store" in cache_control


def test_proxy_exception_log_redacts_query_token(caplog):
    class FailingUpstream(ScriptedUpstream):
        async def handler(self, request):
            if str(request.url).startswith("http://emby.test"):
                raise httpx.ConnectError("boom", request=request)
            return await super().handler(request)

    upstream = FailingUpstream()

    async def _run():
        async with _running_app(upstream, mode="disabled") as (_, client):
            with caplog.at_level(logging.WARNING):
                return await client.get("/items/1?api_key=supersecret")

    response = asyncio.run(_run())
    assert response.status_code == 502
    assert "supersecret" not in caplog.text
