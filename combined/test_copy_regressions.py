"""
多端播放副本回归测试

覆盖复制响应优先、目录快照、目标目录互斥、源身份校验与多候选拒绝
"""
import sys
from threading import Lock
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from conftest import P115_NATIVE_MODULES, stub_missing

stub_missing(P115_NATIVE_MODULES)

import p115_client_wrapper as pw  # noqa: E402
from p115_client_wrapper import P115ClientWrapper  # noqa: E402

SOURCE_PC = "a" * 17
COPY_PC = "9" * 17
OTHER_PC = "8" * 17
SOURCE_ID = 111
COPY_ID = 456
OTHER_ID = 789


def _bare_wrapper():
    wrapper = P115ClientWrapper.__new__(P115ClientWrapper)
    wrapper._cookie = ""
    wrapper._client = MagicMock()
    wrapper._http_client = None
    wrapper._cooldown_lock = Lock()
    wrapper._limiters = {}
    wrapper._cooldowns = {}
    wrapper._copy_locks_guard = Lock()
    wrapper._copy_locks = {}
    return wrapper


@pytest.fixture(autouse=True)
def _pickcode_codec(monkeypatch):
    module = ModuleType("p115pickcode")

    def _to_id(value):
        if isinstance(value, int):
            return value
        if str(value).isdigit():
            return int(value)
        return {
            SOURCE_PC: SOURCE_ID,
            COPY_PC: 999,
            OTHER_PC: 888,
        }.get(value, 777)

    def _to_pickcode(value):
        return {
            COPY_ID: COPY_PC,
            OTHER_ID: OTHER_PC,
        }.get(int(value), "f" * 17)

    module.to_id = _to_id
    module.to_pickcode = _to_pickcode
    monkeypatch.setitem(sys.modules, "p115pickcode", module)
    monkeypatch.setattr(pw, "_DOWNLOAD_RETRY_DELAYS", ())


def test_copy_uses_response_pickcode_after_single_snapshot():
    wrapper = _bare_wrapper()
    wrapper._client.fs_file.return_value = {}
    wrapper._client.fs_files.return_value = {"data": []}
    wrapper._client.fs_copy.return_value = {"state": True, "pc": COPY_PC}

    assert wrapper.copy_pickcode(SOURCE_PC, 555) == COPY_PC
    assert wrapper._client.fs_files.call_count == 1


def test_copy_uses_response_file_id_after_single_snapshot():
    wrapper = _bare_wrapper()
    wrapper._client.fs_file.return_value = {}
    wrapper._client.fs_files.return_value = {"data": []}
    wrapper._client.fs_copy.return_value = {"state": True, "fid": COPY_ID}

    assert wrapper.copy_pickcode(SOURCE_PC, 555) == COPY_PC
    assert wrapper._client.fs_files.call_count == 1


def test_copy_fallback_matches_new_item_by_source_identity():
    wrapper = _bare_wrapper()
    wrapper._client.fs_file.return_value = {
        "file_name": "movie.mkv",
        "file_size": 100,
        "file_sha1": "abc",
    }
    wrapper._client.fs_copy.return_value = {}
    wrapper._client.fs_files.side_effect = [
        {"data": []},
        {
            "data": [
                {
                    "fid": COPY_ID,
                    "pc": COPY_PC,
                    "name": "movie.mkv",
                    "size": 100,
                    "sha1": "abc",
                }
            ]
        },
    ]

    assert wrapper.copy_pickcode(SOURCE_PC, 555) == COPY_PC


def test_copy_fallback_rejects_unrelated_item():
    wrapper = _bare_wrapper()
    wrapper._client.fs_file.return_value = {
        "file_name": "movie.mkv",
        "file_size": 100,
        "file_sha1": "abc",
    }
    wrapper._client.fs_copy.return_value = {}
    wrapper._client.fs_files.side_effect = [
        {"data": []},
        {
            "data": [
                {
                    "fid": OTHER_ID,
                    "pc": OTHER_PC,
                    "name": "other.mkv",
                    "size": 100,
                    "sha1": "abc",
                }
            ]
        },
    ]

    assert wrapper.copy_pickcode(SOURCE_PC, 555) is None


def test_copy_fallback_rejects_multiple_matching_items():
    wrapper = _bare_wrapper()
    wrapper._client.fs_file.return_value = {
        "file_name": "movie.mkv",
        "file_size": 100,
        "file_sha1": "abc",
    }
    wrapper._client.fs_copy.return_value = {}
    wrapper._client.fs_files.side_effect = [
        {"data": []},
        {
            "data": [
                {
                    "fid": COPY_ID,
                    "pc": COPY_PC,
                    "name": "movie.mkv",
                    "size": 100,
                    "sha1": "abc",
                },
                {
                    "fid": OTHER_ID,
                    "pc": OTHER_PC,
                    "name": "movie.mkv",
                    "size": 100,
                    "sha1": "abc",
                },
            ]
        },
    ]

    assert wrapper.copy_pickcode(SOURCE_PC, 555) is None


def test_copy_fallback_without_identity_rejects_multiple_items():
    wrapper = _bare_wrapper()
    wrapper._client.fs_file.return_value = {}
    wrapper._client.fs_copy.return_value = {}
    wrapper._client.fs_files.return_value = {
        "data": [{"pc": COPY_PC}, {"pc": OTHER_PC}]
    }

    assert wrapper.copy_pickcode(SOURCE_PC, 555) is None


def test_copy_fallback_without_identity_rejects_preexisting_single_item():
    wrapper = _bare_wrapper()
    wrapper._client.fs_file.return_value = {}
    wrapper._client.fs_copy.return_value = {}
    wrapper._client.fs_files.side_effect = [
        {"data": [{"fid": OTHER_ID, "pc": OTHER_PC}]},
        {"data": [{"fid": OTHER_ID, "pc": OTHER_PC}]},
    ]

    assert wrapper.copy_pickcode(SOURCE_PC, 555) is None


def test_copy_fallback_abandons_guess_when_snapshot_fails():
    wrapper = _bare_wrapper()
    wrapper._client.fs_file.return_value = {}
    wrapper._client.fs_files.return_value = None

    assert wrapper.copy_pickcode(SOURCE_PC, 555) is None
    wrapper._client.fs_copy.assert_not_called()


def test_copy_lock_is_scoped_by_target_directory():
    wrapper = _bare_wrapper()
    assert wrapper._copy_lock_for(555) is wrapper._copy_lock_for(555)
    assert wrapper._copy_lock_for(555) is not wrapper._copy_lock_for(556)
