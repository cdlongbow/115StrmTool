"""
115 客户端封装测试：405 降级、目录保障、复制/删除副本、限速集成

开发环境未安装 p115client / p115cipher / p115pickcode，
通过 sys.modules 注入替身模块使封装层可独立测试
"""
from threading import Lock
from unittest.mock import MagicMock

from conftest import P115_NATIVE_MODULES, stub_missing

stub_missing(P115_NATIVE_MODULES)

from p115_client_wrapper import P115ClientWrapper  # noqa: E402

COPY_PC = "c" * 17


def _bare_wrapper():
    """绕过外部依赖构造仅含内存状态的封装实例"""
    wrapper = P115ClientWrapper.__new__(P115ClientWrapper)
    wrapper._cookie = ""
    wrapper._client = MagicMock()
    wrapper._http_client = None
    wrapper._cooldown_lock = Lock()
    wrapper._limiters = {}
    wrapper._cooldowns = {}
    return wrapper


class Err405(Exception):
    def __init__(self):
        super().__init__("server returned 405 Method Not Allowed")
        self.status_code = 405


def test_is_405_error_detects_status_and_message():
    assert P115ClientWrapper._is_405_error(Err405())
    assert P115ClientWrapper._is_405_error(Exception("HTTP 405"))
    assert not P115ClientWrapper._is_405_error(Exception("network timeout"))


def test_call_with_405_fallback_switches_to_app():
    wrapper = _bare_wrapper()
    primary = MagicMock(side_effect=Err405())
    fallback = MagicMock(return_value={"ok": 1})
    result = wrapper._call_with_405_fallback(primary, fallback, "测试操作")
    assert result == {"ok": 1}
    primary.assert_called_once()
    fallback.assert_called_once()


def test_call_with_405_fallback_reraises_non_405():
    wrapper = _bare_wrapper()
    primary = MagicMock(side_effect=ValueError("boom"))
    fallback = MagicMock()
    try:
        wrapper._call_with_405_fallback(primary, fallback, "测试操作")
        raise AssertionError("非 405 异常应向上抛出")
    except ValueError:
        pass
    fallback.assert_not_called()


def test_wait_cooling_creates_reusable_limiter():
    wrapper = _bare_wrapper()
    wrapper._cooldowns = {"fs_dir_getid": 5.0}
    wrapper._wait_cooling("fs_dir_getid")
    limiter = wrapper._limiters["fs_dir_getid"]
    assert limiter.interval == 5.0
    wrapper._wait_cooling("fs_dir_getid")
    assert wrapper._limiters["fs_dir_getid"] is limiter, "同一端点应复用限速器"


def test_ensure_folder_returns_existing_id():
    wrapper = _bare_wrapper()
    wrapper._client.fs_dir_getid.return_value = {"state": True, "id": "12345"}
    assert wrapper.ensure_folder("/多端播放") == 12345
    wrapper._client.fs_mkdir.assert_not_called()


def test_ensure_folder_creates_when_missing():
    wrapper = _bare_wrapper()
    wrapper._client.fs_dir_getid.return_value = {"state": True, "id": "-1"}
    wrapper._client.fs_mkdir.return_value = {"state": True, "cid": "999"}
    assert wrapper.ensure_folder("/多端播放") == 999
    wrapper._client.fs_mkdir.assert_called_once_with("多端播放", 0)


def test_ensure_folder_root_returns_zero():
    wrapper = _bare_wrapper()
    assert wrapper.ensure_folder("/") == 0
    assert wrapper.ensure_folder("") == 0


def test_copy_pickcode_returns_copy_pc():
    wrapper = _bare_wrapper()
    wrapper._client.fs_files.return_value = {"data": [{"pc": COPY_PC}]}
    result = wrapper.copy_pickcode("a" * 17, 555)
    assert result == COPY_PC
    wrapper._client.fs_copy.assert_called_once()
    assert wrapper._client.fs_copy.call_args.kwargs.get("pid") == 555


def test_copy_pickcode_returns_none_when_query_empty():
    wrapper = _bare_wrapper()
    wrapper._client.fs_files.return_value = {"data": []}
    import p115_client_wrapper as pw

    original = pw._DOWNLOAD_RETRY_DELAYS
    pw._DOWNLOAD_RETRY_DELAYS = ()
    try:
        assert wrapper.copy_pickcode("a" * 17, 555) is None
    finally:
        pw._DOWNLOAD_RETRY_DELAYS = original


def test_delete_file_calls_fs_delete():
    wrapper = _bare_wrapper()
    assert wrapper.delete_file(COPY_PC) is True
    wrapper._client.fs_delete.assert_called_once()


def test_delete_file_swallows_error():
    wrapper = _bare_wrapper()
    wrapper._client.fs_delete.side_effect = RuntimeError("boom")
    assert wrapper.delete_file(COPY_PC) is False
