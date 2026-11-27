"""
外部播放器协议测试：覆盖播放器与操作系统矩阵及跳转白名单约束
"""
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from starlette.requests import Request

from external_players import (
    ALLOWED_INTENT_PACKAGES,
    EXTERNAL_PLAYERS,
    build_external_player_script,
    decode_redirect_link,
    inject_external_urls,
    is_safe_redirect_target,
)

OS_USER_AGENTS = {
    "windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "macOS": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "ios": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
    "android": "Mozilla/5.0 (Linux; Android 14; Pixel 8)",
    "ubuntu": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64)",
    "other": "curl/8.0",
}

EXPECTED_SCHEMES = {
    "potplayer": {"*": "potplayer"},
    "vlc": {"windows": "vlc", "macOS": "vlc", "ios": "vlc-x-callback", "*": "intent"},
    "iina": {"*": "iina"},
    "infuse": {"*": "infuse"},
    "mpv": {"*": "mpv-handler"},
    "nplayer": {"macOS": "nplayer-mac", "*": "nplayer-https"},
    "omniplayer": {"*": "omniplayer"},
    "figplayer": {"*": "figplayer"},
    "senplayer": {"*": "senplayer"},
    "fileball": {"*": "filebox"},
    "stellarplayer": {"*": "stellar"},
    "mxplayer": {"*": "intent"},
    "mxplayerpro": {"*": "intent"},
    "ddplay": {"android": "intent", "*": "ddplay"},
}


def _request(user_agent: str, scheme: str = "https") -> Request:
    """构造带固定主机与用户代理的测试请求"""
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/emby/Items/1",
        "query_string": b"",
        "headers": [
            (b"host", b"proxy.test"),
            (b"user-agent", user_agent.encode("ascii")),
        ],
        "scheme": scheme,
        "server": ("proxy.test", 443 if scheme == "https" else 80),
    })


def _decoded_target(url: str) -> str:
    """从外部播放器跳转链接中还原原始协议地址"""
    query = parse_qs(urlsplit(url).query)
    return decode_redirect_link(unquote(query["link"][0]))


@pytest.mark.parametrize("player_key", sorted(EXTERNAL_PLAYERS))
@pytest.mark.parametrize("os_type,user_agent", sorted(OS_USER_AGENTS.items()))
def test_player_os_matrix_generates_allowed_target(player_key, os_type, user_agent):
    """每种播放器与操作系统组合都应生成可通过白名单的协议链接"""
    data = {
        "Name": "示例影片",
        "MediaSources": [{"Id": "source-1", "Container": "mkv", "Type": "Video"}],
    }
    injected = inject_external_urls(
        data,
        _request(user_agent),
        "https://proxy.test",
        "1",
        "token",
        [player_key],
    )
    assert injected is True
    target = _decoded_target(data["ExternalUrls"][0]["Url"])
    expected = EXPECTED_SCHEMES[player_key].get(os_type, EXPECTED_SCHEMES[player_key]["*"])
    assert urlsplit(target).scheme.lower() == expected
    assert is_safe_redirect_target(target, "proxy.test") is True


def test_multi_media_source_urls_are_all_injected_and_matchable():
    """多版本媒体源应分别注入链接，前端脚本应保留全部匹配项"""
    data = {
        "Name": "示例影片",
        "MediaSources": [
            {"Id": "source-1", "Name": "1080p", "Container": "mkv", "Type": "Video"},
            {"Id": "source-2", "Name": "4K", "Container": "mkv", "Type": "Video"},
        ],
    }
    assert inject_external_urls(
        data,
        _request(OS_USER_AGENTS["windows"]),
        "https://proxy.test",
        "1",
        "token",
        ["vlc"],
    )
    assert [item["Name"] for item in data["ExternalUrls"]] == [
        "vlc:VLC - 1080p",
        "vlc:VLC - 4K",
    ]
    script = build_external_player_script(["vlc"])
    assert "getPlayerUrls" in script
    assert "matches.forEach" in script


@pytest.mark.parametrize("package", sorted(ALLOWED_INTENT_PACKAGES))
def test_intent_allows_known_package_and_same_host(package):
    """已知 Android 包名且目标同站时应放行"""
    target = f"intent:https://proxy.test/video#Intent;package={package};end"
    assert is_safe_redirect_target(target, "proxy.test") is True


@pytest.mark.parametrize(
    "target",
    [
        "intent:https://proxy.test/video#Intent;package=com.evil.player;end",
        "intent:https://evil.test/video#Intent;package=org.videolan.vlc;end",
        "intent:https://proxy.test/video",
        "intent:ftp://proxy.test/video#Intent;package=org.videolan.vlc;end",
    ],
)
def test_intent_rejects_unconstrained_targets(target):
    """未知包名、跨站、缺失 Intent 后缀与非 HTTP 目标应拒绝"""
    assert is_safe_redirect_target(target, "proxy.test") is False


@pytest.mark.parametrize(
    "fallback_extra",
    [
        "S.browser_fallback_url=https://evil.test/",
        "browser_fallback_url=https://evil.test/",
        "s.browser_fallback_url=https%3A%2F%2Fevil.test%2F",
        "S.browser%5Ffallback%5Furl=https%3A%2F%2Fevil.test%2F",
    ],
)
def test_intent_rejects_browser_fallback_extra(fallback_extra):
    """Intent 回退额外参数应拒绝，避免绕过同站目标限制"""
    target = (
        "intent:https://proxy.test/video#Intent;package=org.videolan.vlc;"
        f"{fallback_extra};end"
    )
    assert is_safe_redirect_target(target, "proxy.test") is False


@pytest.mark.parametrize("scheme", ["nplayer-http", "nplayer-https"])
def test_nplayer_http_schemes_require_same_host(scheme):
    """nPlayer HTTP 系列协议应限制到本站主机"""
    assert is_safe_redirect_target(f"{scheme}://proxy.test/video", "proxy.test") is True
    assert is_safe_redirect_target(f"{scheme}://evil.test/video", "proxy.test") is False
