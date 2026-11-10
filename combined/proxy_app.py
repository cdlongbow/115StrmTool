from asyncio import (
    FIRST_COMPLETED,
    create_task,
    gather,
    sleep,
    wait,
)
from contextlib import asynccontextmanager
from hashlib import sha256
from ipaddress import ip_address
from re import IGNORECASE, compile as re_compile, search as re_search, sub as re_sub
from time import monotonic
from typing import Any, Iterable, List, Tuple
from urllib.parse import quote, urljoin, urlparse

from utils import AsyncKeyLock, AsyncTtlCache, describe_headers, describe_media_url

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from httpx import (
    AsyncClient,
    Limits,
    Request as HttpxRequest,
    Response as HttpxResponse,
    Timeout,
    TimeoutException,
)
from starlette.requests import ClientDisconnect
from websockets import connect

from logger import logger

from config_manager import PLAYBACK_MODE_VALUES, PlaybackMode

from external_players import (
    ALL_EXTERNAL_PLAYER_KEYS,
    EXTERNAL_PLAYERS,
    EXTERNAL_PLAYER_MARKER,
    REDIRECT_PATH,
    build_external_player_script,
    decode_redirect_link,
    extract_api_key,
    is_safe_redirect_target,
    inject_external_urls,
)

PLAYBACK_URL_CACHE_TTL_SECONDS = 90
PLAYBACK_USER_CACHE_TTL_SECONDS = 300
PLAYBACK_URL_CACHE_MAX_SIZE = 500
# STRM 源地址必须在整次播放会话内可用：暂停或长片中途上游断链时
# 续传会重新解析直链，缓存一过期就再也找不到源地址（表现为网络读取失败）
PLAYBACK_STRM_CACHE_TTL_SECONDS = 21600
REDIRECT_RESOLVE_TIMEOUTS = ((3.0, 10.0), (3.0, 15.0), (5.0, 20.0))

STREAM_RETRY_MAX = 2
STREAM_RETRY_BUDGET_SECONDS = 10.0
STREAM_RETRY_BACKOFF_SECONDS = (0.5, 1.5)
STREAM_RESUME_MAX = 3
STREAM_RESUME_TOTAL_MAX = 16
STREAM_RESUME_BUDGET_SECONDS = 8.0
STREAM_RESUME_BACKOFF_SECONDS = (0.3, 1.0, 2.0)
RESUME_DROP_REQUEST_HEADERS = frozenset(
    {
        "if-range",
        "if-match",
        "if-unmodified-since",
        "if-none-match",
        "if-modified-since",
    }
)
UPSTREAM_FORWARD_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "content-length",
        "content-range",
        "accept-ranges",
    }
)

CACHE_KEY_HEADERS = (
    "authorization",
    "cookie",
    "x-emby-token",
    "user-agent",
    "x-emby-device-id",
    "x-emby-device-name",
    "x-emby-client",
    "x-emby-client-version",
    "x-device-id",
    "x-device-name",
    "x-client",
)

HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

MEDIA_ROUTES = [
    "/audio/{item_id}/{name}",
    "/emby/audio/{item_id}/{name}",
    "/videos/{item_id}/{name}",
    "/emby/videos/{item_id}/{name}",
    "/items/{item_id}/download",
    "/emby/items/{item_id}/download",
    "/items/{item_id}/file",
    "/emby/items/{item_id}/file",
    "/sync/jobitems/{item_id}/file",
    "/emby/sync/jobitems/{item_id}/file",
]

NON_MEDIA_NAMES = frozenset(
    {
        "additionalparts",
        "subtitles",
        "similar",
        "thememedia",
        "themevideos",
        "themesongs",
        "specialfeatures",
        "linkeditems",
    }
)

CROSS_ORIGIN_PATTERN = r"&&\s*\(elem\.crossOrigin\s*=\s*initialSubtitleStream\)"

PLUGIN_CROSS_ORIGIN_RE = re_compile(r"&&\(\w+\.crossOrigin=\w+\)")

CROSS_ORIGIN_VALUE_RE = re_compile(
    r'\w+\.IsRemote\s*&&\s*"DirectPlay"\s*===\s*\w+\s*\?\s*null\s*:\s*"anonymous"'
)

CROSS_ORIGIN_INTERCEPT_MARKER = "[EmbyReverseProxy] crossOrigin"

CROSS_ORIGIN_INTERCEPT_SCRIPT = (
    """<script>
(function(){
  // """
    + CROSS_ORIGIN_INTERCEPT_MARKER
    + """ 拦截器 — 302 直链播放避免 CORS
  try {
    Object.defineProperty(HTMLMediaElement.prototype,'crossOrigin',{
      get:function(){return null},
      set:function(){},
      configurable:true
    });
  } catch(e){}
  try {
    var ob=new MutationObserver(function(ms){
      ms.forEach(function(m){
        if(m.type==='attributes'&&m.attributeName==='crossorigin'){
          m.target.removeAttribute('crossorigin');
        }
        if(m.type==='childList'){
          m.addedNodes.forEach(function(n){
            if(n.nodeType===1&&(n.tagName==='VIDEO'||n.tagName==='AUDIO')){
              n.removeAttribute('crossorigin');
            }
          });
        }
      });
    });
    if(document.documentElement){
      ob.observe(document.documentElement,{attributes:true,attributeFilter:['crossorigin'],childList:true,subtree:true});
    } else {
      document.addEventListener('DOMContentLoaded',function(){
        ob.observe(document.documentElement,{attributes:true,attributeFilter:['crossorigin'],childList:true,subtree:true});
      });
    }
  } catch(e){}
})();
</script>"""
)


# ---- 模块级辅助函数（从 create_app 内闭包提升） ----


async def _read_request_body_safe(request: Request) -> bytes | None:
    """
    读取请求体；客户端已断开时返回 None，避免 ClientDisconnect 未处理导致 500

    :param request: 当前请求
    :return: 请求体字节串，或 None 表示客户端已断开
    """
    try:
        return await request.body()
    except ClientDisconnect:
        logger.debug(
            "客户端已断开: %s %s",
            request.method,
            request.scope.get("path", ""),
        )
        return None


def _range_starts_at_zero(range_header: str) -> bool:
    """
    判断 Range 请求头是否从文件起始字节开始

    形如 bytes=0-/bytes=0-1023 的请求语义等价于整段内容，
    上游返回 200 属于合法行为，不应触发换链重试

    :param range_header (str): 原始 Range 请求头

    :return bool: 起始字节为 0 时返回 True，否则返回 False
    """
    value = range_header.strip()
    if not value.lower().startswith("bytes="):
        return False
    spec = value[len("bytes="):].split(",", 1)[0].strip()
    if not spec:
        return False
    start = spec.split("-", 1)[0].strip()
    return start == "0"


def _range_start_offset(range_header: str) -> int:
    """
    解析 Range 请求头的起始字节偏移

    开放区间（bytes=100-）与闭合区间（bytes=100-199）均取起始值；
    仅后缀区间（bytes=-500）与无法解析的 Range 返回 0，
    此时真实起点以首个响应的 Content-Range 为准

    :param range_header (str): 原始 Range 请求头

    :return int: 起始字节偏移，无法确定时为 0
    """
    value = range_header.strip()
    if not value.lower().startswith("bytes="):
        return 0
    spec = value[len("bytes="):].split(",", 1)[0].strip()
    if not spec:
        return 0
    start = spec.split("-", 1)[0].strip()
    if not start.isdigit():
        return 0
    return int(start)


def _content_range_start(content_range: str) -> int | None:
    """
    从 Content-Range 响应头取出本段的起始字节

    形如 `bytes 100-199/1000`，仅识别 bytes 单位且起始为数字的形态

    :param content_range (str): Content-Range 响应头值

    :return int: 本段起始字节，无法解析时返回 None
    """
    match = re_search(r"bytes\s+(\d+)-", content_range.strip(), IGNORECASE)
    if match is None:
        return None
    return int(match.group(1))


def _build_resume_headers(headers: dict[str, str], start: int) -> dict[str, str]:
    """
    基于原转发头构建半身后断续传用的请求头

    Range 改写为从断点偏移开放到底；条件类请求头描述的是已失效的旧链路条件，
    保留会让 CDN 判成整段 200 或 412 从而中断续传，必须剥离

    :param headers (Dict): 原始转发请求头
    :param start (int): 续传起始字节偏移

    :return Dict: 改写 Range 并剥离条件头后的请求头
    """
    resumed = {
        k: v
        for k, v in headers.items()
        if k.lower() != "range" and k.lower() not in RESUME_DROP_REQUEST_HEADERS
    }
    resumed["range"] = f"bytes={start}-"
    return resumed


