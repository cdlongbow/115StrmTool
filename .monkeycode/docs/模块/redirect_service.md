# redirect_service

302 跳转服务。接收 pickcode，通过 `p115_client_wrapper.get_download_url_with_ua` 获取 115 CDN 下载地址，支持缓存和 UA 绑定；开启多端播放（same_playback）后，并发播放同一文件时自动复制网盘副本换取独立播放地址。

## 结构

```
redirect_service.py
├── 常量            # CACHE_TTL_DEFAULT, DOWNLOAD_API_PATH, COPY_CLEANUP_DELAY, COPY_DIR_DEFAULT
├── RedirectService（class）
│   ├── __init__()              # 可注入 same_playback / same_playback_dir 覆盖配置（默认读 config_manager）
│   ├── create_app()            # FastAPI 应用工厂
│   │   ├── _get_same_playback()          # 读取多端播放开关与副本目录
│   │   ├── _resolve_copy_dir_pid()       # 解析副本目录 ID（首次自动创建目录并缓存）
│   │   ├── _create_copy()                # 复制文件副本，返回副本 pickcode
│   │   ├── _schedule_copy_cleanup()      # 延迟副本清理（COPY_CLEANUP_DELAY，异步任务）
│   ├── _do_redirect()          # 核心 302 跳转逻辑（pickcode 解析 + 缓存 + 多端复制 + 302 响应）
│   │   ├── _real_client_ip()   # 获取真实客户端 IP（支持反向代理场景）
│   ├── _build_302()            # 构建 302 响应
│   ├── _extract_pickcode_from_path() # 从路径兜底提取
│   ├── _cache_key()            # 缓存键构建（pickcode:UA_HASH）
│   └── _ua_hash()              # UA 的 SHA256 摘要
```

## 关键方法

### _do_redirect(pickcode, request)

1. 验证 pickcode 格式（17 位字母数字）
2. 检查缓存（key = `pickcode:sha256(ua)[:16]`，同一 key 并发回源只执行一次）
3. 缓存命中 → 直接返回 302 跳转
4. 多端播放开启且该 pickcode 已有其他 UA 的回源缓存（`count_prefix("pickcode:") > 0`）→ 复制副本，以副本 pickcode 取址；复制失败静默回退原文件
5. 调用 `get_download_url_with_ua` 获取新 URL；走过副本路径时无论成败均调度延迟删除副本
6. 写入缓存（TTL = expires_time - 300，max 90s）
7. 返回 302 重定向响应

**重试策略**：`redirect_service` 本身不包含重试逻辑。下载 URL 获取的重试由 `p115_client_wrapper.get_download_url_with_ua()` 内部完成（加密 API 优先 + 4 次阶梯重试 + 405 时直跳 SDK 降级）；副本复制/删除走 `p115_client_wrapper` 的 405 → App API 自动降级，保持跳转服务简洁。

## 路由

| 路径 | 方法 | 说明 |
|------|------|------|
| `/api/v1/plugin/P115StrmHelper/redirect_url?pickcode=XXX` | GET/HEAD/POST | 主端点 |
| `/api/v1/plugin/P115StrmHelper/redirect_url/{file_id}` | GET/HEAD/POST | 兼容 file_id 参数 |
| `/redirect_url?pickcode=XXX` | GET/HEAD/POST | 短路径端点 |
| `/{path}` | GET/HEAD | 兜底路由，从路径段提取 pickcode |

## 缓存机制

- 缓存 key 包含 UA 哈希（前 16 位），不同 UA 独立缓存
- TTL = CDN URL 过期时间 - 300 秒（留安全余量），上限 90 秒
- 计算出的 TTL 小于等于 0 时记录告警日志（含 115 返回的剩余有效期），便于排查播放中途失败
- 最多 1000 个条目，超出时按 LRU 淘汰
- 每次写缓存前清理已过期的条目
- **缓存击穿防护**：对同一 `pickcode:UA_HASH` 的并发请求，仅第一个执行回源获取下载地址（`AsyncKeyLock` 按 key 互斥），其余请求在锁内二次检查缓存后直接复用结果；锁在无使用者时自动清理
- **多端播放并发探测**：`AsyncTtlCache.count_prefix("pickcode:")` 统计该文件当前有效的独立 UA 缓存条目数，大于 0 即视为存在并发播放，触发副本复制（统计时顺带清理过期条目）

## 依赖

- **p115_client_wrapper** — 加密下载 API、副本复制/删除（`copy_pickcode` / `delete_file` / `ensure_folder`）
- **config_manager** — `same_playback` / `same_playback_dir` 配置（构造时注入覆盖参数可跳过）
- **utils** — `AsyncTtlCache`、`AsyncKeyLock`（缓存击穿防护）
- **logger** — 日志