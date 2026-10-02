# p115_client_wrapper

115 网盘 API 客户端封装层，统一调用入口。采用加密 API 优先（UA 绑定）+ SDK 降级的下载策略，内置重试和 405 自适应切换机制。同时负责二维码登录、用户信息查询、存储查询、文件浏览等。

## 结构

```
p115_client_wrapper.py
├── 常量 / 异常        # 下载重试延迟、API 端点、端点冷却表、IncompleteUploadError、DownloadApiUnavailableError
├── P115ClientWrapper（class）
│   ├── get_download_url_with_ua() # 下载入口：加密 API 优先 + SDK 降级 + 内置重试
│   ├── _try_sdk_download_url()    # SDK 下载（不绑定 UA，作为降级方案）
│   ├── _raw_download_url_encrypted() # 单次加密 API 调用（405 抛 DownloadApiUnavailableError）
│   ├── _extract_url_info()        # 从 CDN URL 提取文件名和过期时间
│   ├── download_url()             # 获取纯下载 URL（附属文件下载用）
│   ├── ensure_folder()            # 获取网盘目录 ID，缺失时自动创建（Web/App API 双通道）
│   ├── copy_pickcode()            # 复制网盘文件并返回副本 pickcode（多端播放用）
│   ├── delete_file()              # 删除网盘文件（副本清理用）
│   ├── _call_with_405_fallback()  # 通用 405 降级：Web API → App API
│   ├── _is_405_error()            # 判断异常是否为 HTTP 405（status_code / code / 消息文本）
│   ├── _wait_cooling()            # 端点级调用冷却（基于 utils.RateLimiter）
│   ├── fs_files_app()            # 115 目录浏览（Android API，支持分页）
│   ├── get_qrcode() / check_qrcode() # 二维码登录
│   ├── get_user_info()            # 用户信息
│   ├── get_storage_info()         # 存储信息
│   ├── update_cookie()            # Cookie 热重载
│   └── close()                    # 释放底层连接（幂等，可重复调用）
```

## 关键方法

### get_download_url_with_ua(pickcode, user_agent)

下载地址获取的统一入口。采用两级策略：

1. **加密 API 优先**：通过 `_raw_download_url_encrypted` 调用 Android 加密下载 API，返回绑定指定 UA 的 URL，浏览器跟随 302 时 UA 匹配、CDN 不会拒绝，内置 4 次阶梯重试（间隔 0s / 0.5s / 1.0s / 2.0s）
2. **405 直跳 SDK**：加密 API 返回 405（下载接口被风控）时抛出 `DownloadApiUnavailableError`，入口循环捕获后立即停止重试、直达 SDK 降级通道
3. **SDK 降级**：加密 API 不可用时调用 `_try_sdk_download_url`，返回不绑定 UA 的 URL
4. **IncompleteUploadError 处理**：文件上传不完整异常触发自动重试

:param pickcode (str): 文件 pickcode，17 位字母数字

:param user_agent (str): 客户端 User-Agent；为空时使用 115 iOS 默认 UA

:return Tuple: (下载 URL, 文件名, 过期时间戳)，失败返回 None

### 多端播放接口（copy_pickcode / delete_file / ensure_folder）

- `ensure_folder(pan_path)`：`fs_dir_getid` 查询目录 ID（返回 `-1` 视为不存在），缺失时 `fs_mkdir` 创建（405 降级 `fs_makedirs_app`），创建失败的兜底是再查一次 `fs_dir_getid`。返回目录 ID 或 None，根目录直接返回 0
- `copy_pickcode(pickcode, pid)`：pickcode 转 file id（`p115pickcode.to_id`）后 `fs_copy` 复制到目标目录（405 降级 `fs_copy_app`），随后按 `fs_files`/`fs_files_app` 轮询目录首项的 `pc` 字段获取副本 pickcode，带阶梯重试。返回副本 pickcode 或 None
- `delete_file(pickcode)`：`fs_delete` 删除文件（405 降级 `fs_delete_app`），失败不抛出仅返回 False。用于多端播放副本的延迟清理

三者内部均通过 `_call_with_405_fallback(primary_call, fallback_call, operation)` 实现 Web API → App API 降级：`_is_405_error` 依次检查异常的 `status_code`、`code` 属性和消息文本中的 405 特征，非 405 异常原样上抛。

## 依赖

- **httpx** — HTTP 客户端
- **p115cipher** — RSA 加密/解密
- **p115client** — 115 SDK（降级下载、二维码登录、文件复制/删除）
- **p115pickcode** — pickcode 与 file id 互转
- **app_ver** — 115 iOS 默认 UA 生成
- **utils** — `RateLimiter`（端点冷却）、`capture_exceptions`（异常收口装饰器）
- **logger** — 日志

## 规范

- SDK 方法调用默认注入超时（连接 10s / 读取 60s / 写入 30s / 连接池 10s），调用方可通过 `timeout` 参数覆盖
- 模块自身 httpx 调用超时 10 秒（连接 5 秒）
- 业务方法统一使用 `utils.capture_exceptions` 装饰器收口异常（记录日志并返回默认值），下载链路的 `IncompleteUploadError` / `DownloadApiUnavailableError` 属于控制流异常，在入口内部消化不上抛
- 端点级冷却由 `DEFAULT_ENDPOINT_COOLDOWNS` 声明间隔，`_wait_cooling()` 惰性创建并复用 `RateLimiter`，同一端点并发调用按最小间隔排队
- Cookie 更新通过单独的 `update_cookie()` 方法，保证线程安全