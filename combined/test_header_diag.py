"""请求头摘要：只给名字与长度，值绝不写进日志。"""
from utils import describe_headers


def test_header_values_never_appear():
    out = describe_headers(
        {
            "User-Agent": "YingShiTV For Android",
            "X-Emby-Token": "SUPERSECRET",
            "Cookie": "session=abcdef",
            "Range": "bytes=0-",
        }
    )
    for secret in ("SUPERSECRET", "session=abcdef", "YingShiTV", "bytes=0-"):
        assert secret not in out
    assert "x-emby-token(11)" in out
    assert "cookie(14)" in out
    assert "range(8)" in out
    assert "user-agent(21)" in out
    assert "sensitive=cookie(14),x-emby-token(11)" in out


def test_sensitive_group_and_empty_headers():
    out = describe_headers({"accept": "*/*"})
    assert out == "all=accept(3) | sensitive=-"
    assert describe_headers({}) == "all=- | sensitive=-"
