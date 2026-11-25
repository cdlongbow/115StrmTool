"""
附属文件下载请求头转发测试：P115URL 携带的直链绑定头必须随下载请求发送
"""
from unittest.mock import MagicMock, patch

from conftest import API_HEAVY_DEPS, stub_modules


class _FakeP115URL(str):
    def __new__(cls, url, headers):
        obj = super().__new__(cls, url)
        obj._mapping = {"headers": headers}
        return obj

    def get(self, key, default=None):
        return self._mapping.get(key, default)


class _FakeStreamResponse:
    def __init__(self, chunks):
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self):
        yield from self._chunks


def _generator(client):
    with patch.dict("sys.modules", stub_modules(API_HEAVY_DEPS)):
        from strm_generator import StrmGenerator
    return StrmGenerator(client, "http://127.0.0.1:3333")


def _download(tmp_path, monkeypatch, result):
    captured = {}

    def fake_stream(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return _FakeStreamResponse([b"subtitle-data"])

    monkeypatch.setattr("httpx.stream", fake_stream)
    client = MagicMock()
    client.download_url.return_value = result
    target = tmp_path / "nested" / "movie.zh.srt"
    _generator(client)._download_aux_file("pc123", "movie.zh.srt", target)
    return target, captured


class TestAuxDownloadHeaders:
    def test_forwards_p115url_bound_headers(self, tmp_path, monkeypatch):
        url = _FakeP115URL(
            "https://cdn.test/a.srt",
            {"User-Agent": "115-UA", "Cookie": "UID=1"},
        )
        target, captured = _download(tmp_path, monkeypatch, url)
        assert target.read_bytes() == b"subtitle-data"
        assert captured["method"] == "GET"
        assert captured["url"] == "https://cdn.test/a.srt"
        assert captured["headers"] == {
            "user-agent": "115-UA",
            "cookie": "UID=1",
        }

    def test_forwards_dict_result_headers(self, tmp_path, monkeypatch):
        target, captured = _download(
            tmp_path,
            monkeypatch,
            {"url": "https://cdn.test/a.srt", "headers": {"user-agent": "UA-DICT"}},
        )
        assert target.read_bytes() == b"subtitle-data"
        assert captured["headers"] == {"user-agent": "UA-DICT"}

    def test_plain_url_without_headers_passes_none(self, tmp_path, monkeypatch):
        target, captured = _download(
            tmp_path, monkeypatch, "https://cdn.test/a.srt"
        )
        assert target.read_bytes() == b"subtitle-data"
        assert captured["headers"] is None
