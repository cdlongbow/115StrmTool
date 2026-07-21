# redirect_service

302 跳转服务。接收 pickcode，通过 `p115_client_wrapper.get_download_url_with_ua` 获取 115 CDN 下载地址，支持缓存和 UA 绑定。

## 结构

```
redirect_service.py
├── 常量            # CACHE_TTL_DEFAULT, CACHE_MAX_SIZE, DOWNLOAD_API_PATH
├── RedirectService（class）
│   ├── create_app()          # FastAPI 应用工厂
│   ├── _do_redirect()        # 核心 302 跳转逻辑（pickcode 解析 + 缓存 + 302 响应）
│   │   ├── _real_client_ip()  # 获取真实客户端 IP（支持反向代理场景）
│   ├── _build_302()          # 构建 302 响应
│   ├── _extract_pickcode_from_path() # 从路径兜底提取
│   ├── _cache_key()          # 缓存键构建（pickcode:UA_HASH）
│   ├── _ua_hash()            # UA 的 SHA256 摘要
│   ├── _get_cached() / _set_cache() # 缓存读写
│   └── clear_cache()         # 缓存清理
```

## 关键方法

### _do_redirect(pickcode, request)

1. 验证 pickcode 格式（17 位字母数字）
2. 检查缓存（key = `pickcode:sha256(ua)[:16]`）
3. 缓存命中 → 直接返回 302 跳转
4. 缓存未命中 → 调用 `get_download_url_with_ua` 获取新 URL
5. 写入缓存（TTL = expires_time - 300，max 90s）
6. 返回 302 重定向响应

**重试策略**：`redirect_service` 本身不包含重试逻辑。下载 URL 获取的重试由 `p115_client_wrapper.get_download_url_with_ua()` 内部完成（SDK 优先 + 加密 API 4 次阶梯重试 + 405 自适应切换），保持跳转服务简洁。

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
- 最多 1000 个条目，超出时淘汰最旧的
- 每次写缓存前清理已过期的条目

## 依赖

- **p115_client_wrapper** — 加密下载 API
- **logger** — 日志