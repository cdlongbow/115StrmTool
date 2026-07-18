"""
Proxy App 模块级函数测试：核心逻辑单元测试
"""
import json
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest


class TestApplyPinRules:

    def test_no_rules(self):
        from proxy_app import _apply_pin_rules
        assert _apply_pin_rules("/movies/test.mp4", []) == "/movies/test.mp4"

    def test_no_match(self):
        from proxy_app import _apply_pin_rules
        rules = [("/tv", "http://192.168.1.1:8096/tv")]
        result = _apply_pin_rules("/movies/test.mp4", rules)
        assert result == "/movies/test.mp4"

    def test_match_prefix(self):
        from proxy_app import _apply_pin_rules
        rules = [("/movies", "http://192.168.1.1:8096/movies")]
        result = _apply_pin_rules("/movies/test.mp4", rules)
        assert result == "http://192.168.1.1:8096/movies/test.mp4"

    def test_match_with_query(self):
        from proxy_app import _apply_pin_rules
        rules = [("/movies", "http://192.168.1.1:8096/movies")]
        result = _apply_pin_rules("http://emby:8096/movies/test.mp4?token=abc", rules)
        assert result == "http://192.168.1.1:8096/movies/test.mp4?token=abc"

    def test_exact_match(self):
        from proxy_app import _apply_pin_rules
        rules = [("/movies", "http://192.168.1.1:8096/movies")]
        result = _apply_pin_rules("/movies", rules)
        assert result == "http://192.168.1.1:8096/movies"

    def test_empty_url(self):
        from proxy_app import _apply_pin_rules
        rules = [("/movies", "http://192.168.1.1:8096/movies")]
        assert _apply_pin_rules("", rules) == ""


class TestMayReturnEmbyHtmlShell:

    def test_root(self):
        from proxy_app import _may_return_emby_html_shell
        assert _may_return_emby_html_shell("/") is True
        assert _may_return_emby_html_shell("") is True

    def test_web_path(self):
        from proxy_app import _may_return_emby_html_shell
        assert _may_return_emby_html_shell("/web/index.html") is True
        assert _may_return_emby_html_shell("/web") is True

    def test_html_file(self):
        from proxy_app import _may_return_emby_html_shell
        assert _may_return_emby_html_shell("/some/page.html") is True
        assert _may_return_emby_html_shell("/page.htm") is True

    def test_playback_info(self):
        from proxy_app import _may_return_emby_html_shell
        assert _may_return_emby_html_shell("/Items/1/PlaybackInfo") is False

    def test_api_path(self):
        from proxy_app import _may_return_emby_html_shell
        assert _may_return_emby_html_shell("/emby/Items") is False
        assert _may_return_emby_html_shell("/videos/1/stream") is False
        assert _may_return_emby_html_shell("/audio/1/stream") is False
        assert _may_return_emby_html_shell("/sync/jobitems/1/file") is False

    def test_no_extension_path(self):
        from proxy_app import _may_return_emby_html_shell
        assert _may_return_emby_html_shell("/dashboard") is True
        assert _may_return_emby_html_shell("/users") is True

    def test_static_file(self):
        from proxy_app import _may_return_emby_html_shell
        assert _may_return_emby_html_shell("/css/style.css") is False
        assert _may_return_emby_html_shell("/js/app.js") is False


class TestMediaSourcesIndicateStrm:

    def test_empty_data(self):
        from proxy_app import _media_sources_indicate_strm
        assert _media_sources_indicate_strm({}) is False

    def test_no_sources(self):
        from proxy_app import _media_sources_indicate_strm
        assert _media_sources_indicate_strm({"MediaSources": []}) is False

    def test_not_strm(self):
        from proxy_app import _media_sources_indicate_strm
        data = {"MediaSources": [{"IsRemote": False, "Protocol": "File"}]}
        assert _media_sources_indicate_strm(data) is False

    def test_is_strm(self):
        from proxy_app import _media_sources_indicate_strm
        data = {"MediaSources": [{"IsRemote": True, "Protocol": "Http"}]}
        assert _media_sources_indicate_strm(data) is True

    def test_mixed_sources(self):
        from proxy_app import _media_sources_indicate_strm
        data = {
            "MediaSources": [
                {"IsRemote": False, "Protocol": "File"},
                {"IsRemote": True, "Protocol": "Http"},
            ]
        }
        assert _media_sources_indicate_strm(data) is True


class TestMediaSourcesMatchPinRules:

    def test_no_rules(self):
        from proxy_app import _media_sources_match_pin_rules
        data = {"MediaSources": [{"Path": "http://emby:8096/movies/test.mp4"}]}
        assert _media_sources_match_pin_rules(data, []) is False

    def test_no_match(self):
        from proxy_app import _media_sources_match_pin_rules
        rules = [("/tv", "http://192.168.1.1:8096/tv")]
        data = {"MediaSources": [{"Path": "http://emby:8096/movies/test.mp4"}]}
        assert _media_sources_match_pin_rules(data, rules) is False

    def test_match(self):
        from proxy_app import _media_sources_match_pin_rules
        rules = [("/movies", "http://192.168.1.1:8096/movies")]
        data = {"MediaSources": [{"Path": "http://emby:8096/movies/test.mp4"}]}
        assert _media_sources_match_pin_rules(data, rules) is True


