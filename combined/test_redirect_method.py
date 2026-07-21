"""
测试 HEAD vs GET 请求方式对 CDN 直链结果的影响

模拟场景：
1. CDN 不支持 HEAD（返回 405）
2. CDN 根据请求方法返回不同的 URL（方法感知签名）
3. 重定向链中 HEAD 跟随到仅支持 GET 的 CDN
4. 正常 CDN（HEAD 和 GET 行为一致）
"""
import asyncio
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import httpx
import requests
import pytest


class MockCDNHandler(BaseHTTPRequestHandler):
    """模拟 CDN 服务器"""

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        scenario = params.get("scenario", ["normal"])[0]

        if scenario == "reject_head":
            self._respond_200()

        elif scenario == "method_sign_url":
            self._respond_with_method_url("GET")

        elif scenario == "redirect_to_cdn":
            final = params.get("final_scenario", ["normal"])[0]
            self.send_response(302)
            self.send_header("Location", f"/cdn_final?scenario={final}")
            self.end_headers()

        elif scenario in ("cdn_final", "normal"):
            self._respond_200()

        elif scenario == "get_only_cdn":
            self._respond_200()

        else:
            self._respond_200()

    def do_HEAD(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        scenario = params.get("scenario", ["normal"])[0]

        if scenario == "reject_head":
            self.send_response(405)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()

        elif scenario == "method_sign_url":
            self._respond_with_method_url("HEAD")

        elif scenario == "redirect_to_cdn":
            final = params.get("final_scenario", ["normal"])[0]
            self.send_response(302)
            self.send_header("Location", f"/cdn_final?scenario={final}")
            self.end_headers()

        elif scenario == "cdn_final":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

        elif scenario == "get_only_cdn":
            self.send_response(405)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()

        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

    def _respond_200(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok"}).encode())

    def _respond_with_method_url(self, method):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        body = json.dumps({
            "url": f"http://cdn.example.com/file?method={method}",
            "method": method,
        }).encode()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@pytest.fixture(scope="module")
def mock_server():
    server = HTTPServer(("127.0.0.1", 0), MockCDNHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()


class TestHeadVsGetMethod:

    def test_cdn_rejects_head(self, mock_server):
        """
        CDN 对 HEAD 返回 405，对 GET 返回 200

        _resolve_redirect 用 HEAD 请求得到 405，
        但 httpx 仍返回最终 URL（resp.url）。
        客户端后续用 GET Range 请求同一 URL，CDN 可能拒绝。
        """
        base = f"http://127.0.0.1:{mock_server}"
        url = f"{base}/file?scenario=reject_head"

        # 模拟 _resolve_redirect 的 HEAD 方式
        s = requests.Session()
        resp_head = s.send(requests.Request("HEAD", url).prepare(), allow_redirects=True)
        resp_get = s.send(requests.Request("GET", url).prepare(), allow_redirects=True)

        print(f"\n[场景 1] HEAD: status={resp_head.status_code}, url={resp_head.url}")
        print(f"[场景 1] GET:  status={resp_get.status_code}, url={resp_get.url}")

        assert resp_head.status_code == 405
        assert resp_get.status_code == 200
        assert str(resp_head.url) == str(resp_get.url)
        print("[结论] HEAD 405 但 URL 相同，客户端后续 GET Range 请求可能失败")

    def test_cdn_method_dependent_url(self, mock_server):
        """
        CDN 根据请求方法返回不同的 URL（方法感知签名）

        HEAD 拿到 token_for_HEAD，GET 拿到 token_for_GET。
        _resolve_redirect 用 HEAD 拿到 token_for_HEAD 的 URL，
        客户端用 GET + Range 请求该 URL → token 不匹配 → 403/416
        """
        base = f"http://127.0.0.1:{mock_server}"
        url = f"{base}/file?scenario=method_sign_url"

        s = requests.Session()
        resp_head = s.send(requests.Request("HEAD", url).prepare(), allow_redirects=True)
        resp_get = s.send(requests.Request("GET", url).prepare(), allow_redirects=True)

        # 注意: requests 库对 HEAD 响应自动丢弃 body（RFC 7231 允许）
        # 但 httpx 的 behavior 不同，可能保留 body
        head_body = resp_head.text
        get_data = resp_get.json()

        print(f"\n[场景 2] HEAD 响应体: '{head_body}'（requests 丢弃 HEAD body）")
        print(f"[场景 2] GET 返回 URL:  {get_data['url']}")

        assert get_data["method"] == "GET"
        print("[结论] HEAD 无法获取响应体内容 → 若 CDN 在响应体中返回签名，HEAD 拿不到")

    def test_redirect_chain_head_to_get_only_cdn(self, mock_server):
        """
        重定向链: STRM URL → 302 → CDN（仅支持 GET）

        HEAD 跟随重定向到 CDN 后得到 405，
        GET 正常得到 200。
        """
        base = f"http://127.0.0.1:{mock_server}"
        url = f"{base}/redirect?scenario=redirect_to_cdn&final_scenario=get_only_cdn"

        s = requests.Session()
        resp_head = s.send(requests.Request("HEAD", url).prepare(), allow_redirects=True)
        resp_get = s.send(requests.Request("GET", url).prepare(), allow_redirects=True)

        print(f"\n[场景 3 重定向链]")
        print(f"  HEAD: status={resp_head.status_code}, final_url={resp_head.url}")
        print(f"  GET:  status={resp_get.status_code}, final_url={resp_get.url}")

        assert resp_head.status_code == 405
        assert resp_get.status_code == 200
        print("[结论] HEAD 跟随重定向到 CDN 但被拒绝（405），URL 对 GET 无效")

    def test_normal_cdn(self, mock_server):
        """
        正常 CDN: HEAD 和 GET 都返回 200，URL 一致

        基准测试
        """
        base = f"http://127.0.0.1:{mock_server}"
        url = f"{base}/file?scenario=normal"

        s = requests.Session()
        resp_head = s.send(requests.Request("HEAD", url).prepare(), allow_redirects=True)
        resp_get = s.send(requests.Request("GET", url).prepare(), allow_redirects=True)

        print(f"\n[场景 4 正常 CDN]")
        print(f"  HEAD: status={resp_head.status_code}, url={resp_head.url}")
        print(f"  GET:  status={resp_get.status_code}, url={resp_get.url}")

        assert resp_head.status_code == 200
        assert resp_get.status_code == 200
        assert str(resp_head.url) == str(resp_get.url)
        print("[结论] 正常 CDN 下 HEAD 和 GET 行为一致，无问题")

    def test_redirect_chain_normal_cdn(self, mock_server):
        """
        完整重定向链 + 正常 CDN（HEAD 和 GET 都支持）
        """
        base = f"http://127.0.0.1:{mock_server}"
        url = f"{base}/redirect?scenario=redirect_to_cdn&final_scenario=normal"

        s = requests.Session()
        resp_head = s.send(requests.Request("HEAD", url).prepare(), allow_redirects=True)
        resp_get = s.send(requests.Request("GET", url).prepare(), allow_redirects=True)

        print(f"\n[场景 5 重定向链 + 正常 CDN]")
        print(f"  HEAD: status={resp_head.status_code}, url={resp_head.url}")
        print(f"  GET:  status={resp_get.status_code}, url={resp_get.url}")

        assert resp_head.status_code == 200
        assert resp_get.status_code == 200
        assert str(resp_head.url) == str(resp_get.url)
        print("[结论] 正常场景下 HEAD 和 GET 结果一致")


class TestResolveRedirectBehavior:

    def test_head_resolve_returns_405_url(self, mock_server):
        """
        _resolve_redirect 用 HEAD 解析重定向链时，
        若 CDN 返回 405，函数仍返回 CDN URL，
        但该 URL 对客户端 GET 请求可能无效。
        """
        base = f"http://127.0.0.1:{mock_server}"
        url = f"{base}/redirect?scenario=redirect_to_cdn&final_scenario=get_only_cdn"

        s = requests.Session()

        # 模拟 _resolve_redirect 的 HEAD 方式
        resp = s.send(requests.Request("HEAD", url).prepare(), allow_redirects=True)
        resolved_url = str(resp.url)

        print(f"\n[HEAD 解析] 解析结果: {resolved_url}")
        print(f"[HEAD 解析] 状态码: {resp.status_code}")

        # 用 GET 请求解析到的 URL
        resp_get = s.get(resolved_url)
        print(f"[HEAD 解析] 用 GET 请求该 URL: status={resp_get.status_code}")

        assert resp.status_code == 405
        assert resp_get.status_code == 200, "GET 请求本应成功，但 HEAD 解析的 URL 可能无效"
        print("[结论] HEAD 解析出的 URL 对后续 GET 请求可能无效（CDN 拒绝 HEAD 时）")

    def test_get_resolve_returns_200_url(self, mock_server):
        """
        用 GET 解析重定向链，CDN 返回 200，
        后续客户端 GET Range 请求使用同一 URL 无问题。
        """
        base = f"http://127.0.0.1:{mock_server}"
        url = f"{base}/redirect?scenario=redirect_to_cdn&final_scenario=normal"

        s = requests.Session()

        # 用 GET 方式
        resp = s.send(requests.Request("GET", url).prepare(), allow_redirects=True)
        resolved_url = str(resp.url)

        print(f"\n[GET 解析] 解析结果: {resolved_url}")
        print(f"[GET 解析] 状态码: {resp.status_code}")

        assert resp.status_code == 200
        print("[结论] GET 解析出的 URL 对后续客户端 GET Range 请求有效")


@pytest.mark.asyncio
class TestHttpxHeadVsGet:
    """
    使用 httpx（与生产代码相同的库）测试 HEAD vs GET

    重点验证 httpx 对 HEAD 响应体的处理方式（与 requests 库不同）
    """

    async def test_httpx_head_discards_body(self, mock_server):
        """
        httpx 对 HEAD 响应是否丢弃 body？

        若 httpx 也丢弃 body，则 _resolve_redirect 用 HEAD 无法获取
        CDN 在响应体中返回的任何信息（如签名 token）
        """
        base = f"http://127.0.0.1:{mock_server}"
        url = f"{base}/file?scenario=method_sign_url"

        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp_head = await client.head(url)
            resp_get = await client.get(url)

            head_text = resp_head.text
            get_data = resp_get.json()

            print(f"\n[httpx] HEAD 响应体: '{head_text}'")
            print(f"[httpx] GET 返回 URL: {get_data['url']}")

            # 验证 httpx 是否保留 HEAD 响应体
            print(f"[httpx] HEAD 是否有 body: {bool(head_text)}")
            print(f"[httpx] HEAD content-length: {resp_head.headers.get('content-length', 'N/A')}")

    async def test_httpx_redirect_chain_head_vs_get(self, mock_server):
        """
        使用 httpx 模拟 _resolve_redirect 的完整流程

        httpx 的 follow_redirects=True 时，HEAD 和 GET 在重定向链中的行为差异
        """
        base = f"http://127.0.0.1:{mock_server}"

        async with httpx.AsyncClient(follow_redirects=True) as client:
            # 模拟 _resolve_redirect 的 HEAD 方式
            url = f"{base}/redirect?scenario=redirect_to_cdn&final_scenario=get_only_cdn"
            resp_head = await client.head(url)
            head_url = str(resp_head.url)

            # 用 GET 请求同一 URL
            resp_get = await client.get(url)
            get_url = str(resp_get.url)

            print(f"\n[httpx 重定向链]")
            print(f"  HEAD: status={resp_head.status_code}, url={head_url}")
            print(f"  GET:  status={resp_get.status_code}, url={get_url}")

            assert head_url == get_url, "HEAD 和 GET 跟随重定向到相同最终 URL"
            assert resp_head.status_code == 405
            assert resp_get.status_code == 200
            print("[httpx 结论] HEAD 跟随重定向后 CDN 拒绝，GET 正常")

    async def test_get_resolve_redirect_alternative(self, mock_server):
        """
        用 GET + allow_redirects=False 手动解析 Location 头

        这是另一个方案：不实际请求 CDN，只解析重定向链中的 Location 头
        """
        from urllib.parse import urljoin

        base = f"http://127.0.0.1:{mock_server}"

        async with httpx.AsyncClient(follow_redirects=False) as client:
            url = f"{base}/redirect?scenario=redirect_to_cdn&final_scenario=normal"
            seen_urls = []
            current_url = url

            for _ in range(5):
                resp = await client.get(current_url)
                seen_urls.append((resp.status_code, current_url))
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location", "")
                    if not location:
                        break
                    current_url = urljoin(current_url, location)
                else:
                    break

            final_url = current_url
            print(f"\n[手动解析重定向链]")
            for status, u in seen_urls:
                print(f"  {status} -> {u}")
            print(f"  [最终] {final_url}")

            assert "cdn_final" in final_url
            print("[结论] 手动解析 Location 头可避免实际请求 CDN，且不依赖 HTTP 方法")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])