# proxy_app

Emby 反向代理核心模块。代理所有 Emby 客户端请求，拦截 PlaybackInfo 强制 DirectPlay，302 重定向模式下让客户端直连 CDN，注入 crossOrigin 拦截脚本和外部播放器按钮。

## 结构

```
proxy_app.py（最大模块）
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
│   ├── _build_302_redirect()        # 构建 302 重定向到 CDN 直链
│   ├── _resolve_redirect()          # 重定向链解析（单跳跟随，返回 URL + 成功标志）
│   ├── _try_media_response()        # 媒体 URL 解析入口（三级缓存 → 302）
│   └── _build_forward_headers()     # 转发请求头构建
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
  ├─ _try_media_response()      # 三级缓存查询 → 解析 STRM 跳转链
  │   ├─ redirect_mode=true     → _build_302_redirect()  # 302 重定向到 CDN 直链
  │   └─ redirect_mode=false    → return None（回退反向代理转发 Emby 响应）
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
  5. 替换 MediaSources[].Path 为 CDN 直链（兼容使用 Path 播放的客户端）
  6. 缓存 STRM 源 URL 供后续媒体请求使用
  7. 返回修改后的 PlaybackInfo
```

## 重定向模式与回退

通过 `redirect_mode` 配置项切换两种播放路径：

- **302 重定向模式**（`redirect_mode=true`）：`_try_media_response` 调用 `_build_302_redirect` 返回 302 响应，客户端直连 115 CDN。媒体流量不经过代理服务器。
- **关闭重定向**（`redirect_mode=false`）：`_try_media_response` 直接返回 None，媒体路由回退到 `_reverse_proxy` 通用转发，由 Emby 服务器按原响应处理。

## 手动解析 Location 头

`_resolve_redirect()` 使用 `follow_redirects=False` 发起 HEAD 请求，手动解析响应头中的 `Location` 字段获取 CDN URL，不实际请求 CDN。这避免了 CDN 拒绝 HEAD 请求（405 Method Not Allowed）或返回方法绑定的签名 URL 导致客户端后续 GET Range 请求失败的问题。

返回值为 `(最终 URL, 是否成功解析)` 元组：

- 解析成功 → 缓存按正常 TTL（90s）写入
- 解析失败（超时、缺 Location 头等）→ 返回原始 URL 并标记失败，仅缓存 5 秒，避免坏结果长期驻留导致后续请求反复 302 到失效地址

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
| playback_url_cache | 全局 | 90s（解析失败 5s） | 媒体 URL 缓存 |
| strm_source_cache | 全局 | 300s | STRM 源 URL 缓存 |
| playback_user_cache | 全局 | 300s | 用户关联缓存 |

`playback_url_cache` 的写入由 `playback_url_key_lock`（按 `item_id + MediaSourceId + 用户 + 请求头` 隔离的互斥锁）保护，同一媒体源并发播放时仅第一个请求执行 PlaybackInfo 回源与重定向链解析，其余请求复用缓存结果。缓存底层为 `AsyncTtlCache`（OrderedDict 实现的 TTL + LRU 淘汰）。

## WebSocket 代理

`_ws_proxy` 双向转发客户端与 Emby 后端的 WebSocket（`/embywebsocket`）。客户端→后端方向用 `receive()` 统一处理文本与二进制帧；两个转发任务用 `FIRST_COMPLETED` 等待，任一方向断开即取消另一方向并关闭连接，避免协程悬挂在已断开的对端。

## 依赖

- **external_players** — 外部播放器注入
- **config_manager** — pin_rules 解析
- **utils** — `AsyncTtlCache`、`AsyncKeyLock`（缓存击穿防护）
- **logger** — 日志