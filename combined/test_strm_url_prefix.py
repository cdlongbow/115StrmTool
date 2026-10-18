"""
STRM 跳转地址空值防护三层测试：配置归一、同步入口拦截、写盘兜底
借鉴上游 DDSRem P115StrmHelper cf48b39c（handle empty STRM addresses）
"""
from unittest.mock import MagicMock, patch

import pytest

from conftest import API_HEAVY_DEPS, DB_HEAVY_DEPS, stub_modules


class TestConfigNormalize:
    def test_trims_surrounding_whitespace(self):
        from config_manager import P115Config
        cfg = P115Config(strm_url_prefix=" http://host:3333/ ")
        assert cfg.strm_url_prefix == "http://host:3333/"

    def test_blank_becomes_empty_string(self):
        from config_manager import P115Config
        assert P115Config(strm_url_prefix=" \t\n ").strm_url_prefix == ""

    def test_none_becomes_empty_string(self):
        from config_manager import P115Config
        assert P115Config(strm_url_prefix=None).strm_url_prefix == ""

    def test_default_keeps_builtin_value(self):
        from config_manager import P115Config
        assert P115Config().strm_url_prefix == "http://192.168.2.100:3333"


class TestGeneratorGuard:
    def _generator(self, url_prefix):
        with patch.dict("sys.modules", stub_modules(API_HEAVY_DEPS)):
            from strm_generator import StrmGenerator
        return StrmGenerator(MagicMock(), url_prefix)

    def test_init_tolerates_none_and_trims(self):
        assert self._generator(None)._url_prefix == ""
        assert self._generator(" http://h:1/ ")._url_prefix == "http://h:1"

    def test_blank_prefix_raises_on_write(self, tmp_path):
        gen = self._generator("   ")
        gen._overwrite_mode = "never"
        target = tmp_path / "a.mkv.strm"
        with pytest.raises(ValueError, match="未配置"):
            gen._ensure_strm_file(target, "PICKCODE", force=True)
        assert not target.exists()

    def test_valid_prefix_writes_normally(self, tmp_path):
        gen = self._generator("http://h:3333/")
        gen._overwrite_mode = "never"
        target = tmp_path / "a.mkv.strm"
        gen._ensure_strm_file(target, "PICKCODE", force=True)
        assert target.read_text(encoding="utf-8") == (
            "http://h:3333/api/v1/plugin/P115StrmHelper/redirect_url?pickcode=PICKCODE"
        )


class TestLaunchSyncGuard:
    def _api_routes(self):
        with patch.dict("sys.modules", stub_modules(API_HEAVY_DEPS)):
            import api_routes
        return api_routes

    def _launch_with_prefix(self, prefix):
        api_routes = self._api_routes()
        cm = MagicMock()
        cm.get.return_value = {"p115": {"strm_url_prefix": prefix}}
        with (
            patch.object(api_routes, "get_client", return_value=MagicMock()),
            patch.object(api_routes, "config_manager", cm),
        ):
            return api_routes._launch_sync("full")

    def test_blank_prefix_rejected(self):
        resp = self._launch_with_prefix("  ")
        assert resp["status"] == "error"
        assert "未配置" in resp["message"]

    def test_valid_prefix_passes_guard(self):
        with patch.dict("sys.modules", {
            **stub_modules(API_HEAVY_DEPS),
            **stub_modules(DB_HEAVY_DEPS),
        }):
            import api_routes
            cm = MagicMock()
            cm.get.return_value = {"p115": {"strm_url_prefix": "http://h:3333", "paths": []}}
            with (
                patch.object(api_routes, "get_client", return_value=MagicMock()),
                patch.object(api_routes, "config_manager", cm),
            ):
                resp = api_routes._launch_sync("full")
        assert resp["status"] != "error" or "未配置" not in resp["message"]
