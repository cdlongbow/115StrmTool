"""
局域网流代理模式（stream-proxy-mode）测试

覆盖播放模式三态分发、Range/响应头白名单转发、直链过期换新链自愈、
半身后断的同一响应内续传、私有网段硬门禁、并发解析击穿防护与配置/API 兼容迁移；
上游 Emby/115 全部经 httpx.MockTransport 模拟，无真实网络
"""
import asyncio
from unittest.mock import MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError

from proxy_app import create_app

ENTRY_URL = "http://entry.test/redirect_url?pickcode=" + "1" * 17
CDN_PREFIX = "http://cdn.test/"
ITEM_ID = "item1"
MEDIA_URL = f"/videos/{ITEM_ID}/movie.mp4?MediaSourceId=ms1"
LAN_HEADERS = {"x-forwarded-for": "192.168.1.50"}


class FlakyStream(httpx.AsyncByteStream):
    """先吐出给定字节再抛错的假上游流，用于模拟半身后断"""

    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")

    async def aclose(self):
        self.closed = True


class FakeUpstream:
    """按脚本应答的假上游：entry HEAD 出 302，CDN GET/HEAD 按脚本序列返回"""

    def __init__(self, cdn_script=None, passthrough_emby=False):
        self.cdn_script = cdn_script or [
            (206, {"content-range": "bytes 0-3/4", "accept-ranges": "bytes",
                   "content-length": "4"}, b"BODY")
        ]
        self.head_count = 0
        self.get_count = 0
        self.last_range = None
        self.ranges = []
        self.last_headers = None
        self.passthrough_emby = passthrough_emby

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if self.passthrough_emby and url.startswith("http://emby.test"):
            return httpx.Response(200, content=b"EMBY")
        if url.startswith(ENTRY_URL) and request.method == "HEAD":
            self.head_count += 1
            return httpx.Response(
                302, headers={"location": f"{CDN_PREFIX}f{self.head_count}"}
            )
        if url.startswith(CDN_PREFIX) and request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": "4"})
        if url.startswith(CDN_PREFIX) and request.method == "GET":
            idx = min(self.get_count, len(self.cdn_script) - 1)
            self.get_count += 1
            self.last_range = request.headers.get("range")
            self.ranges.append(self.last_range)
            self.last_headers = dict(request.headers.multi_items())
            status, headers, body = self.cdn_script[idx]
            if isinstance(body, httpx.AsyncByteStream):
                return httpx.Response(status, headers=headers, stream=body)
            return httpx.Response(status, headers=headers, content=body)
        raise AssertionError(f"unexpected upstream request: {request.method} {url}")


def _make_client(app: object, upstream: FakeUpstream) -> httpx.AsyncClient:
    app.state.http_client_no_follow = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream.handler)
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://internal.test"
    )


def _seed_strm_cache(app: object) -> None:
    app.state.strm_source_cache.put(ITEM_ID, {"ms1": ENTRY_URL})


async def _with_lifespan(mode: str, upstream: FakeUpstream) -> httpx.AsyncClient:
    app = create_app(emby_host="http://emby.test", playback_mode=mode)
    await app.router.lifespan_context(app).__aenter__()
    _seed_strm_cache(app)
    return _make_client(app, upstream)


def _proxy_request(headers=None, script=None, method="get"):
    upstream = FakeUpstream(cdn_script=script)
    hdrs = dict(LAN_HEADERS)
    if headers:
        hdrs.update(headers)

    async def _run():
        client = await _with_lifespan("stream_proxy", upstream)
        async with client:
            resp = await getattr(client, method)(MEDIA_URL, headers=hdrs)
            return resp, upstream

    return asyncio.run(_run())


