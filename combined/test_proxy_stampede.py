"""
Emby 反向代理 302 解析缓存击穿防护集成测试

并发请求相同 item_id 时，PlaybackInfo 回源只执行一次，其余请求复用缓存结果
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from proxy_app import create_app

PICKCODE = "abc12345678901234"

PLAYBACK_COUNTS = {"calls": 0}
REDIRECT_PORTS = {"redirect": 0, "cdn": 0}


class MockEmbyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path.rstrip("/").endswith("/PlaybackInfo"):
            PLAYBACK_COUNTS["calls"] += 1
            body = {
                "MediaSources": [{
                    "Id": "ms1",
                    "Path": (
                        f"http://127.0.0.1:{REDIRECT_PORTS['redirect']}"
                        f"/redirect?pickcode={PICKCODE}"
                    ),
                    "Container": "mkv",
                }]
            }
            data = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


class MockRedirectHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(302)
        self.send_header(
            "Location", f"http://127.0.0.1:{REDIRECT_PORTS['cdn']}/file.mkv"
        )
        self.end_headers()

    def log_message(self, *args):
        pass


class MockCDNHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "5")
        self.end_headers()

    def do_GET(self):
        data = b"media"
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def _serve(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_proxy_302_stampede():
    cdn_server = _serve(MockCDNHandler)
    redirect_server = _serve(MockRedirectHandler)
    emby_server = _serve(MockEmbyHandler)
    try:
        REDIRECT_PORTS["cdn"] = cdn_server.server_port
        REDIRECT_PORTS["redirect"] = redirect_server.server_port
        emby_port = emby_server.server_port
        PLAYBACK_COUNTS["calls"] = 0

        app = create_app(
            emby_host=f"http://127.0.0.1:{emby_port}",
            redirect_mode=True,
        )

        async def _run():
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://proxy"
                ) as client:
                    responses = await asyncio.gather(*(
                        client.get(
                            "/videos/123/movie.mkv",
                            params={"MediaSourceId": "ms1"},
                            headers={"X-Emby-Token": "tok"},
                        )
                        for _ in range(10)
                    ))
                    return responses

        responses = asyncio.run(_run())
        assert all(r.status_code == 302 for r in responses), (
            [r.status_code for r in responses]
        )
        assert PLAYBACK_COUNTS["calls"] == 1, (
            f"PlaybackInfo 回源应只 1 次，实际 {PLAYBACK_COUNTS['calls']} 次"
        )
    finally:
        cdn_server.shutdown()
        redirect_server.shutdown()
        emby_server.shutdown()
