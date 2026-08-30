"""
浏览目录模拟测试：验证 normalize_attr 字段映射与目录过滤
"""
from unittest.mock import MagicMock, patch

import pytest


def _sample_resp():
    """构建 fs_files_app 模拟响应（Android API 字段）"""
    return {
        "state": True,
        "data": [
            {"fc": 0, "fid": "111", "pid": "0", "fn": "电影", "n": "电影"},
            {"fc": 0, "fid": "222", "pid": "0", "fn": "剧集", "n": "剧集"},
            {"fc": 1, "fid": "333", "pid": "0", "fn": "movie.mkv", "s": "1024", "sha1": "abc", "pc": "ABC"},
            {"fc": 1, "fid": "444", "pid": "0", "fn": "sub.srt", "s": "10", "sha1": "def", "pc": "DEF"},
        ],
    }


@pytest.fixture
def client():
    _client = MagicMock()
    _client.fs_files_app.return_value = _sample_resp()
    return _client


@pytest.fixture
def browse(client):
    def _normalize_attr(info):
        """模拟 p115client.tool.attr.normalize_attr 对 Android API 字段的映射"""
        is_dir = int(info.get("fc", 1)) == 0
        return {
            "is_dir": is_dir,
            "id": int(info.get("fid", 0)),
            "name": info.get("fn") or info.get("n") or "",
        }

    _attr_mod = MagicMock()
    _attr_mod.normalize_attr = _normalize_attr
    _tool_mod = MagicMock()
    _tool_mod.attr = _attr_mod
    _client_mod = MagicMock()
    _client_mod.check_response = lambda resp: resp

    with patch.dict(
        "sys.modules",
        {
            "config_manager": MagicMock(),
            "database": MagicMock(),
            "logger": MagicMock(),
            "p115_client_wrapper": MagicMock(),
            "p115client": _client_mod,
            "p115client.tool": _tool_mod,
            "p115client.tool.attr": _attr_mod,
        },
    ):
        from api_routes import set_client
        import api_routes

        set_client(client)
        yield api_routes.browse_directory
        set_client(None)


def test_browse_only_returns_directories(browse):
    result = browse(pid="0")
    items = result["items"]
    assert len(items) == 2
    assert items[0]["is_dir"] is True
    assert items[0]["id"] == "111"
    assert items[0]["name"] == "电影"
    assert items[1]["id"] == "222"
    assert items[1]["name"] == "剧集"


def test_browse_filters_out_files(browse):
    result = browse(pid="0")
    names = [i["name"] for i in result["items"]]
    assert "movie.mkv" not in names
    assert "sub.srt" not in names


def test_browse_fc_int_type(client, browse):
    resp = _sample_resp()
    resp["data"][0]["fc"] = 0
    resp["data"][1]["fc"] = "0"
    client.fs_files_app.return_value = resp
    result = browse(pid="0")
    assert len(result["items"]) == 2