class TestStreamProxyForwarding:
    def test_range_206_forwarded_with_header_whitelist(self):
        script = [(206, {
            "content-range": "bytes 100-199/1000",
            "content-length": "100",
            "accept-ranges": "bytes",
            "content-type": "video/mp4",
            "etag": "abc",
            "last-modified": "somewhere",
            "content-disposition": "attachment; filename=x.mp4",
            "cache-control": "max-age=60",
            "set-cookie": "sid=secret",
            "expires": "never",
        }, b"X" * 8)]
        resp, upstream = _proxy_request(
            headers={"range": "bytes=100-199"}, script=script
        )
        assert resp.status_code == 206
        assert resp.content == b"X" * 8
        assert upstream.last_range == "bytes=100-199"
        lowered = {k.lower() for k in resp.headers}
        assert {"content-range", "content-length", "accept-ranges", "content-type"} <= lowered
        for banned in ("etag", "last-modified", "content-disposition",
                       "cache-control", "set-cookie", "expires"):
            assert banned not in lowered

    def test_get_without_range_forwards_full_stream(self):
        script = [(200, {
            "content-length": "4", "content-type": "video/mp4",
        }, b"BODY")]
        resp, upstream = _proxy_request(script=script)
        assert resp.status_code == 200
        assert resp.content == b"BODY"
        assert upstream.get_count == 1
        assert upstream.last_range is None

    def test_head_returns_headers_without_body(self):
        resp, upstream = _proxy_request(method="head")
        assert resp.status_code == 200
        assert resp.content == b""
        assert upstream.get_count == 0

    def test_no_115_address_leaks_into_response(self):
        resp, _ = _proxy_request()
        leaked = [
            v for k, v in resp.headers.items()
            if "115" in str(k).lower() or "115" in str(v).lower()
        ] + [v for v in resp.headers.values() if "entry.test" in v]
        assert leaked == []
        assert b"115" not in resp.content


class TestStreamProxySelfHealing:
    def test_expired_link_retry_refetches_fresh_direct_url(self):
        with patch("proxy_app.sleep", new=lambda *_a, **_k: asyncio.sleep(0)):
            script = [
                (403, {}, b"expired"),
                (206, {"content-range": "bytes 0-3/4", "content-length": "4"}, b"BODY"),
            ]
            resp, upstream = _proxy_request(
                headers={"range": "bytes=0-3"}, script=script
            )
        assert resp.status_code == 206
        assert resp.content == b"BODY"
        assert upstream.get_count == 2
        assert upstream.head_count == 2

    def test_retry_exhausted_returns_502(self):
        with patch("proxy_app.sleep", new=lambda *_a, **_k: asyncio.sleep(0)):
            resp, upstream = _proxy_request(
                script=[(403, {}, b"")], headers={"range": "bytes=0-9"}
            )
        assert resp.status_code == 502
        assert upstream.get_count == 3
        assert upstream.head_count == 3

    def test_ignored_range_full_200_triggers_retry(self):
        with patch("proxy_app.sleep", new=lambda *_a, **_k: asyncio.sleep(0)):
            script = [
                (200, {"content-length": "4"}, b"FULL"),
                (206, {"content-range": "bytes 5-8/9", "content-length": "4"}, b"PART"),
            ]
            resp, upstream = _proxy_request(
                headers={"range": "bytes=5-8"}, script=script
            )
        assert resp.status_code == 206
        assert resp.content == b"PART"

    def test_range_from_zero_full_200_forwarded_without_retry(self):
        with patch("proxy_app.sleep", new=lambda *_a, **_k: asyncio.sleep(0)):
            script = [
                (200, {"content-length": "4"}, b"FULL"),
            ]
            resp, upstream = _proxy_request(
                headers={"range": "bytes=0-"}, script=script
            )
        assert resp.status_code == 200
        assert resp.content == b"FULL"
        assert upstream.get_count == 1
        assert upstream.head_count == 1

    def test_ignored_range_persistent_200_forwarded_after_retries(self):
        with patch("proxy_app.sleep", new=lambda *_a, **_k: asyncio.sleep(0)):
            script = [
                (200, {"content-length": "4"}, b"FULL"),
                (200, {"content-length": "4"}, b"FULL"),
                (200, {"content-length": "4"}, b"FULL"),
            ]
            resp, upstream = _proxy_request(
                headers={"range": "bytes=5-8"}, script=script
            )
        assert resp.status_code == 200
        assert resp.content == b"FULL"
        assert upstream.get_count == 3
        assert upstream.head_count == 3


