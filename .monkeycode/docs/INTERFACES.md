# 接口文档

## 管理 API

基础路径：`http://<admin_host>:8100/api`

### 服务状态

```
GET /api/status
```

返回服务运行状态、115 客户端就绪状态、用户/存储信息。

**响应示例**：

```json
{
  "status": "running",
  "p115_ready": true,
  "user_info": {"user_name": "xxx", "user_id": "..."},
  "storage_info": {"total_size": "...", "used_size": "..."}
}
```

### 选择本地目录

```
GET /api/select-directory
```

弹出系统原生目录选择对话框（Tkinter，独立线程运行并带超时保护），返回所选路径。用于 Web UI 中选择 STRM 输出目录。

**响应示例**：

```json
{"path": "D:/strm/media"}
```

### 浏览 115 目录

```
GET /api/browse?pid=<directory_id>
```

返回指定目录下的子目录列表，供 Web UI 树形选择器使用。

### STRM 同步

路径映射取自当前配置中的 `p115.paths`，请求无需传 body。

```
POST /api/sync/start
```

启动全量 STRM 同步。遍历 115 目录，为媒体文件生成 .strm 文件。返回 `{"status": "started", "message": "同步任务已启动"}`；已有任务在跑时返回 `{"status": "error", "message": "同步正在进行中"}`。

```
POST /api/sync/incremental
```

启动增量同步。对比上次同步 SHA1，处理新增/变更/删除文件，并自动迁移网盘内被移动或重命名的文件对应的本地 STRM。返回值与全量同步相同。

```
POST /api/sync/reset-baseline
```

清空 STRM 清单和同步历史，下次增量从零开始。返回 `{"success": true}`。

```
POST /api/sync/cancel
```

取消正在进行的同步任务。返回 `{"status": "cancelled"}`。

```
GET /api/sync/progress
```

轮询同步进度。返回：

```json
{
  "running": true,
  "progress": 0.65,
  "current": 650,
  "total": 1000,
  "phase": "scanning|syncing|cleaning|done"
}
```

```
GET /api/sync/history
```

返回同步历史记录列表。

```
POST /api/sync/history/clear
```

清空同步历史记录。返回 `{"success": true}`。

### STRM 文件列表

```
GET /api/strm/list?page=1&page_size=50
GET /api/strm/count
```

分页查询已生成的 STRM 文件清单及总数。

### 二维码登录

```
GET /api/qrcode
```

获取 115 二维码登录图像（HTML img 可直接显示）。

```
POST /api/qrcode/check
Content-Type: application/json

{"payload": "..."}
```

轮询二维码扫描状态。返回 `{"status": "waiting|scanned|expired|success", "data": {...}}`。

### 签到

```
GET /api/checkin/status
```

返回今日签到状态、下次计划时刻和最近一次结果。

```
POST /api/checkin/run
```

立即执行一次签到。返回 `{"status": "ok"|"error", "message": "..."}`。

```
POST /api/checkin/config
Content-Type: application/json

{"enabled": true, "time_range": "06:00-09:00"}
```

保存签到开关和时间段。返回 `{"status": "ok", "message": "签到配置已保存"}`。

## 管理面板 API

基础路径：`http://<admin_host>:8100/admin/api`

### 配置

```
GET /admin/api/config
POST /admin/api/config
Content-Type: application/json

{
  "admin_host": "127.0.0.1",
  "admin_port": 8100,
  "emby": {...},
  "p115": {...}
}
```

读写全局配置。`GET` 返回的配置中 `p115.cookie` 以掩码 `********` 显示；`POST` 时回传掩码值表示保持原 Cookie，传空串表示清除，传新值表示更换（更换后自动重建 115 客户端）。

### Emby 配置

```
GET /admin/api/emby/config
POST /admin/api/emby/config
Content-Type: application/json

{
  "enabled": true,
  "emby_host": "http://192.168.2.100:8096",
  "proxy_host": "0.0.0.0",
  "proxy_port": 8097,
  "pin_rules": "",
  "redirect_mode": false,
  "external_player_url": false,
  "external_player_list": []
}
```