class TestApplyForceDirectPlay:

    def test_empty_sources(self):
        from proxy_app import _apply_force_direct_play_to_media_sources
        data = {}
        _apply_force_direct_play_to_media_sources(data, "1")
        assert "MediaSources" not in data

    def test_video_source(self):
        from proxy_app import _apply_force_direct_play_to_media_sources
        data = {"MediaSources": [{"Id": "src1", "SupportsTranscoding": True}]}
        _apply_force_direct_play_to_media_sources(data, "item1")
        ms = data["MediaSources"][0]
        assert ms["SupportsDirectPlay"] is True
        assert ms["SupportsTranscoding"] is False
        assert "TranscodingUrl" not in ms
        assert "/videos/item1/stream" in ms["DirectStreamUrl"]

    def test_audio_source(self):
        from proxy_app import _apply_force_direct_play_to_media_sources
        data = {"MediaSources": [{"Id": "src1", "Type": "Audio"}]}
        _apply_force_direct_play_to_media_sources(data, "item1")
        ms = data["MediaSources"][0]
        assert "/audio/item1/stream" in ms["DirectStreamUrl"]


class TestRealClientIp:

    def make_request(self, headers=None, client_host="127.0.0.1"):
        req = MagicMock()
        req.headers = headers or {}
        req.client.host = client_host
        return req

    def test_x_forwarded_for(self):
        from proxy_app import _real_client_ip
        req = self.make_request({"x-forwarded-for": "10.0.0.1, 192.168.1.1"})
        assert _real_client_ip(req) == "10.0.0.1"

    def test_x_real_ip(self):
        from proxy_app import _real_client_ip
        req = self.make_request({"x-real-ip": "10.0.0.2"})
        assert _real_client_ip(req) == "10.0.0.2"

    def test_direct_client(self):
        from proxy_app import _real_client_ip
        req = self.make_request({}, "192.168.1.100")
        assert _real_client_ip(req) == "192.168.1.100"


class TestStripAcceptEncoding:

    def test_strips_accept_encoding(self):
        from proxy_app import _strip_accept_encoding
        headers = {"Accept-Encoding": "gzip", "Content-Type": "text/html"}
        result = _strip_accept_encoding(headers)
        assert "accept-encoding" not in result
        assert "Accept-Encoding" not in result
        assert result["Content-Type"] == "text/html"


class TestCurrentPort:

    def make_request(self, headers=None, server=("127.0.0.1", 8097)):
        req = MagicMock()
        req.headers = headers or {}
        req.scope = {"server": server}
        return req

    def test_forwarded_port(self):
        from proxy_app import _current_port
        req = self.make_request({"x-forwarded-port": "8443"})
        assert _current_port(req) == 8443

    def test_server_port(self):
        from proxy_app import _current_port
        req = self.make_request({})
        assert _current_port(req) == 8097

    def test_default_port(self):
        from proxy_app import _current_port
        req = MagicMock()
        req.headers = {}
        req.scope = {}
        assert _current_port(req) == 80


class TestBuildForwardHeaders:

    def make_request(self, headers=None):
        req = MagicMock()
        req.headers.multi_items.return_value = list((headers or {}).items())
        req.headers.items.return_value = list((headers or {}).items())
        return req

    def test_removes_hop_by_hop(self):
        from proxy_app import _build_forward_headers
        headers = {
            "Host": "emby:8096",
            "Connection": "keep-alive",
            "Content-Type": "text/html",
            "User-Agent": "Mozilla/5.0",
        }
        result = _build_forward_headers(self.make_request(headers))
        assert "Host" not in result
        assert "Connection" not in result
        assert result["Content-Type"] == "text/html"
        assert result["User-Agent"] == "Mozilla/5.0"


class TestInjectScriptsIntoHtml:

    def test_no_injection_needed(self):
        from proxy_app import _inject_scripts_into_html
        html = "<html><head></head><body></body></html>"
        result = _inject_scripts_into_html(html)
        assert result is not None
        assert "[EmbyReverseProxy] crossOrigin" in result

    def test_inject_external_player(self):
        from proxy_app import _inject_scripts_into_html
        html = "<html><head></head><body></body></html>"
        player_script = '<script>alert("player")</script>'
        result = _inject_scripts_into_html(html, player_script)
        assert "alert" in result

    def test_already_injected(self):
        from proxy_app import _inject_scripts_into_html, CROSS_ORIGIN_INTERCEPT_MARKER
        html = f"<html><head>{CROSS_ORIGIN_INTERCEPT_MARKER}</head><body></body></html>"
        result = _inject_scripts_into_html(html)
        assert result is None

    def test_no_head_tag(self):
        from proxy_app import _inject_scripts_into_html
        html = "<html><body>no head</body></html>"
        result = _inject_scripts_into_html(html)
        assert result is None


class TestHeaderHash:

    def make_request(self, headers=None):
        req = MagicMock()
        req.headers = {k.lower(): v for k, v in (headers or {}).items()}
        return req

    def test_consistent(self):
        from proxy_app import _header_hash
        req = self.make_request({"Authorization": "Bearer token1"})
        h1 = _header_hash(req)
        h2 = _header_hash(req)
        assert h1 == h2

    def test_different(self):
        from proxy_app import _header_hash
        req1 = self.make_request({"Authorization": "Bearer token1"})
        req2 = self.make_request({"Authorization": "Bearer token2"})
        assert _header_hash(req1) != _header_hash(req2)


class TestPlaybackUserKey:

    def make_request(self, ip="10.0.0.1", ua="Mozilla/5.0"):
        req = MagicMock()
        req.headers = {"user-agent": ua}
        req.client.host = ip
        return req

    def test_user_key_format(self):
        from proxy_app import _playback_user_key
        req = self.make_request()
        key = _playback_user_key(req, "item123")
        assert len(key) == 3
        assert key[0] == "10.0.0.1"
        assert key[1] == "Mozilla/5.0"
        assert key[2] == "item123"