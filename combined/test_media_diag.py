"""诊断日志用的直链摘要：可排障但不能泄漏签名。"""
import time

from utils import describe_media_url


def test_signature_params_never_appear():
    url = "https://cdn.115.com/path/movie.mkv?t=1800000000&key=SECRET&sign=ABC&uid=123"
    out = describe_media_url(url)
    assert "cdn.115.com/path/movie.mkv" in out
    for secret in ("SECRET", "sign=", "key=", "uid=", "1800000000"):
        assert secret not in out


def test_expiry_seconds_are_reported():
    future = int(time.time()) + 1200
    out = describe_media_url(f"https://cdn.115.com/a.mp4?t={future}")
    assert "t_left=" in out
    left = int(out.split("t_left=")[1].rstrip("s"))
    assert 1180 <= left <= 1200


def test_jump_url_and_empty_input():
    out = describe_media_url(
        "http://192.168.2.100:3333/redirect_url?pickcode=abc&refresh=1"
    )
    assert "192.168.2.100:3333/redirect_url" in out
    assert "t=none" in out
    assert "pickcode" not in out
    assert "refresh" not in out
    assert describe_media_url("") == "(empty)"