class TestStreamProxyMidStreamResume:
    """响应头已产出后的半身后断：同一响应内换新链按已交付字节续传"""

    # 转发按 64KB 成块交付，凑不满一块的尾字节不会到达播放端，
    # 断点偏移以已交付计（多退少补，重取窗口最多一块）
    CHUNK = b"A" * 65536
    LOST = b"BCD"
    TAIL = b"BCDE"
    FILE_SIZE = 65536 + 4
    FIRST_HEADERS = {
        "content-range": f"bytes 0-{FILE_SIZE - 1}/{FILE_SIZE}",
        "content-length": str(FILE_SIZE),
    }
    RESUME_RESPONSE = (
        206,
        {
            "content-range": f"bytes 65536-{FILE_SIZE - 1}/{FILE_SIZE}",
            "content-length": "4",
        },
        TAIL,
    )

    def _flaky(self):
        return FlakyStream([self.CHUNK, self.LOST])

    def test_break_resumes_from_delivered_offset(self):
        with patch("proxy_app.sleep", new=lambda *_a, **_k: asyncio.sleep(0)):
            script = [(206, self.FIRST_HEADERS, self._flaky()), self.RESUME_RESPONSE]
            resp, upstream = _proxy_request(
                headers={"range": "bytes=0-"}, script=script
            )
        assert resp.status_code == 206
        assert resp.content == self.CHUNK + self.TAIL
        assert upstream.ranges == ["bytes=0-", "bytes=65536-"]
        assert upstream.get_count == 2
        assert upstream.head_count == 2

    def test_resume_replaces_range_and_drops_conditionals(self):
        with patch("proxy_app.sleep", new=lambda *_a, **_k: asyncio.sleep(0)):
            script = [(206, self.FIRST_HEADERS, self._flaky()), self.RESUME_RESPONSE]
            resp, upstream = _proxy_request(
                headers={
                    "range": "bytes=0-",
                    "if-range": "etag-value",
                    "if-none-match": 'W/"x"',
                },
                script=script,
            )
        assert resp.content == self.CHUNK + self.TAIL
        lowered = {k.lower(): v for k, v in upstream.last_headers.items()}
        assert lowered["range"] == "bytes=65536-"
        assert [k for k in lowered if k.startswith("if-")] == []

    def test_full_get_break_resumes_with_open_ended_range(self):
        with patch("proxy_app.sleep", new=lambda *_a, **_k: asyncio.sleep(0)):
            script = [
                (200, {"content-length": str(self.FILE_SIZE)}, self._flaky()),
                self.RESUME_RESPONSE,
            ]
            resp, upstream = _proxy_request(script=script)
        assert resp.status_code == 200
        assert resp.content == self.CHUNK + self.TAIL
        assert upstream.ranges == [None, "bytes=65536-"]
        assert upstream.get_count == 2
        assert upstream.head_count == 2

    def test_expired_resume_link_re_resolved_until_success(self):
        with patch("proxy_app.sleep", new=lambda *_a, **_k: asyncio.sleep(0)):
            script = [
                (206, self.FIRST_HEADERS, self._flaky()),
                (403, {}, b"expired"),
                self.RESUME_RESPONSE,
            ]
            resp, upstream = _proxy_request(
                headers={"range": "bytes=0-"}, script=script
            )
        assert resp.content == self.CHUNK + self.TAIL
        assert upstream.ranges == ["bytes=0-", "bytes=65536-", "bytes=65536-"]
        assert upstream.get_count == 3
        assert upstream.head_count == 3

    def test_whole_file_200_on_resume_ends_body_without_replay(self):
        with patch("proxy_app.sleep", new=lambda *_a, **_k: asyncio.sleep(0)):
            script = [
                (206, self.FIRST_HEADERS, self._flaky()),
                (200, {"content-length": str(self.FILE_SIZE)}, b"FULL"),
            ]
            resp, upstream = _proxy_request(
                headers={"range": "bytes=0-"}, script=script
            )
        assert resp.content == self.CHUNK
        assert upstream.ranges == ["bytes=0-", "bytes=65536-"]
        assert upstream.get_count == 2
        assert upstream.head_count == 2

    def test_misaligned_206_on_resume_ends_body(self):
        with patch("proxy_app.sleep", new=lambda *_a, **_k: asyncio.sleep(0)):
            script = [
                (206, self.FIRST_HEADERS, self._flaky()),
                (206, self.FIRST_HEADERS, b"X" * 4),
            ]
            resp, upstream = _proxy_request(
                headers={"range": "bytes=0-"}, script=script
            )
        assert resp.content == self.CHUNK
        assert upstream.get_count == 2
        assert upstream.head_count == 2

    def test_resume_attempts_exhausted_ends_body(self):
        with patch("proxy_app.sleep", new=lambda *_a, **_k: asyncio.sleep(0)):
            script = [
                (206, self.FIRST_HEADERS, self._flaky()),
                (403, {}, b"expired"),
            ]
            resp, upstream = _proxy_request(
                headers={"range": "bytes=0-"}, script=script
            )
        assert resp.content == self.CHUNK
        assert upstream.get_count == 4
        assert upstream.head_count == 4

    def test_resume_total_cap_stops_pathological_reconnects(self, monkeypatch):
        monkeypatch.setattr("proxy_app.STREAM_RESUME_TOTAL_MAX", 2)
        counts = {"head": 0, "get": 0}

        class AlwaysFlaky:
            """每次应答都在交付一块后断流的 CDN，用于触发续传总次数上限"""

            def handler(self, request: httpx.Request) -> httpx.Response:
                url = str(request.url)
                if url.startswith(ENTRY_URL) and request.method == "HEAD":
                    counts["head"] += 1
                    return httpx.Response(
                        302, headers={"location": f"{CDN_PREFIX}f{counts['head']}"}
                    )
                range_header = request.headers.get("range") or ""
                start = (
                    range_header[len("bytes="):].split("-", 1)[0]
                    if range_header.startswith("bytes=") else "0"
                )
                counts["get"] += 1
                return httpx.Response(
                    206,
                    headers={
                        "content-range": f"bytes {start}-999999/1000000",
                        "content-length": str(1000000 - int(start)),
                    },
                    stream=FlakyStream([b"A" * 65536]),
                )

        upstream = AlwaysFlaky()

        async def _run():
            client = await _with_lifespan("stream_proxy", upstream)
            async with client:
                return await client.get(
                    MEDIA_URL, headers={**LAN_HEADERS, "range": "bytes=0-"}
                )

        with patch("proxy_app.sleep", new=lambda *_a, **_k: asyncio.sleep(0)):
            resp = asyncio.run(_run())
        assert counts["get"] == 3
        assert resp.content == b"A" * (65536 * 3)


