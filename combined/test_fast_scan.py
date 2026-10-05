"""
快速整树扫描测试：分页合并、路径拼接、pickcode 推导、降级矩阵、取消与进度
"""
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest

with patch("sys.modules", {"logger": __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()}):
    import fast_scan

with patch("sys.modules", {"logger": __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()}):
    from fast_scan import FastScanUnavailable, iter_files_fast

ROOT_CID = 100
ROOT_PATH = "/Media"

FILES_PAGE1 = {
    "state": True,
    "count": 4,
    "data": [
        {"cid": "200", "n": "a.mkv", "s": "1024", "sha1": "aa", "pc": "ab12"},
        {"cid": "300", "n": "b.mp4", "s": 2048, "sha1": "", "pc": "cd34"},
        {"cid": "300", "n": "cover.jpg", "s": "10", "sha1": "cc", "pc": "ef56"},
    ],
}
DIRS_PAGE1 = {
    "state": True,
    "data": {
        "list": [
            {"fid": "200", "fn": "Movie", "pid": "100"},
            {"fid": "300", "fn": "TV", "pid": "200"},
        ],
        "has_next_page": False,
    },
}


class FakeWrapper:
    """按调用序列回放 scan_get_json 响应或抛出异常"""

    def __init__(self, handler):
        self._handler = handler
        self.calls = []
        self.derived_pc = None

    def scan_get_json(self, endpoint, params):
        self.calls.append((endpoint, dict(params)))
        result = self._handler(endpoint, len(self.calls))
        if isinstance(result, Exception):
            raise result
        return result


def _pickcode_mod(stable="0ab1"):
    return SimpleNamespace(
        get_stable_point=lambda pc: stable if pc else (_ for _ in ()).throw(ValueError),
        id_to_pickcode=lambda cid, sp, prefix: f"PC{prefix}{cid}",
    )


def _default_handler(endpoint, nth):
    if endpoint == "files":
        return FILES_PAGE1 if nth == 1 else {
            "state": True, "count": 4, "data": [
                {"cid": "200", "n": "d.txt", "s": "1", "sha1": "", "pc": "gh78"},
            ],
        }
    return DIRS_PAGE1


def _run(handler, cancellation=None, pickcode_mod=None):
    wrapper = FakeWrapper(handler)
    mod = pickcode_mod if pickcode_mod is not None else _pickcode_mod()
    with patch.dict("sys.modules", {"p115pickcode": mod}):
        return iter_files_fast(
            wrapper, ROOT_CID, root_pan_path=ROOT_PATH, cancellation=cancellation,
        ), wrapper


def test_paths_and_fields():
    results, _ = _run(_default_handler)
    by_name = {r["name"]: r for r in results}
    assert set(by_name) == {"a.mkv", "b.mp4", "cover.jpg", "d.txt"}
    assert by_name["a.mkv"]["path"] == "/Media/Movie/a.mkv"
    assert by_name["b.mp4"]["path"] == "/Media/Movie/TV/b.mp4"
    assert by_name["a.mkv"]["size"] == 1024
    assert by_name["b.mp4"]["size"] == 2048
    assert by_name["cover.jpg"]["sha1"] == "cc"
    assert all(r["is_dir"] is False for r in results)


def test_multi_page_files_and_progress():
    def handler(endpoint, nth):
        if endpoint == "files":
            return _default_handler(endpoint, nth)
        return DIRS_PAGE1

    results, wrapper = _run(handler)
    assert len(results) == 4
    files_calls = [c for c in wrapper.calls if c[0] == "files"]
    assert [c[1]["offset"] for c in files_calls] == ["0", "3"]


def test_progress_callback():
    wrapper = FakeWrapper(_default_handler)
    progress = []
    with patch.dict("sys.modules", {"p115pickcode": _pickcode_mod()}):
        fast_scan.iter_files_fast(
            wrapper, ROOT_CID, root_pan_path=ROOT_PATH,
            progress=lambda cur, total: progress.append((cur, total)),
        )
    assert progress[0][1] == 4


def test_empty_page_shortcut():
    def handler(endpoint, nth):
        if endpoint == "files":
            if nth == 1:
                return {"state": True, "count": 99, "data": FILES_PAGE1["data"]}
            return {"state": True, "count": 99, "data": []}
        return DIRS_PAGE1

    results, _ = _run(handler)
    assert len(results) == 3


def test_state_false_raises():
    def handler(endpoint, nth):
        if endpoint == "files":
            return {"state": False, "error": "risk"}
        return DIRS_PAGE1

    with pytest.raises(FastScanUnavailable):
        _run(handler)


def test_request_error_raises():
    def handler(endpoint, nth):
        return RuntimeError("connection reset")

    with pytest.raises(FastScanUnavailable):
        _run(handler)


def test_no_usable_pc_raises():
    def handler(endpoint, nth):
        if endpoint == "files":
            return {"state": True, "count": 1, "data": [
                {"cid": "200", "n": "x.mkv", "s": "1", "pc": "fdir"},
            ]}
        return DIRS_PAGE1

    with pytest.raises(FastScanUnavailable):
        _run(handler)


def test_stable_point_error_raises():
    mod = SimpleNamespace(
        get_stable_point=lambda pc: (_ for _ in ()).throw(ValueError("bad")),
        id_to_pickcode=lambda cid, sp, prefix: "PC",
    )
    with pytest.raises(FastScanUnavailable):
        _run(_default_handler, pickcode_mod=mod)


def test_dir_map_missing_raises():
    def handler(endpoint, nth):
        if endpoint == "files":
            return {"state": True, "count": 1, "data": [
                {"cid": "999", "n": "x.mkv", "s": "1", "pc": "ab12"},
            ]}
        return DIRS_PAGE1

    with pytest.raises(FastScanUnavailable):
        _run(handler)


def test_dirs_page_limit(monkeypatch):
    monkeypatch.setattr(fast_scan, "MAX_DIRS_PAGES", 2)

    def handler(endpoint, nth):
        if endpoint == "files":
            return {"state": True, "count": 1, "data": [
                {"cid": "200", "n": "x.mkv", "s": "1", "pc": "ab12"},
            ]}
        return {"state": True, "data": {
            "list": [{"fid": "200", "fn": "M", "pid": "100"}], "has_next_page": True,
        }}

    with pytest.raises(FastScanUnavailable):
        _run(handler)


def test_cancel_returns_empty():
    cancelled = Event()
    cancelled.set()

    def handler(endpoint, nth):
        return DIRS_PAGE1

    results, wrapper = _run(handler, cancellation=cancelled)
    assert results == []
    assert wrapper.calls == []


def test_root_dir_entry_skipped():
    def handler(endpoint, nth):
        if endpoint == "files":
            return {"state": True, "count": 1, "data": [
                {"cid": "100", "n": "top.mkv", "s": "1", "pc": "ab12"},
            ]}
        return {"state": True, "data": {"list": [
            {"fid": "100", "fn": "Media", "pid": "0"},
        ], "has_next_page": False}}

    results, _ = _run(handler)
    assert results[0]["path"] == "/Media/top.mkv"