- `redirect_mode`: `true` 为 302 直链模式（客户端直连 CDN），`false`（默认）为回退通用反向代理，转发 Emby 原响应

```
POST /admin/api/emby/restart
```

重启 Emby 代理服务。

### 状态与日志

```
GET /admin/api/status
```

返回组合状态（Emby + P115 是否运行中）。

```
GET /admin/api/p115/status
```

返回 115 客户端就绪状态、统计信息、用户信息。

```
GET /admin/api/logs?lines=200
```

返回最近 N 行日志文本。

```
DELETE /admin/api/logs
```

清空主日志文件并删除轮转备份。返回 `{"status": "ok", "message": "日志已清空"}`。

### 开机自启

```
GET /admin/api/autostart
POST /admin/api/autostart
Content-Type: application/json

{"enabled": true}
```

读写 Windows 注册表开机自启项（`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`）。

## 302 跳转服务

基础路径：`http://<redirect_host>:3333`

### 获取下载重定向

```
GET|POST /api/v1/plugin/P115StrmHelper/redirect_url?pickcode=<17位字母数字>
```

返回：

- **成功**: HTTP 302，`Location` 头指向 115 CDN 下载 URL
- **失败**: HTTP 502，JSON `{"code": -1, "msg": "Failed to resolve download URL"}`
- **参数错误**: HTTP 400，JSON `{"code": -1, "msg": "Missing or invalid pickcode"}`

### 兜底路由

```
GET /<pickcode>
```

从路径直接提取 pickcode，相同逻辑。

## STRM 文件格式

生成的 `.strm` 文件内容为一行 URL：

```
http://<redirect_host>:3333/api/v1/plugin/P115StrmHelper/redirect_url?pickcode=<17位字母数字>
```

Emby 扫描到 `.strm` 文件后，读取此 URL 作为媒体源的 Path。

## 外部播放器

### 支持的播放器

| 标识符 | 名称 | 平台 |
|---------|------|------|
| PotPlayer | PotPlayer | Windows |
| VLC | VLC | 全平台 |
| IINA | IINA | macOS |
| Infuse | Infuse | iOS/tvOS |
| MPV | MPV | 全平台 |
| nPlayer | nPlayer | iOS |
| OmniPlayer | OmniPlayer | macOS |
| FigPlayer | Fig Player | iOS |
| SenPlayer | SenPlayer | iOS |
| Fileball | Fileball | iOS |
| StellarPlayer | StellarPlayer | Windows |
| MX Player | MX Player | Android |
| MX Player Pro | MX Player Pro | Android |
| 弹弹Play | 弹弹Play | Android |

### 注入方式

代理拦截 `/Users/{user_id}/Items/{item_id}` 响应，在响应顶层 `ExternalUrls` 数组中注入外部播放器条目。每个播放器对应一个 `{Name, Url, Description}` 条目，`Name` 前缀保留播放器 key 供前端匹配，URL 统一经 `/redirect2external?link=<base64>` 中转后 302 到自定义协议地址（如 `potplayer://...`、`vlc://...`）。

## 配置结构

```json
{
  "admin_host": "127.0.0.1",
  "admin_port": 8100,
  "emby": {
    "enabled": false,
    "emby_host": "http://192.168.2.100:8096",
    "proxy_host": "0.0.0.0",
    "proxy_port": 8097,
    "pin_rules": "",
    "external_player_url": false,
    "external_player_list": [],
    "redirect_mode": false
  },
  "p115": {
    "enabled": false,
    "cookie": "",
    "redirect_host": "0.0.0.0",
    "redirect_port": 3333,
    "strm_url_prefix": "http://192.168.2.100:3333",
    "rmt_mediaext": "mp4,mkv,ts,iso,rmvb,avi,mov,mpeg,mpg,wmv,3gp,asf,m4v,flv,m2ts,tp,f4v,webm",
    "download_mediaext": "srt,ssa,ass,sup,pgs,sub,idx",
    "auto_download_mediainfo": false,
    "overwrite_mode": "never",
    "cleanup_deleted_strm": false,
    "use_rust": false,
    "paths": []
  },
  "checkin": {
    "enabled": false,
    "time_range": "06:00-09:00"
  }
}
```