# Changelog

## [Unreleased]

- 无

## [2026-07-22]

1. **新增 302 直链模式开关**
   管理界面 Emby 配置新增"302 直链模式"开关，默认关闭。关闭时走老的流式代理，开启时走 302 直链。

2. **修复 302 直链模式下拖拽进度卡顿问题**
   `_resolve_redirect` 改用 `follow_redirects=False` + 手动解析 `Location` 头，避免实际请求 CDN。消除 HEAD 方式请求 CDN 导致的方法差异问题（CDN 可能拒绝 HEAD 或返回方法绑定签名 URL）。

3. **修复部分客户端（如王二小放牛娃）直链播放失败问题**
   在 PlaybackInfo 拦截时，当 `redirect_mode=True`，将 `Path` 替换为已解析的 CDN 直链。兼容使用 `Path`（而非 `DirectStreamUrl`）播放的客户端。

4. **修复 _resolve_redirect 的 h11 协议错误**
   `_build_forward_headers` 排除 `content-length` 头，避免 HEAD 请求携带 POST 的 `Content-Length` 导致 `h11._util.LocalProtocolError: Too little data for declared Content-Length`。

5. **全量同步后清理残留记录（默认关闭，需手动开启）**
   全量同步完成后，自动检测并标记数据库中已删除的文件的记录为 `deleted`，同时删除磁盘上对应的残留 STRM 文件。默认关闭，需在配置中设置 `cleanup_deleted_strm: true` 开启。

6. **同步锁防止并发冲突**
   全量同步和增量同步添加 `threading.RLock` 互斥锁，防止用户快速多次触发同步导致数据混乱。

## [2026-07-21]

1. **播放流量不再经过服务器**
   代理改为 302 重定向，客户端直连 115 CDN 下载视频。

2. **修复 302 导致浏览器黑屏的问题**
   - 去掉 `Content-Disposition: attachment` 头，浏览器 `<video>` 不再把播放当下载。
   - 下载策略改回加密 API 优先，返回 UA 绑定的 CDN URL。浏览器跟 302 到 CDN 时 UA 匹配，CDN 放行。此前 SDK 优先的 URL 不绑 UA，CDN 直接拒绝浏览器请求。

3. **下载地址获取优先使用加密 API**
   加密 API 返回 UA 绑定 URL（浏览器 302 场景必须），失败自动切 SDK 兜底。内置 4 次阶梯重试，接口崩了能降级，文件没传完能等。

4. **跳转服务代码精简**
   去掉外层重复的重试逻辑，都放进下载方法里了。

5. **p115client 版本号更正**
   错误提示里的版本号从 `0.0.8.10` 改成实际用的 `0.0.9.4.6.1`。

6. **文档全部刷新**
   所有文档从"流式代理"改回"302 重定向"，跟代码保持一致了。
