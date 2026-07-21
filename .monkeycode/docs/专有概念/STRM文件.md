# STRM 文件

STRM 是 Emby / Jellyfin 等媒体服务器使用的一种占位文件格式，内容只有一行文本 URL。播放时，媒体服务器读取这个 URL 作为媒体源的地址，不校验文件扩展名。

## 什么是 STRM 文件？

STRM 文件是一种纯文本文件（`.strm`），内容是一条指向实际媒体资源的 HTTP URL。Emby 扫描媒体库时会将 `.strm` 文件视为一个媒体条目，播放时直接请求其中的 URL。

**关键特征**:
- 占用极小磁盘空间（几十字节）
- 不包含实际媒体数据，只包含获取媒体的方式
- Emby 将其识别为远程媒体源（`IsRemote=true`, `Protocol=Http`）

## 本工具中的 STRM 格式

生成的 STRM 文件内容为：

```
http://<redirect_host>:3333/api/v1/plugin/P115StrmHelper/redirect_url?pickcode=<17位字母数字>
```

- `redirect_host`/`redirect_port` — 由 `config.json` 中 `p115.strm_url_prefix` 配置
- `pickcode` — 115 网盘文件的唯一标识符（17 位字母数字）

## 生成流程

1. 用户通过管理 Web UI 配置路径映射（115 路径 → 本地路径）
2. 触发全量同步后，STRM 生成器遍历 115 目录
3. 为每个符合条件的媒体文件（按 `rmt_mediaext` 过滤）写入 `.strm` 文件到本地路径
4. 文件元数据（pickcode、大小、sha1 等）记录到 SQLite 数据库
5. Emby 扫描本地路径时自动发现 STRM 文件并入库

## 播放流程

1. Emby 客户端请求播放 STRM 对应的媒体条目
2. 反向代理拦截 PlaybackInfo，检测 MediaSource 为远程 HTTP 源
3. 代理强制 DirectPlay 并解析 Path 中的 pickcode
4. 代理解析 STRM 跳转链后返回 302 重定向，客户端直连 115 CDN 获取媒体数据

## 附属于 STRM 的元数据文件

生成 STRM 时可选同步附属媒体信息文件（如字幕 `.srt`、`.ass`），通过 `download_mediaext` 配置扩展名过滤。