class TestStreamProxyNetworkGate:

    def test_public_source_rejected(self):
        resp, upstream = _proxy_request(headers={"x-forwarded-for": "8.8.8.8"})
        assert resp.status_code == 403
        assert upstream.head_count == 0 and upstream.get_count == 0

    def test_private_xff_first_hop_allowed(self):
        resp, upstream = _proxy_request(
            headers={"x-forwarded-for": "192.168.1.50, 8.8.8.8"}
        )
        assert resp.status_code == 206
        assert upstream.get_count == 1


class TestPlaybackModeRegression:
    def test_redirect_mode_still_returns_302(self):
        upstream = FakeUpstream()

        async def _run():
            client = await _with_lifespan("redirect", upstream)
            async with client:
                return await client.get(MEDIA_URL)

        resp = asyncio.run(_run())
        assert resp.status_code == 302
        assert resp.headers["location"] == f"{CDN_PREFIX}f1"

    def test_disabled_mode_passes_through_to_emby(self):
        upstream = FakeUpstream(passthrough_emby=True)

        async def _run():
            client = await _with_lifespan("disabled", upstream)
            async with client:
                return await client.get(MEDIA_URL)

        resp = asyncio.run(_run())
        assert resp.status_code == 200
        assert resp.content == b"EMBY"


class TestStreamProxyStampede:
    def test_concurrent_streams_parse_final_url_once(self):
        upstream = FakeUpstream()

        async def _run():
            client = await _with_lifespan("stream_proxy", upstream)
            async with client:
                responses = await asyncio.gather(*(
                    client.get(MEDIA_URL, headers={**LAN_HEADERS, "range": f"bytes={i}-3"})
                    for i in range(5)
                ))
                return responses, upstream

        responses, upstream = asyncio.run(_run())
        assert all(r.status_code == 206 for r in responses)
        assert upstream.head_count == 1
        assert upstream.get_count == 5


class TestConfigPlaybackModeCompat:
    def _migrate(self, emby_dict):
        from config_manager import _migrate_legacy_emby_fields

        raw = {"emby": emby_dict}
        _migrate_legacy_emby_fields(raw)
        return raw["emby"]

    def test_legacy_true_maps_to_redirect(self):
        out = self._migrate({"redirect_mode": True})
        assert out["playback_mode"] == "redirect"
        assert "redirect_mode" not in out

    def test_legacy_false_maps_to_disabled(self):
        out = self._migrate({"redirect_mode": False})
        assert out["playback_mode"] == "disabled"
        assert "redirect_mode" not in out

    def test_new_field_not_overridden(self):
        out = self._migrate({"redirect_mode": True, "playback_mode": "stream_proxy"})
        assert out["playback_mode"] == "stream_proxy"

    def test_emby_config_invalid_value_falls_back(self):
        from config_manager import EmbyConfig, PlaybackMode

        assert EmbyConfig(playback_mode="bogus").playback_mode is PlaybackMode.DISABLED

    def test_emby_config_accepts_valid_values(self):
        from config_manager import EmbyConfig, PlaybackMode

        assert EmbyConfig(playback_mode="stream_proxy").playback_mode is PlaybackMode.STREAM_PROXY