def _strip_accept_encoding(headers: dict[str, str]) -> dict[str, str]:
    """
    去掉 Accept-Encoding，便于上游返回未压缩内容以便修改 body

    :param headers: 原始转发头
    :return: 不包含 accept-encoding 的头字典
    """
    return {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}


def _strip_response_validators(headers: dict[str, str]) -> dict[str, str]:
    """
    移除正文变化后失效的响应校验头，防止客户端缓存脏数据

    当代理改写响应体（如强制 DirectPlay、注入端口）后，
    ETag/Last-Modified/Content-MD5 不再匹配实际内容，
    必须剥离以避免客户端用旧缓存命中改写前的响应

    :param headers: 上游响应头

    :return: 不含正文校验字段的响应头
    """
    validators = {"content-md5", "etag", "last-modified"}
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in validators
    }


RESPONSE_VALIDATOR_HEADERS = frozenset({"content-md5", "etag", "last-modified"})


def _filter_response_headers(
    resp: HttpxResponse, drop: Iterable[str] = ()
) -> list[tuple[str, str]]:
    """
    生成透传给客户端的响应头列表，剔除 hop-by-hop 与附加排除项

    使用二元组列表而非字典，保留 Set-Cookie 等重复同名头

    :param resp (HttpxResponse): 上游响应
    :param drop (Iterable): 额外的排除头名（小写）

    :return List: (header 名, 值) 列表
    """
    excluded = HOP_BY_HOP_HEADERS | {name.lower() for name in drop}
    return [
        (k, v)
        for k, v in resp.headers.multi_items()
        if k.lower() not in excluded
    ]


def _with_raw_headers(response: Response, pairs: list[tuple[str, str]]) -> Response:
    """
    用 raw_headers 将（含重复同名头的）响应头列表应用到 Response

    :param response (Response): 目标响应对象
    :param pairs (List): (header 名, 值) 列表

    :return Response: 同一响应对象，便于链式使用
    """
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in pairs]
    lower_names = {k for k, _ in raw}
    body = getattr(response, "body", None)
    if b"content-length" not in lower_names and isinstance(body, bytes):
        raw.append((b"content-length", str(len(body)).encode("latin-1")))
    response.raw_headers = raw
    return response


def _may_return_emby_html_shell(path: str) -> bool:
    """
    判断路径是否可能返回 Emby Web 的 HTML 壳（用于是否走缓冲并注入脚本）

    :param path: 请求路径
    :return: 若可能为 HTML 页面则 True
    """
    pl = path.lower()
    if "playbackinfo" in pl:
        return False
    if path == "/" or path == "":
        return True
    if path.startswith("/web/") or path == "/web":
        return True
    if path.endswith(".html") or path.endswith(".htm"):
        return True
    if path.startswith(("/emby/", "/items/", "/videos/", "/audio/", "/sync/")):
        return False
    last = path.rsplit("/", 1)[-1]
    if "." not in last:
        return True
    return False


def _inject_scripts_into_html(html: str, external_player_script: str = "") -> str | None:
    """
    在 HTML 的 head 中注入 crossOrigin 拦截脚本和外部播放器脚本；已注入则跳过

    :param html: 页面 HTML 文本
    :param external_player_script: 外部播放器脚本内容
    :return: 注入后的 HTML；无需修改时返回 None
    """
    scripts = ""
    if CROSS_ORIGIN_INTERCEPT_MARKER not in html:
        scripts += CROSS_ORIGIN_INTERCEPT_SCRIPT
    if external_player_script and EXTERNAL_PLAYER_MARKER not in html:
        scripts += external_player_script
    if not scripts:
        return None
    m = re_search(r"</head>", html, IGNORECASE)
    if m:
        i = m.start()
        return html[:i] + scripts + html[i:]
    m2 = re_search(r"<head[^>]*>", html, IGNORECASE)
    if m2:
        return html[: m2.end()] + scripts + html[m2.end() :]
    return None


def _media_sources_indicate_strm(data: dict[str, Any]) -> bool:
    """
    根据 PlaybackInfo JSON 判断是否为 STRM
    Emby 解析 .strm 后 Path 变为文件内的实际地址（HTTP URL），
    因此通过 Protocol=Http + IsRemote=True 来识别

    :param data: PlaybackInfo 解析后的字典
    :return: 若任一 MediaSource 为 STRM 解析后的远程源则为 True
    """
    sources = data.get("MediaSources")
    if not isinstance(sources, list):
        return False
    for ms in sources:
        if not isinstance(ms, dict):
            continue
        if ms.get("IsRemote") is True and ms.get("Protocol") == "Http":
            return True
    return False


def _media_sources_match_pin_rules(
    data: dict[str, Any], rules: List[Tuple[str, str]]
) -> bool:
    """
    判断 PlaybackInfo 中是否有 MediaSource 的 Path 经 pin_rules 替换后变为 HTTP URL

    :param data: PlaybackInfo 解析后的字典
    :param rules: 顶置路径规则列表
    :return: 若任一 MediaSource 命中规则且替换结果为 HTTP URL 则为 True
    """
    if not rules:
        return False
    sources = data.get("MediaSources")
    if not isinstance(sources, list):
        return False
    for ms in sources:
        if not isinstance(ms, dict):
            continue
        path = ms.get("Path")
        if not isinstance(path, str):
            continue
        replaced = _apply_pin_rules(path, rules)
        if replaced != path and replaced.startswith(("http://", "https://")):
            return True
    return False


def _apply_force_direct_play_to_media_sources(
    data: dict[str, Any], item_id: str
) -> None:
    """
    对 PlaybackInfo 中所有 MediaSource 强制 DirectPlay，去掉转码相关字段，
    并将 DirectStreamUrl 设为相对直链（相对当前连接主机，便于走代理 302）

    :param data: PlaybackInfo 字典（就地修改）
    :param item_id: 条目 ID，用于拼 /videos 或 /audio 下的 stream 路径
    """
    sources = data.get("MediaSources")
    if not isinstance(sources, list):
        return
    for ms in sources:
        if not isinstance(ms, dict):
            continue
        ms["SupportsDirectPlay"] = True
        ms["SupportsDirectStream"] = True
        ms["SupportsTranscoding"] = False
        for key in (
            "TranscodingUrl",
            "TranscodingContainer",
            "TranscodingSubProtocol",
        ):
            ms.pop(key, None)
        ms.pop("DirectStreamUrl", None)
        sid = ms.get("Id", "")
        if not isinstance(sid, str) or not sid:
            continue
        qsid = quote(sid, safe="")
        if str(ms.get("Type", "")).lower() == "audio":
            ms["DirectStreamUrl"] = (
                f"/audio/{item_id}/stream?Static=true&MediaSourceId={qsid}"
            )
        else:
            ms["DirectStreamUrl"] = (
                f"/videos/{item_id}/stream?Static=true&MediaSourceId={qsid}"
            )


def _build_forward_headers(request: Request) -> dict[str, str]:
    """
    构建转发请求头，排除 host 和 hop-by-hop 头

    :param request: 当前请求
    :return: 用于转发的请求头字典
    """
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
        and k.lower() not in ("host", "content-length")
    }


