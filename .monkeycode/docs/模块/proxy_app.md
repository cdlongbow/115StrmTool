# proxy_app

Emby 反向代理核心模块。代理所有 Emby 客户端请求，拦截 PlaybackInfo 强制 DirectPlay，流式代理 115 CDN 媒体流，注入 crossOrigin 拦截脚本和外部播放器按钮。

## 结构

```
proxy_app.py（1434 行，最大模块）
├── create_app()             # FastAPI 应用工厂
├── 路由处理程序
│   ├── _handle_media()              # 媒体路由：/videos/, /audio/, /items/download
│   ├── _playback_info_strm_direct_play()  # PlaybackInfo 拦截
│   ├── _system_info_handler()       # System/Info 端口重写
│   ├── _patch_basehtmlplayer_js()   # JS croussOrigin 修补
│   ├── _patch_plugin_js()           # plugin.js crossOrigin 修补
│   ├── _items_external_player_handler()  # 外部播放器注入
│   ├── _reverse_proxy()             # 通用反向代理
│   └── catch_all()                  # 兜底路由
├── 辅助方法
│   ├── _stream_from_cdn()           # 115 CDN 流式代理（核心修复）
│   ├── _resolve_redirect()          # 重定向链解析
│   ├── _try_media_response()        # 媒体 URL 解析入口
│   ├── _build_forward_headers()     # 转发请求头构建
│   └── _stream_from_cdn()           # CDN→客户端流式传输
└── 常量
    ├── MEDIA_ROUTES                 # 媒体路由前缀列表
    ├── CACHE_KEY_HEADERS            # 缓存 key 白名单头
    └── HOP_BY_HOP_HEADERS           # 逐跳头排除列表
```

## 关键路径

### 媒体流处理

```
Client → /videos/{id}/stream
  ↓
_handle_media()
  ├─ _try_media_response()      # 检查缓存 / PlaybackInfo / STRM 缓存
  │   └─ _stream_from_cdn()     # httpx GET CDN URL → StreamingResponse
  └─ _reverse_proxy()            # 回退到 Emby 服务器
```

### PlaybackInfo 拦截

```
Client → POST /Items/{id}/PlaybackInfo
  ↓
_playback_info_strm_direct_play()
  1. 代理请求到 Emby 服务器
  2. 检测 MediaSources 是否为 STRM（IsRemote=true, Protocol=Http）
  3. 强制 DirectPlay：SupportsTranscoding=false
  4. 构建 DirectStreamUrl 相对路径
  5. 缓存 STRM 源 URL 供后续媒体请求使用
  6. 返回修改后的 PlaybackInfo
```

## CDN 流式代理

`_stream_from_cdn()` 是解决 CORS 问题的核心。

**输入**：115 CDN URL + 原始请求

**流程**：
1. 用 `_build_forward_headers(request)` 构建请求头（包括 Range、User-Agent）
2. 用 `httpx.AsyncClient`（`follow_redirects=False`）向 CDN 发 GET 请求
3. 用 `StreamingResponse` + `aiter_bytes(65536)` 逐块转发响应
4. 排除 `HOP_BY_HOP_HEADERS` 和 `content-encoding`/`transfer-encoding` 头

## JS 修补

### crossOrigin 拦截脚本

注入到所有 Emby HTML 页面的 `<head>` 中：

- 将 `HTMLMediaElement.prototype.crossOrigin` 的 getter 改为返回 null
- 用 MutationObserver 监听新增的 video/audio 元素，移除 crossorigin 属性

### basehtmlplayer.js 修补

将 `getCrossOriginValue` 的三元表达式替换为 null，阻止浏览器在媒体元素上设置 crossorigin="anonymous"。

### plugin.js 修补

移除 `elem.crossOrigin = value` 的赋值语句。

## 缓存系统

| 缓存 | 作用域 | TTL | 用途 |
|------|--------|-----|------|
| playback_url_cache | 全局 | 90s | 媒体 URL 缓存 |
| strm_source_cache | 全局 | 300s | STRM 源 URL 缓存 |
| playback_user_cache | 全局 | 300s | 用户关联缓存 |

## 依赖

- **external_players** — 外部播放器注入
- **config_manager** — pin_rules 解析
- **logger** — 日志