class TestAdminApiPlaybackModeCompat:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self._restart_count = 0

        def _cb():
            self._restart_count += 1

        from admin_api import set_emby_restart_callback

        set_emby_restart_callback(_cb)
        yield
        set_emby_restart_callback(None)

    def _patch_cm(self):
        _cm = MagicMock()
        _cm.get.return_value = {"emby": {}}
        patcher = patch("admin_api.config_manager", _cm)
        patcher.start()
        self._cm = _cm
        self.addcleanup = patcher.stop

    def test_legacy_bool_maps_to_playback_mode(self):
        from admin_api import EmbyConfigRequest, update_emby_config

        self._patch_cm()
        try:
            update_emby_config(EmbyConfigRequest(redirect_mode=True))
        finally:
            self.addcleanup()
        sent = self._cm.update.call_args[0][0]
        assert sent["emby"]["playback_mode"] == "redirect"
        assert "redirect_mode" not in sent["emby"]
        assert self._restart_count == 1

    def test_invalid_playback_mode_rejected(self):
        from admin_api import EmbyConfigRequest

        with pytest.raises(ValidationError):
            EmbyConfigRequest(playback_mode="teleport")

    def test_new_field_passes_through_and_restarts(self):
        from admin_api import EmbyConfigRequest, update_emby_config

        self._patch_cm()
        try:
            update_emby_config(EmbyConfigRequest(playback_mode="stream_proxy"))
        finally:
            self.addcleanup()
        sent = self._cm.update.call_args[0][0]
        assert sent["emby"]["playback_mode"] == "stream_proxy"
        assert self._restart_count == 1
class TestStreamProxyReResolveGuard:
    def test_resolve_raises_recovers(self, monkeypatch):
        upstream = FakeUpstream(
            cdn_script=[
                (403, {}, b"expired"),
                (206, {"content-length": "4"}, b"BODY"),
            ]
        )

        hits = {"n": 0}

        async def _run():
            app = create_app(emby_host="http://emby.test", playback_mode="stream_proxy")
            async with app.router.lifespan_context(app):
                _seed_strm_cache(app)
                client = _make_client(app, upstream)
                real_get = app.state.strm_source_cache.get

                def flaky_get(key):
                    hits["n"] += 1
                    if hits["n"] == 2:
                        raise RuntimeError("boom")
                    return real_get(key)

                app.state.strm_source_cache.get = flaky_get
                async with client:
                    return await client.get(
                        MEDIA_URL,
                        headers={**LAN_HEADERS, "range": "bytes=0-3"},
                    )

        with patch("proxy_app.sleep", new=asyncio.sleep):
            resp = asyncio.run(_run())

        assert resp.status_code == 206
        assert resp.content == b"BODY"
        assert hits["n"] == 3

    def test_budget_exceeded_after_resolve_still_502(self, monkeypatch):
        upstream = FakeUpstream(
            cdn_script=[
                (403, {}, b"expired"),
                (206, {"content-length": "4"}, b"BODY"),
            ]
        )

        async def _run():
            app = create_app(emby_host="http://emby.test", playback_mode="stream_proxy")
            async with app.router.lifespan_context(app):
                _seed_strm_cache(app)
                client = _make_client(app, upstream)
                async with client:
                    return await client.get(
                        MEDIA_URL,
                        headers={**LAN_HEADERS, "range": "bytes=0-3"},
                    )

        ticks = iter([0.0, 0.1, 11.0, 11.0, 11.0, 11.0])

        def fake_monotonic():
            return next(ticks, 11.0)

        monkeypatch.setattr("proxy_app.monotonic", fake_monotonic)

        with patch("proxy_app.sleep", new=asyncio.sleep):
            resp = asyncio.run(_run())

        assert resp.status_code == 502
        assert upstream.get_count == 1
        assert upstream.head_count == 2