def _real_client_ip(request: Request) -> str:
    """
    获取真实客户端 IP

    若 MoviePilot 前置有反向代理（nginx/Cloudflare 等），
    `request.client.host` 会是上游代理的 IP，导致局域网内所有用户看起来
    都来自同一 IP。优先读取 `X-Forwarded-For` / `X-Real-IP` 恢复真实 IP

    注意：结果仅供日志与缓存 key 使用，不得用于安全判定（见 `_is_private_source`），
    因为这两个请求头可由客户端任意伪造

    :param request: 当前请求
    :return: 客户端 IP 字符串，取不到时返回空串
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",", 1)[0].strip()
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    return request.client.host if request.client else ""


def _is_private_source(request: Request) -> bool:
    """
    判断请求来源是否属于私有网段，用于流代理模式的局域网硬门禁

    安全判定一律以连接层 IP（request.client.host）为准，不读取
    X-Forwarded-For / X-Real-IP，避免伪造请求头绕过门禁。
    `ipaddress.is_private` 覆盖 RFC 1918、环回、ULA 与运营商 NAT 网段；
    无法解析的 IP 一律按非私有处理

    :param request: 当前请求
    :return: 来源属于私有网段返回 True，否则 False
    """
    ip = request.client.host if request.client else ""
    if not ip:
        return False
    try:
        return ip_address(ip).is_private
    except ValueError:
        return False


def _playback_user_key(request: Request, item_id: str) -> tuple[str, str, str]:
    """
    构造用于关联 PlaybackInfo → Stream 的复合 key

    Stream 请求通常不携带任何 Emby 认证字段，但与其前置的 PlaybackInfo
    来自同一浏览器/设备，因此可以用 (client_ip, user_agent, item_id)
    三元组作为稳定关联键。同一设备下多用户互不影响：不同用户的
    PlaybackInfo 分属不同 item_id 的播放动作，不会相互覆盖

    :param request: 当前请求
    :param item_id: 媒体项 ID
    :return: 复合 key
    """
    ip = _real_client_ip(request)
    ua = request.headers.get("user-agent", "")
    return ip, ua, item_id


def _header_hash(request: Request) -> str:
    """
    对缓存 key 白名单内的请求头做稳定序列化并哈希，用于区分认证/设备

    :param request: 当前请求
    :return: 十六进制摘要字符串
    """
    parts = []
    for name in sorted(CACHE_KEY_HEADERS):
        value = request.headers.get(name)
        if value is not None:
            parts.append(f"{name}:{value}")
    return sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _apply_pin_rules(url_or_path: str, rules: List[Tuple[str, str]]) -> str:
    """
    对 PlaybackInfo 返回的 Path（URL 或路径）应用顶置规则：匹配前缀则替换为目标 URL

    :param url_or_path: 完整 URL 或路径字符串
    :param rules: 顶置规则列表 (路径前缀, 目标URL)
    :return: 替换后的 URL，未命中则返回原串
    """
    if not url_or_path or not rules:
        return url_or_path
    path_component: str
    original_query: str = ""
    if url_or_path.startswith(("http://", "https://")):
        parsed = urlparse(url_or_path)
        path_component = parsed.path or "/"
        original_query = parsed.query or ""
    else:
        path_component = (
            url_or_path if url_or_path.startswith("/") else "/" + url_or_path
        )
    for path_prefix, target_url in rules:
        if path_component != path_prefix and not path_component.startswith(
            path_prefix + "/"
        ):
            continue
        suffix = path_component[len(path_prefix) :].lstrip("/")
        base = target_url.rstrip("/")
        new_url = base + ("/" + suffix if suffix else "")
        if original_query:
            new_url += "?" + original_query
        return new_url
    return url_or_path


def _current_port(request: Request) -> int:
    """
    从请求中解析当前代理端口（供客户端连回），优先 X-Forwarded-Port

    :param request: 当前请求
    :return: 端口号
    """
    forwarded = request.headers.get("x-forwarded-port")
    if forwarded:
        try:
            return int(forwarded.split(",")[0].strip())
        except (ValueError, AttributeError):
            pass
    server = request.scope.get("server")
    if server and len(server) >= 2:
        try:
            return int(server[1])
        except (ValueError, TypeError):
            pass
    return 80


def create_app(
    emby_host: str,
    pin_rules: List[Tuple[str, str]] | None = None,
    external_player_url: bool = False,
    external_player_list: List[str] | None = None,
    playback_mode: str = "disabled",
) -> FastAPI:
    """
    创建 Emby 反向代理 FastAPI 应用

    :param emby_host (str): Emby 服务器根地址
    :param pin_rules (List): 顶置路径规则列表 (路径前缀, 目标URL)；命中时先替换再 302
    :param external_player_url (bool): 是否启用外部播放器链接注入
    :param external_player_list (List): 要注入的外部播放器 key 列表；为空时使用全部
    :param playback_mode (str): 播放模式 disabled / redirect / stream_proxy，非法值回落 disabled

    :return FastAPI: 配置好的 FastAPI 应用实例
    """
    emby_host = emby_host.rstrip("/")
    pin_rules = pin_rules or []
    mode_value = (playback_mode or "").strip().lower() if isinstance(playback_mode, str) else ""
    if mode_value in PLAYBACK_MODE_VALUES:
        playback_mode_enum = PlaybackMode(mode_value)
    elif isinstance(playback_mode, bool):
        playback_mode_enum = PlaybackMode.REDIRECT if playback_mode else PlaybackMode.DISABLED
        logger.warning("playback_mode 收到布尔值，已映射为 %s", playback_mode_enum.value)
    else:
        playback_mode_enum = PlaybackMode.DISABLED
        logger.warning("playback_mode 取值非法，回落 disabled: %r", playback_mode)
    if external_player_url:
        _player_keys = (
            [k for k in external_player_list if k in EXTERNAL_PLAYERS]
            if external_player_list
            else ALL_EXTERNAL_PLAYER_KEYS
        )
    else:
        _player_keys = []

    _external_player_script = build_external_player_script(_player_keys)

    async def _strip_credentials_cross_host(request: HttpxRequest) -> None:
        """
        httpx request hook：跟随重定向跨主机时剥离凭据头，
        防止 Emby token / Cookie 随重定向链外发到第三方主机

        :param request (httpx.Request): 本次将发出的请求（含每一跳）
        """
        origin_host = request.extensions.get("_origin_host")
        current_host = request.url.host or ""
        if origin_host is None:
            request.extensions["_origin_host"] = current_host
            return
        if origin_host == current_host:
            return
        for header in ("authorization", "cookie", "x-emby-token", "x-emby-authorization"):
            request.headers.pop(header, None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """
        FastAPI 生命周期管理器：创建共享 httpx 客户端及缓存，关闭时清理资源
        """
        limits = Limits(max_keepalive_connections=20, keepalive_expiry=30.0)
        # 媒体流经此客户端长距转发，读超时须放开，否则 CDN 停顿即中断且仅有状态码可见
        media_timeout = Timeout(connect=10.0, read=None, write=None, pool=10.0)
        app.state.http_client_follow = AsyncClient(
            follow_redirects=True,
            limits=limits,
            timeout=media_timeout,
            event_hooks={"request": [_strip_credentials_cross_host]},
        )
        app.state.http_client_no_follow = AsyncClient(
            follow_redirects=False, limits=limits, timeout=media_timeout
        )
        app.state.playback_url_cache = AsyncTtlCache(ttl=PLAYBACK_URL_CACHE_TTL_SECONDS, max_size=PLAYBACK_URL_CACHE_MAX_SIZE)
        app.state.strm_source_cache = AsyncTtlCache(ttl=PLAYBACK_STRM_CACHE_TTL_SECONDS, max_size=500)
        app.state.playback_user_cache = AsyncTtlCache(ttl=PLAYBACK_USER_CACHE_TTL_SECONDS, max_size=500)
        app.state.playback_url_key_lock = AsyncKeyLock()
        yield
        await app.state.http_client_follow.aclose()
        await app.state.http_client_no_follow.aclose()

    app = FastAPI(lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def path_to_lower_middleware(request: Request, call_next):
        """
        FastAPI 中间件：将 Emby API 路径中的大写转小写，提升路由匹配兼容性
        """
        path = request.scope["path"]
        lower = path.lower()
        _api_prefixes = (
            "/emby/videos/",
            "/emby/audio/",
            "/emby/items/",
            "/emby/sync/",
            "/emby/system/",
            "/emby/users/",
            "/videos/",
            "/audio/",
            "/items/",
            "/sync/",
            "/system/",
            "/users/",
        )
        if lower.startswith(_api_prefixes):
            request.scope["path"] = lower
        return await call_next(request)

    async def _handle_media(
        request: Request, item_id: str, name: str = ""
    ) -> RedirectResponse | StreamingResponse | JSONResponse | Response:
        """
        处理媒体路由：按播放模式分流——302 直链解析、流代理取流（仅私有网段）
        或通用反向代理回退

        :param request: 当前请求
        :param item_id: 媒体项 ID
        :param name: 路径末段名称，用于识别非媒体辅助请求
        :return: 重定向、流式响应或错误 JSON
        """
        if name.lower() in NON_MEDIA_NAMES:
            return await _reverse_proxy(request)
        api_key = extract_api_key(request)
        if playback_mode_enum is PlaybackMode.STREAM_PROXY:
            if not _is_private_source(request):
                logger.warning(
                    "流代理拒绝非私有网段来源: item_id=%s ip=%s",
                    item_id, _real_client_ip(request),
                )
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "Forbidden",
                        "detail": "流代理模式仅服务私有网段设备",
                    },
                )
            final_url, resolved = await _resolve_media_target(item_id, api_key, request)
            if final_url is None:
                return await _reverse_proxy(request)
            return await _proxy_stream(request, item_id, final_url, resolved)
        resp = await _try_media_response(item_id, api_key, request)
        if resp:
            return resp
        return await _reverse_proxy(request)

    async def _resolve_redirect(
        client: AsyncClient,
        url: str,
        headers: dict[str, str],
        user_id: str | None = None,
    ) -> Tuple[str, bool]:
        """
        通过单次 HEAD 请求跟随 115 跳转，返回 CDN 直链

        使用 follow_redirects=False 手动读取 Location 头，HEAD 请求只落在
        重定向链的起点 URL，不会跟随到 CDN，避免 CDN 收到与播放器
        GET 请求方法不一致的 HEAD 请求

        :param client: httpx 客户端（建议 follow_redirects=False）
        :param url: 起始 URL
        :param headers: 请求头
        :param user_id: Emby 用户 ID，可为 None

        :return Tuple: (最终 URL，是否成功解析)；失败时返回原始 url 并标记未解析
        """
        if user_id:
            headers = {**headers, "X-Emby-UserId": user_id}
        max_attempts = len(REDIRECT_RESOLVE_TIMEOUTS)
        for attempt, (connect_timeout, read_timeout) in enumerate(
            REDIRECT_RESOLVE_TIMEOUTS, 1
        ):
            try:
                resp = await client.head(
                    url,
                    headers=headers,
                    timeout=Timeout(read_timeout, connect=connect_timeout),
                )
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if location:
                        # 相对 Location 必须按本次请求 URL 绝对化，否则下发后基址错位
                        return urljoin(str(resp.url), location), True
                    logger.warning(
                        "重定向响应缺少 Location 头 url=%s status=%s",
                        url, resp.status_code,
                    )
                    return url, False
                if resp.status_code >= 400:
                    # 非重定向且非 2xx 说明地址或权限已坏，按未解析短缓存处理
                    return url, False
                return str(resp.url), True
            except TimeoutException as e:
                if attempt >= max_attempts:
                    logger.warning(
                        "解析重定向超时，使用原始 URL: %s (%s)",
                        url,
                        type(e).__name__,
                        exc_info=True,
                    )
                    return url, False
                logger.info(
                    "解析重定向超时，准备重试: url=%s, attempt=%s/%s, connect=%ss, read=%ss, error=%s",
                    url,
                    attempt,
                    max_attempts,
                    connect_timeout,
                    read_timeout,
                    type(e).__name__,
                )
            except Exception:
                logger.warning("解析重定向失败，使用原始 URL: %s", url, exc_info=True)
                return url, False
        return url, False

    def _with_refresh_marker(url: str) -> str:
        """给跳转地址加刷新标记，让 302 服务跳过旧直链缓存重新签发"""
        if "refresh=" in url or "pickcode=" not in url:
            return url
        return f"{url}{'&' if '?' in url else '?'}refresh=1"

    def _build_302_redirect(url: str) -> RedirectResponse:
        """
        构建 302 重定向响应，让客户端直连 CDN

        URL 中的非 ASCII 字符做百分号编码，避免某些 HTTP 客户端无法解析

        :param url (str): CDN 直链 URL

        :return RedirectResponse: 302 重定向响应
        """
        encoded_url = quote(url, safe=":/?#@!$&'()*+,;=%")
        return RedirectResponse(url=encoded_url, status_code=302)

    async def _resolve_media_target(
        item_id: str,
        api_key: str | None,
        request: Request,
        *,
        skip_final_url_cache: bool = False,
    ) -> Tuple[str | None, bool]:
        """
        解析媒体项实际播放地址，302 直链与流代理两种模式共用

        三级缓存策略，优先级递减：
          1. playback_url_cache（90s TTL）— 已解析的重定向最终 URL；
             skip_final_url_cache=True（直链失效换新链场景）时跳过本级
          2. PlaybackInfo API 实时查询 — 从 Emby 获取 MediaSource Path
          3. strm_source_cache（300s TTL）— STRM 文件的直链缓存

        :param item_id (str): 媒体项 ID
        :param api_key (str): Emby API Key，可为 None
        :param request (Request): 当前请求
        :param skip_final_url_cache (bool): 跳过已解析 URL 缓存强制重新解析

        :return Tuple: (最终 URL，是否已解析为 CDN 直链)；URL 为 None 表示媒体源无法解析
        """
        media_source_id = request.query_params.get("MediaSourceId") or ""

        # 第一级（用户关联）：从请求头提取用户 ID，用作后续缓存的 key 组成部分
        user_key = _playback_user_key(request, item_id)
        user_cache = request.app.state.playback_user_cache
        user_id: str | None = None
        async with user_cache.lock:
            user_id = user_cache.get(user_key)

        cache_key = (
            item_id,
            media_source_id,
            user_id or "",
            _header_hash(request),
        )
        cache = request.app.state.playback_url_cache
        key_lock = request.app.state.playback_url_key_lock

        # 缓存击穿防护：同一 item_id+MediaSourceId 并发解析只执行一次，
        # 其余请求在锁内二次检查缓存后直接复用结果
        async with key_lock.acquire(cache_key):
            # 第二级：已解析 URL 缓存 — 命中则直接返回，跳过后续查询
            if not skip_final_url_cache:
                cached_entry = None
                async with cache.lock:
                    cached_entry = cache.get(cache_key)
                if cached_entry is not None:
                    # 缓存与返回值同为 (url, resolved)，保留解析失败标志
                    if isinstance(cached_entry, tuple) and len(cached_entry) == 2:
                        cached_url, cached_resolved = cached_entry
                    else:
                        cached_url, cached_resolved = cached_entry, True
                    logger.debug(
                        "媒体地址缓存命中: item_id=%s -> %s (resolved=%s)",
                        item_id, cached_url, cached_resolved,
                    )
                    return cached_url, cached_resolved

            http_path = None

            # 第三级：PlaybackInfo API 实时查询 — 从 Emby 解析媒体源路径
            if api_key:
                url = (
                    f"{emby_host}/Items/{item_id}/PlaybackInfo"
                    f"?X-Emby-Token={quote(api_key, safe='')}"
                )
                client_follow = request.app.state.http_client_follow
                try:
                    resp = await client_follow.post(url, timeout=10)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception:
                    logger.warning(
                        "PlaybackInfo 请求失败: item_id=%s", item_id, exc_info=True
                    )
                    data = {}
                for source in data.get("MediaSources", []):
                    sid = source.get("Id", "")
                    if media_source_id and sid != media_source_id:
                        continue
                    path = source.get("Path", "")
                    path = _apply_pin_rules(path, pin_rules)
                    if path.startswith(("http://", "https://")):
                        http_path = path
                        break

            # 第四级：STRM 源缓存 — PlaybackInfo 未命中时使用
            if not http_path:
                strm_cache = request.app.state.strm_source_cache
                async with strm_cache.lock:
                    sources_map = strm_cache.get(item_id)
                if sources_map:
                    if media_source_id and media_source_id in sources_map:
                        http_path = sources_map[media_source_id]
                    else:
                        http_path = next(iter(sources_map.values()))
                    if http_path:
                        logger.debug("使用 STRM 源缓存: item_id=%s", item_id)

            # 第五级：同一播放请求内此前解析出的源地址。半身后断续传发生在
            # 同一个 request 里，此时 PlaybackInfo 已不可查、各级缓存也可能过期，
            # 用它保证续传永远能重新解析，而不是「resolve returned no target」
            if not http_path:
                http_path = getattr(request.state, "strm_source_url", None)
                if http_path:
                    logger.debug("使用本次请求的 STRM 源地址: item_id=%s", item_id)

            if not http_path:
                return None, False

            # 记录源地址供本次请求后续续传使用（仅内存态，不跨请求）
            request.state.strm_source_url = http_path

            # 解析重定向链：STRM 中的跳转 URL -> 115 CDN 直链
            # 使用 follow_redirects=False 避免实际请求 CDN
            client_no_follow = request.app.state.http_client_no_follow
            fwd_headers = _build_forward_headers(request)
            # 换链/续传场景强制重新签发：否则 302 服务仍返回缓存里的旧地址，
            # 表现为连续 403「换新链」却始终换不到新链
            resolve_url = (
                _with_refresh_marker(http_path)
                if skip_final_url_cache
                else http_path
            )
            final_url, resolved = await _resolve_redirect(
                client_no_follow, resolve_url, fwd_headers, user_id
            )

            # 解析成功回写默认 TTL；解析失败在常规路径只做 5 秒短缓存
            # 避免坏 URL 长期驻留，换新链场景则不写回以免覆盖正常缓存
            async with cache.lock:
                if resolved:
                    cache.put(cache_key, (final_url, True), ttl=None)
                elif not skip_final_url_cache:
                    cache.put(cache_key, (final_url, False), ttl=5)

            logger.debug("媒体地址已解析: item_id=%s -> %s", item_id, final_url)
            return final_url, resolved

    async def _proxy_stream(
        request: Request,
        item_id: str,
        direct_url: str,
        resolved: bool,
    ) -> Response:
        """
        以流代理方式向 115 CDN 取媒体流并流式回传播放端

        重试状态机（仅响应头产出前）：上游连接失败、403/404、5xx、3xx
        或带 Range 得 200 时，以 skip_final_url_cache 重新解析换新链重试，
        次数与总时长受 STREAM_RETRY_* 常量约束。响应头已产出后的半身后断
        （播放端暂停期间 CDN 空闲关链、直链时长到点等）由 stream() 在同一响应内
        按已交付字节换新链续传，受 STREAM_RESUME_* 常量约束；续传未成功时
        就此收尾响应体并记日志，由播放端按截断自行重新发起 Range 请求

        :param request (Request): 当前请求
        :param item_id (str): 媒体项 ID
        :param direct_url (str): 本轮已解析的取流地址
        :param resolved (bool): direct_url 是否已解析为 CDN 直链

        :return Response: 流式回传响应（白名单响应头）；重连耗尽返回 502 JSON
        """
        start = monotonic()
        fwd_headers = _strip_accept_encoding(_build_forward_headers(request))
        req_range = request.headers.get("range")
        method = request.method
        client = request.app.state.http_client_no_follow
        api_key = extract_api_key(request)
        cur_url, cur_resolved = direct_url, resolved
        last_error = "unknown"
        total_attempts = 1 + STREAM_RETRY_MAX

        for attempt in range(1, total_attempts + 1):
            if attempt > 1:
                backoff = STREAM_RETRY_BACKOFF_SECONDS[
                    min(attempt - 2, len(STREAM_RETRY_BACKOFF_SECONDS) - 1)
                ]
                if monotonic() - start + backoff >= STREAM_RETRY_BUDGET_SECONDS:
                    last_error = "retry budget exceeded"
                    break
                await sleep(backoff)
                # 解析链异常不应拖垮整个请求
                try:
                    new_url, cur_resolved = await _resolve_media_target(
                        item_id, api_key, request, skip_final_url_cache=True
                    )
                except Exception:
                    last_error = "resolve raised"
                    logger.warning(
                        "stream proxy re-resolve failed: item_id=%s attempt=%s",
                        item_id, attempt, exc_info=True
                    )
                    continue
                # 换链解析耗时计入预算
                if monotonic() - start >= STREAM_RETRY_BUDGET_SECONDS:
                    if new_url is not None:
                        cur_url = new_url
                        last_error = "retry budget exceeded after resolve"
                        break
                    last_error = "retry budget exceeded"
                    break
                if new_url is None:
                    last_error = "resolve returned no target"
                    continue
                cur_url = new_url

            if not cur_resolved:
                last_error = "unresolved redirect target"
                logger.info(
                    "流代理目标未解析为直链，换新链: item_id=%s attempt=%s",
                    item_id, attempt,
                )
                continue

            logger.info(
                "流代理外发请求头诊断: item_id=%s attempt=%s url=%s headers=%s",
                item_id, attempt, describe_media_url(cur_url),
                describe_headers(fwd_headers),
            )
            try:
                upstream = await client.send(
                    client.build_request(
                        method=method, url=cur_url, headers=fwd_headers
                    ),
                    stream=True,
                )
            except Exception:
                last_error = "upstream connect error"
                logger.warning(
                    "流代理上游连接异常: item_id=%s attempt=%s",
                    item_id, attempt, exc_info=True,
                )
                continue

            status = upstream.status_code
            # 带 Range 得 200：可能是 CDN 返回整段内容（如回退到开头的 bytes=0-）
            # 设计约定重试后仍 200 则原样转发，避免误判为不可用而返回 502
            range_ignored = (
                req_range is not None
                and status == 200
                and not _range_starts_at_zero(req_range)
                and attempt < total_attempts
            )
            retryable = (
                status in (403, 404)
                or status >= 500
                or 300 <= status < 400
                or range_ignored
            )
            if retryable:
                last_error = f"upstream status {status}"
                if status in (403, 404) or status >= 500:
                    try:
                        body_hint = (await upstream.aread())[:200].decode(
                            "utf-8", "replace"
                        ).replace("\r", " ").replace("\n", " ")
                    except Exception:
                        body_hint = "(unreadable)"
                    logger.warning(
                        "流代理拒绝诊断: item_id=%s attempt=%s status=%s url=%s ua=%s refresh=%s body=%s",
                        item_id, attempt, status, describe_media_url(cur_url),
                        (fwd_headers.get("user-agent") or "")[:80],
                        "refresh=" in cur_url, body_hint,
                    )
                await upstream.aclose()
                logger.info(
                    "流代理上游不可用，准备换新链重试: item_id=%s attempt=%s status=%s",
                    item_id, attempt, status,
                )
                continue

            pairs = [
                (k, v)
                for k, v in upstream.headers.items()
                if k.lower() in UPSTREAM_FORWARD_RESPONSE_HEADERS
            ]
            if method == "HEAD":
                await upstream.aclose()
                return _with_raw_headers(Response(status_code=status), pairs)

            # 断点偏移的绝对基准：Range 请求以首个响应的 Content-Range 为准
            # （后缀区间 bytes=-N 无法从请求头取起点），无 Range 的整段请求从 0 计
            first_start = _content_range_start(upstream.headers.get("content-range", ""))
            if first_start is None:
                first_start = _range_start_offset(req_range) if req_range else 0

            async def resume_segment(next_offset: int) -> HttpxResponse | None:
                """
                半身后断后换新直链，按断点偏移重新取一段可续传的流

                退避、换链与建连在 STREAM_RESUME_MAX 次与
                STREAM_RESUME_BUDGET_SECONDS 预算内尝试；仅接受 206 且
                Content-Range 起点恰为断点偏移的响应。403/404/5xx/3xx 等
                按预算换新链再试；200 整段与偏移错位的 206 即刻放弃，
                避免向播放端重复喂已交付过的字节

                :param next_offset (int): 播放端期望的下一个绝对字节偏移

                :return HttpxResponse: 可续传的流式响应，放弃时为 None
                """
                break_start = monotonic()
                last_reason = "unknown"
                for resume_attempt in range(1, STREAM_RESUME_MAX + 1):
                    backoff = STREAM_RESUME_BACKOFF_SECONDS[
                        min(resume_attempt - 1, len(STREAM_RESUME_BACKOFF_SECONDS) - 1)
                    ]
                    if (
                        monotonic() - break_start + backoff
                        >= STREAM_RESUME_BUDGET_SECONDS
                    ):
                        last_reason = "resume budget exceeded"
                        break
                    await sleep(backoff)
                    try:
                        resume_url, resume_resolved = await _resolve_media_target(
                            item_id, api_key, request, skip_final_url_cache=True
                        )
                    except Exception:
                        last_reason = "resolve raised"
                        logger.warning(
                            "流代理续传换链异常: item_id=%s attempt=%s",
                            item_id, resume_attempt, exc_info=True,
                        )
                        continue
                    if resume_url is None or not resume_resolved:
                        last_reason = "resolve returned no target"
                        logger.info(
                            "流代理续传未取到直链，换新链: item_id=%s attempt=%s",
                            item_id, resume_attempt,
                        )
                        continue
                    resume_headers = _build_resume_headers(fwd_headers, next_offset)
                    logger.info(
                        "流代理续传外发请求头诊断: item_id=%s attempt=%s next=%s headers=%s",
                        item_id, resume_attempt, next_offset,
                        describe_headers(resume_headers),
                    )
                    try:
                        seg = await client.send(
                            client.build_request(
                                method="GET",
                                url=resume_url,
                                headers=resume_headers,
                            ),
                            stream=True,
                        )
                    except Exception:
                        last_reason = "upstream connect error"
                        logger.warning(
                            "流代理续传上游连接异常: item_id=%s attempt=%s",
                            item_id, resume_attempt, exc_info=True,
                        )
                        continue
                    actual_start = _content_range_start(
                        seg.headers.get("content-range", "")
                    )
                    if seg.status_code == 206 and actual_start == next_offset:
                        logger.info(
                            "流代理已换新链续传: item_id=%s next=%s attempt=%s",
                            item_id, next_offset, resume_attempt,
                        )
                        return seg
                    last_reason = f"resume status {seg.status_code} start {actual_start}"
                    if seg.status_code in (403, 404) or seg.status_code >= 500:
                        logger.warning(
                            "流代理续传拒绝诊断: item_id=%s attempt=%s status=%s url=%s ua=%s refresh=%s next=%s",
                            item_id, resume_attempt, seg.status_code,
                            describe_media_url(resume_url),
                            (fwd_headers.get("user-agent") or "")[:80],
                            "refresh=" in resume_url, next_offset,
                        )
                    # 200 整段与偏移错位的 206 说明 CDN 给不出断点之后的字节，再试只会
                    # 更早重复喂给播放端已交付过的内容；其余状态码（直链过期 403 等）
                    # 属于新链可能修好的场景，按预算继续换新链
                    give_up = seg.status_code in (200, 206)
                    await seg.aclose()
                    if give_up:
                        break
                logger.warning(
                    "流代理续传放弃: item_id=%s next=%s reason=%s",
                    item_id, next_offset, last_reason,
                )
                return None

            async def stream():
                """
                流式读取上游响应逐块输出，上游中断时在同一响应内换新链补传

                响应头已产出，状态码与 Content-Length 无法再改，只能从断点偏移续传；
                续传未成功则就此收尾响应体并记日志，由播放端按截断自行重新发起请求

                :yields bytes: 上游媒体流字节块
                """
                seg = upstream
                seg_next = first_start
                resumed = 0
                while True:
                    try:
                        async for chunk in seg.aiter_bytes(chunk_size=65536):
                            if chunk:
                                seg_next += len(chunk)
                            yield chunk
                        return
                    except Exception:
                        logger.error(
                            "流代理转发中断: item_id=%s next=%s",
                            item_id, seg_next, exc_info=True,
                        )
                    finally:
                        await seg.aclose()

                    # 走到这里说明读流已抛异常（取消与客户端断开属 BaseException，
                    # 经 finally 关闭上游后原样上抛），只能在同一响应内续传
                    if resumed >= STREAM_RESUME_TOTAL_MAX:
                        logger.error(
                            "流代理续传总次数越限，中断回传: item_id=%s resumed=%s",
                            item_id, resumed,
                        )
                        return
                    seg = await resume_segment(seg_next)
                    if seg is None:
                        logger.error(
                            "流代理续传未成功，中断回传: item_id=%s next=%s",
                            item_id, seg_next,
                        )
                        return
                    resumed += 1

            return _with_raw_headers(
                StreamingResponse(stream(), status_code=status), pairs
            )

        logger.error(
            "流代理重连耗尽: item_id=%s method=%s attempts=%s last=%s ip=%s",
            item_id, method, total_attempts, last_error, _real_client_ip(request),
        )
        return JSONResponse(
            status_code=502,
            content={
                "error": "Bad Gateway",
                "detail": "媒体流获取失败：向 115 重连重试已耗尽",
            },
        )

    async def _try_media_response(
        item_id: str, api_key: str | None, request: Request
    ) -> Response | None:
        """
        302 直链模式下解析媒体项并返回 302 重定向响应

        解析行为（三级缓存、击穿防护、失败短缓存）全部由
        _resolve_media_target 承担并回写缓存；非 redirect 模式直接返回 None

        :param item_id: 媒体项 ID
        :param api_key: Emby API Key，可为 None
        :param request: 当前请求

        :return Response: 302 重定向响应，或 None 表示回退到反向代理
        """
        if playback_mode_enum is not PlaybackMode.REDIRECT:
            return None

        final_url, resolved = await _resolve_media_target(item_id, api_key, request)
        # 解析失败时不能把未解析的跳转地址当作 115 直链下发给客户端
        if final_url is None or not resolved:
            return None

        logger.debug("302 直链: item_id=%s -> %s", item_id, final_url)
        return _build_302_redirect(final_url)

    for route in MEDIA_ROUTES:
        app.api_route(route, methods=["GET", "HEAD"], response_model=None)(
            _handle_media
        )

    async def _ws_proxy(ws_client: WebSocket) -> None:
        """
        双向代理客户端 WebSocket 与 Emby 后端 WebSocket

        :param ws_client: 客户端 WebSocket 连接
        """
        await ws_client.accept()
        parsed = urlparse(emby_host)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        qs = str(ws_client.scope.get("query_string", b""), "utf-8")
        backend_url = f"{scheme}://{parsed.netloc}/embywebsocket"
        if qs:
            backend_url += f"?{qs}"

        try:
            async with connect(backend_url) as ws_backend:

                async def client_to_backend() -> None:
                    """
                    WebSocket 客户端→后端：转发客户端消息到后端

                    使用 receive() 统一处理文本与二进制帧
                    """
                    try:
                        while True:
                            msg = await ws_client.receive()
                            if msg.get("type") == "websocket.disconnect":
                                break
                            if (text := msg.get("text")) is not None:
                                await ws_backend.send(text)
                            elif (data := msg.get("bytes")) is not None:
                                await ws_backend.send(data)
                    except WebSocketDisconnect:
                        pass
                    except Exception as e:
                        logger.debug("WebSocket 客户端->后端 结束: %s", e)

                async def backend_to_client() -> None:
                    """
                    WebSocket 后端→客户端：转发后端消息到客户端
                    """
                    try:
                        async for msg in ws_backend:
                            if isinstance(msg, str):
                                await ws_client.send_text(msg)
                            else:
                                await ws_client.send_bytes(msg)
                    except Exception as e:
                        logger.debug("WebSocket 后端->客户端 结束: %s", e)

                tasks = [
                    create_task(client_to_backend()),
                    create_task(backend_to_client()),
                ]
                done, pending = await wait(
                    tasks, return_when=FIRST_COMPLETED
                )
                for task in done:
                    exc = task.exception()
                    if exc:
                        logger.debug("WebSocket 转发任务异常: %s", exc)
                # 单向断开即双向取消，避免协程泄漏悬挂在对方连接上
                for task in pending:
                    task.cancel()
                await gather(*pending, return_exceptions=True)
        except Exception:
            logger.warning("WebSocket 代理异常", exc_info=True)
        finally:
            try:
                await ws_client.close()
            except Exception as e:
                logger.debug("关闭客户端 WebSocket 时异常: %s", e)

    app.websocket("/embywebsocket")(_ws_proxy)
    app.websocket("/emby/embywebsocket")(_ws_proxy)

    async def _system_info_handler(
        request: Request,
    ) -> JSONResponse | StreamingResponse:
        """
        代理 /emby/system/info 与 /system/info，并改写响应中的端口，使客户端连回代理而非后端

        :param request: 当前请求
        :return: 改写后的 JSON 或 502 错误
        """
        path = request.scope.get("path", "/")
        qs = str(request.url.query)
        target_url = f"{emby_host}{path}"
        if qs:
            target_url += f"?{qs}"
        headers = _build_forward_headers(request)
        client = request.app.state.http_client_follow
        try:
            resp = await client.request(
                request.method,
                target_url,
                headers=headers,
                timeout=10,
            )
        except Exception as e:
            err_msg = str(e) or type(e).__name__
            logger.warning(
                "System/Info 请求失败: %s — %s", target_url, err_msg, exc_info=True
            )
            return JSONResponse(
                status_code=502,
                content={
                    "error": "Bad Gateway",
                    "detail": "请求失败",
                },
            )
        if resp.status_code != 200:
            logger.warning(
                "System/Info 后端返回非 200: status=%s, url=%s",
                resp.status_code,
                target_url,
            )
            return JSONResponse(
                status_code=502,
                content={
                    "error": "Bad Gateway",
                    "detail": "System/Info 请求失败",
                },
            )
        try:
            body = resp.json()
        except Exception:
            logger.warning("System/Info 响应非 JSON: %s", target_url, exc_info=True)
            return JSONResponse(
                status_code=502,
                content={
                    "error": "Bad Gateway",
                    "detail": "System/Info 请求失败",
                },
            )
        origin_port = body.get("WebSocketPortNumber")
        if origin_port is not None:
            current_port = _current_port(request)
            try:
                origin_port = int(origin_port)
            except (TypeError, ValueError):
                origin_port = None
            if origin_port is not None:
                body["WebSocketPortNumber"] = current_port
                if body.get("HttpServerPortNumber") is not None:
                    body["HttpServerPortNumber"] = current_port
                for key in ("LocalAddresses", "RemoteAddresses"):
                    if isinstance(body.get(key), list):
                        body[key] = [
                            str(s).replace(str(origin_port), str(current_port))
                            for s in body[key]
                        ]
                for key in ("LocalAddress", "WanAddress"):
                    if body.get(key) is not None:
                        body[key] = str(body[key]).replace(
                            str(origin_port), str(current_port)
                        )
        pairs = _filter_response_headers(
            resp,
            drop=frozenset(("content-length", "content-encoding")) | RESPONSE_VALIDATOR_HEADERS,
        )
        return _with_raw_headers(
            JSONResponse(status_code=200, content=body), pairs
        )

    for _path in ("/emby/system/info", "/system/info"):
        app.api_route(
            _path,
            methods=["GET", "HEAD"],
            response_model=None,
        )(_system_info_handler)

    async def _playback_info_strm_direct_play(
        request: Request, item_id: str
    ) -> JSONResponse | Response:
        """
        代理 Items/PlaybackInfo：若 MediaSource.Path 为 .strm，则强制 DirectPlay，
        避免浏览器因编码能力走 HLS 转码，使 302 直链失效

        :param request: 当前请求
        :param item_id: 路径中的条目 ID
        :return: 改写后的 JSON 或透传响应
        """
        path = request.scope.get("path", "/")
        qs = str(request.url.query)
        target_url = f"{emby_host}{path}"
        if qs:
            target_url += f"?{qs}"

        headers = _strip_accept_encoding(_build_forward_headers(request))
        body = await _read_request_body_safe(request)
        if body is None:
            return Response(status_code=204, content=b"")
        client = request.app.state.http_client_follow

        def _resp_headers_from_httpx(r: HttpxResponse) -> list[tuple[str, str]]:
            return _filter_response_headers(
                r, drop=("content-encoding", "content-length")
            )

        if request.method == "HEAD":
            try:
                resp = await client.request(
                    "HEAD",
                    target_url,
                    headers=headers,
                    timeout=60.0,
                )
            except Exception:
                logger.warning("PlaybackInfo HEAD 失败: %s", target_url, exc_info=True)
                return JSONResponse(
                    status_code=502,
                    content={"error": "Bad Gateway"},
                )
            return _with_raw_headers(
                Response(content=b"", status_code=resp.status_code),
                _resp_headers_from_httpx(resp),
            )

        try:
            resp = await client.request(
                request.method,
                target_url,
                headers=headers,
                content=body if body else None,
                timeout=120.0,
            )
        except Exception:
            logger.warning("PlaybackInfo 请求失败: %s", target_url, exc_info=True)
            return JSONResponse(
                status_code=502,
                content={
                    "error": "Bad Gateway",
                    "detail": "无法连接到媒体服务器",
                },
            )

        if resp.status_code != 200:
            return _with_raw_headers(
                Response(content=resp.content, status_code=resp.status_code),
                _resp_headers_from_httpx(resp),
            )

        try:
            data = resp.json()
        except Exception:
            logger.warning("PlaybackInfo 响应非 JSON: %s", target_url, exc_info=True)
            return _with_raw_headers(
                Response(content=resp.content, status_code=resp.status_code),
                _resp_headers_from_httpx(resp),
            )

        if not isinstance(data, dict):
            return _with_raw_headers(
                JSONResponse(content=data, status_code=200),
                _resp_headers_from_httpx(resp),
            )

        is_strm = _media_sources_indicate_strm(data)
        is_pin = _media_sources_match_pin_rules(data, pin_rules)
        if not is_strm and not is_pin:
            return _with_raw_headers(
                JSONResponse(content=data, status_code=200),
                _resp_headers_from_httpx(resp),
            )

        _apply_force_direct_play_to_media_sources(data, item_id)

        strm_sources: dict[str, str] = {}
        for ms in data.get("MediaSources", []):
            if not isinstance(ms, dict):
                continue
            ms_path = ms.get("Path", "")
            ms_path_applied = _apply_pin_rules(ms_path, pin_rules)
            if ms_path_applied.startswith(("http://", "https://")):
                strm_sources[ms.get("Id", "")] = ms_path_applied
        if strm_sources:
            strm_cache = request.app.state.strm_source_cache
            async with strm_cache.lock:
                strm_cache.put(item_id, strm_sources)

        if playback_mode_enum is PlaybackMode.REDIRECT:
            fwd_headers = _build_forward_headers(request)
            client_no_follow = request.app.state.http_client_no_follow
            for ms in data.get("MediaSources", []):
                if not isinstance(ms, dict):
                    continue
                ms_path = ms.get("Path", "")
                if ms_path.startswith(("http://", "https://")):
                    final_url, resolved_ok = await _resolve_redirect(
                        client_no_follow, ms_path, fwd_headers
                    )
                    if resolved_ok:
                        ms["Path"] = final_url
                        logger.info(
                            "302 直链下发诊断: item_id=%s url=%s ua=%s headers=%s",
                            item_id, describe_media_url(final_url),
                            (fwd_headers.get("user-agent") or "")[:80],
                            describe_headers(fwd_headers),
                        )
                    else:
                        logger.info(
                            "302 直链解析失败诊断: item_id=%s url=%s ua=%s",
                            item_id, describe_media_url(ms_path),
                            (fwd_headers.get("user-agent") or "")[:80],
                        )

        uid = request.query_params.get("UserId")
        if uid:
            user_key = _playback_user_key(request, item_id)
            user_cache = request.app.state.playback_user_cache
            async with user_cache.lock:
                user_cache.put(user_key, uid)

        reason = "STRM" if is_strm else "路径替换"
        logger.info(
            "PlaybackInfo: %s 已强制 DirectPlay item_id=%s",
            reason,
            item_id,
        )
        return _with_raw_headers(
            JSONResponse(content=data, status_code=200),
            [
                (k, v)
                for k, v in _resp_headers_from_httpx(resp)
                if k.lower() not in RESPONSE_VALIDATOR_HEADERS
            ],
        )

    for _playback_path in (
        "/items/{item_id}/playbackinfo",
        "/emby/items/{item_id}/playbackinfo",
    ):
        app.api_route(
            _playback_path,
            methods=["GET", "POST", "HEAD"],
            response_model=None,
        )(_playback_info_strm_direct_play)

    async def _reverse_proxy_html_inject(request: Request) -> Response | JSONResponse:
        """
        对可能返回 HTML 的 GET 请求整包拉取，注入 crossOrigin 拦截脚本后再返回

        :param request: 当前请求（须为 GET）
        :return: 响应体或 502 JSON
        """
        path = request.scope.get("path", "/")
        qs = str(request.url.query)
        target_url = f"{emby_host}{path}"
        if qs:
            target_url += f"?{qs}"

        headers = _strip_accept_encoding(_build_forward_headers(request))
        body = await _read_request_body_safe(request)
        if body is None:
            return Response(status_code=204, content=b"")
        client = request.app.state.http_client_no_follow
        try:
            resp = await client.request(
                request.method,
                target_url,
                headers=headers,
                content=body if body else None,
                timeout=120.0,
            )
        except Exception:
            logger.warning("无法连接到 Emby: %s", target_url, exc_info=True)
            return JSONResponse(
                status_code=502,
                content={
                    "error": "Bad Gateway",
                    "detail": "无法连接到媒体服务器",
                },
            )

        ct = (resp.headers.get("content-type") or "").lower()
        raw = resp.content
        out = raw
        if resp.status_code == 200 and "text/html" in ct:
            try:
                html = resp.text
            except Exception:
                html = raw.decode("utf-8", errors="replace")
            injected = _inject_scripts_into_html(html, _external_player_script)
            if injected is not None:
                out = injected.encode("utf-8")
                logger.info("已在 HTML 注入脚本: path=%s", path)

        pairs = _filter_response_headers(
            resp, drop=("content-encoding", "content-length")
        )
        if out != raw and resp.status_code == 200 and "text/html" in ct:
            pairs = [
                (k, v)
                for k, v in pairs
                if k.lower() not in RESPONSE_VALIDATOR_HEADERS
            ]
            pairs.extend(
                [
                    ("cache-control", "no-cache, no-store, must-revalidate"),
                    ("pragma", "no-cache"),
                    ("expires", "0"),
                ]
            )

        return _with_raw_headers(
            Response(content=out, status_code=resp.status_code),
            pairs,
        )

    async def _reverse_proxy(
        request: Request,
    ) -> StreamingResponse | JSONResponse | Response:
        """
        将请求反向代理到 Emby 服务器并流式返回响应

        :param request: 当前请求
        :return: 流式响应或 502 JSON 错误
        """
        path = request.scope.get("path", "/")
        if request.method == "GET" and _may_return_emby_html_shell(path):
            return await _reverse_proxy_html_inject(request)

        qs = str(request.url.query)
        target_url = f"{emby_host}{path}"
        if qs:
            target_url += f"?{qs}"

        headers = _build_forward_headers(request)
        body = await _read_request_body_safe(request)
        if body is None:
            return Response(status_code=204, content=b"")

        client = request.app.state.http_client_no_follow
        try:
            req = client.build_request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body if body else None,
            )
            resp = await client.send(req, stream=True)
        except Exception:
            logger.warning("无法连接到 Emby: %s", target_url, exc_info=True)
            return JSONResponse(
                status_code=502,
                content={
                    "error": "Bad Gateway",
                    "detail": "无法连接到媒体服务器",
                },
            )

        drop_headers = {"content-encoding"}
        if resp.headers.get("content-encoding"):
            drop_headers.add("content-length")
        resp_raw_headers = _filter_response_headers(resp, drop=drop_headers)

        async def stream():
            """
            流式读取 httpx 响应并逐块输出，完成后关闭响应；中断时留日志便于排查播放卡死
            """
            try:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    yield chunk
            except Exception:
                logger.error("转发流中断: %s", target_url, exc_info=True)
                raise
            finally:
                await resp.aclose()

        return _with_raw_headers(
            StreamingResponse(stream(), status_code=resp.status_code),
            resp_raw_headers,
        )

    def _patch_js_response_headers(
        resp: HttpxResponse, extra: dict[str, str] | None = None
    ) -> list[tuple[str, str]]:
        """
        从 httpx 响应构造转发头列表，并可选附加 Cache-Control 等

        :param resp: httpx Response
        :param extra: 额外头

        :return List: (header 名, 值) 列表
        """
        pairs = _filter_response_headers(
            resp, drop=("content-encoding", "content-length")
        )
        if extra:
            pairs.extend(extra.items())
        return pairs

    async def _patch_basehtmlplayer_js(request: Request) -> JSONResponse | Response:
        """
        拦截 htmlvideoplayer/basehtmlplayer.js，将 getCrossOriginValue 相关逻辑改为不设置 crossorigin

        :param request: 当前请求
        :return: 修补后的 JS 或 502
        """
        path = request.scope.get("path", "/")
        qs = str(request.url.query)
        target_url = f"{emby_host}{path}"
        if qs:
            target_url += f"?{qs}"
        headers = _strip_accept_encoding(_build_forward_headers(request))
        client = request.app.state.http_client_follow
        try:
            resp = await client.get(target_url, headers=headers, timeout=15)
        except Exception:
            logger.warning("获取 basehtmlplayer.js 失败: %s", target_url, exc_info=True)
            return JSONResponse(status_code=502, content={"error": "Bad Gateway"})

        if resp.status_code != 200:
            return _with_raw_headers(
                Response(content=resp.content, status_code=resp.status_code),
                _patch_js_response_headers(resp),
            )

        content = resp.text
        original = content
        patched = CROSS_ORIGIN_VALUE_RE.sub("null", content)
        if patched != original:
            logger.info(
                "已修补 basehtmlplayer.js: getCrossOriginValue 三元表达式 -> null"
            )
        elif "getCrossOriginValue" in original:
            logger.info(
                "basehtmlplayer.js: 精确正则未命中，尝试将 anonymous 替换为 null"
            )
            patched = original.replace('"anonymous"', "null").replace(
                "'anonymous'", "null"
            )

        extra: dict[str, str] = {
            "content-type": "application/javascript; charset=utf-8",
        }
        if patched != original:
            extra["cache-control"] = "no-cache, no-store, must-revalidate"
            extra["pragma"] = "no-cache"
            extra["expires"] = "0"
        return _with_raw_headers(
            Response(content=patched.encode("utf-8"), status_code=resp.status_code),
            _patch_js_response_headers(resp, extra=extra),
        )

    async def _patch_plugin_js(request: Request) -> JSONResponse | Response:
        """
        拦截 htmlvideoplayer/plugin.js，去除 crossOrigin 赋值，
        使浏览器播放器在 302 重定向时不触发 CORS 预检

        :param request: 当前请求
        :return: 修补后的 JS 响应或 502 错误 JSON
        """
        path = request.scope.get("path", "/")
        qs = str(request.url.query)
        target_url = f"{emby_host}{path}"
        if qs:
            target_url += f"?{qs}"
        headers = _strip_accept_encoding(_build_forward_headers(request))
        client = request.app.state.http_client_follow
        try:
            resp = await client.get(target_url, headers=headers, timeout=15)
        except Exception:
            logger.warning("获取 plugin.js 失败: %s", target_url, exc_info=True)
            return JSONResponse(status_code=502, content={"error": "Bad Gateway"})

        # 非 200 响应原样透传（错误页/重定向），避免把错误内容当 JS 修补
        if resp.status_code != 200:
            return _with_raw_headers(
                Response(content=resp.content, status_code=resp.status_code),
                _patch_js_response_headers(resp),
            )

        content = resp.text
        original = content
        patched = PLUGIN_CROSS_ORIGIN_RE.sub("", content)
        patched = re_sub(CROSS_ORIGIN_PATTERN, "", patched)
        if patched != original:
            logger.info("已修补 plugin.js: 移除 crossOrigin 赋值")

        extra: dict[str, str] = {
            "content-type": "application/javascript; charset=utf-8",
        }
        if patched != original:
            extra["cache-control"] = "no-cache, no-store, must-revalidate"
            extra["pragma"] = "no-cache"
            extra["expires"] = "0"
        return _with_raw_headers(
            Response(content=patched.encode("utf-8"), status_code=resp.status_code),
            _patch_js_response_headers(resp, extra=extra),
        )

    for _js_path in (
        "/emby/web/modules/htmlvideoplayer/basehtmlplayer.js",
        "/web/modules/htmlvideoplayer/basehtmlplayer.js",
    ):
        app.api_route(_js_path, methods=["GET", "HEAD"], response_model=None)(
            _patch_basehtmlplayer_js
        )

    for _js_path in (
        "/emby/web/modules/htmlvideoplayer/plugin.js",
        "/web/modules/htmlvideoplayer/plugin.js",
    ):
        app.api_route(_js_path, methods=["GET", "HEAD"], response_model=None)(
            _patch_plugin_js
        )

    if _player_keys:

        async def _items_external_player_handler(
            request: Request, user_id: str, item_id: str
        ) -> JSONResponse | StreamingResponse | Response:
            """
            代理 /Users/{user_id}/Items/{item_id}，为含有 MediaSources 的响应注入 ExternalUrls

            :param request: 当前请求
            :param user_id: 用户 ID
            :param item_id: 媒体项 ID
            :return: 注入后的 JSON 或透传响应
            """
            path = request.scope.get("path", "/")
            qs = str(request.url.query)
            target_url = f"{emby_host}{path}"
            if qs:
                target_url += f"?{qs}"

            headers = _strip_accept_encoding(_build_forward_headers(request))
            client = request.app.state.http_client_follow

            try:
                resp = await client.request(
                    request.method,
                    target_url,
                    headers=headers,
                    timeout=30.0,
                )
            except Exception:
                logger.warning("Items 请求失败: %s", target_url, exc_info=True)
                return JSONResponse(
                    status_code=502,
                    content={"error": "Bad Gateway"},
                )

            if resp.status_code != 200:
                return _with_raw_headers(
                    Response(content=resp.content, status_code=resp.status_code),
                    _filter_response_headers(
                        resp, drop=("content-encoding", "content-length")
                    ),
                )

            try:
                data = resp.json()
            except Exception:
                return _with_raw_headers(
                    Response(content=resp.content, status_code=resp.status_code),
                    _filter_response_headers(
                        resp, drop=("content-encoding", "content-length")
                    ),
                )

            if isinstance(data, dict):
                api_key = extract_api_key(request) or ""
                if inject_external_urls(
                    data=data,
                    request=request,
                    emby_host=emby_host,
                    item_id=item_id,
                    api_key=api_key,
                    player_keys=_player_keys,
                ):
                    logger.debug(
                        "已注入 ExternalUrls: user_id=%s, item_id=%s",
                        user_id,
                        item_id,
                    )

            return _with_raw_headers(
                JSONResponse(content=data, status_code=200),
                _filter_response_headers(
                    resp, drop=("content-encoding", "content-length")
                ),
            )

        for _items_path in (
            "/users/{user_id}/items/{item_id}",
            "/emby/users/{user_id}/items/{item_id}",
        ):
            app.api_route(
                _items_path,
                methods=["GET"],
                response_model=None,
            )(_items_external_player_handler)

    if _player_keys:

        @app.get(REDIRECT_PATH, response_model=None)
        async def redirect_to_external(
            request: Request, link: str | None = None
        ) -> RedirectResponse:
            """
            解码外部播放器链接并 302 跳转，目标 scheme/主机受白名单约束

            :param request (Request): 当前请求，用于取本站 netloc 做同站校验
            :param link (str): base64 编码后的原始播放器链接

            :return RedirectResponse: 302 重定向响应
            """
            if not link:
                return RedirectResponse(url="/", status_code=302)
            try:
                decoded = decode_redirect_link(link)
            except Exception:
                logger.warning("无效的 redirect2external link 参数")
                return RedirectResponse(url="/", status_code=302)
            if not is_safe_redirect_target(decoded, request.url.netloc):
                logger.warning("redirect2external 目标不在允许列表: %.120s", decoded)
                return RedirectResponse(url="/", status_code=302)
            return RedirectResponse(url=decoded, status_code=302)

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
        response_model=None,
    )
    async def catch_all(
        request: Request,
    ) -> StreamingResponse | JSONResponse | Response:
        """
        兜底路由：将未匹配请求反向代理到 Emby

        :param request (Request): 当前请求

        :return Response: 流式响应或 502 JSON 错误
        """
        return await _reverse_proxy(request)

    return app
