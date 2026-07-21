# p115_client_wrapper

115 网盘 API 客户端封装层，统一调用入口。采用 SDK 优先 + 加密 API 降级的下载策略，内置重试和 405 自适应切换机制。同时负责二维码登录、文件浏览、用户信息查询等。

## 结构

```
p115_client_wrapper.py
├── 常量 / 异常        # 下载重试延迟、API 端点、频率控制、IncompleteUploadError
├── P115ClientWrapper（class）
│   ├── get_download_url_with_ua() # 下载入口：SDK 优先 + 加密 API 降级 + 内置重试
│   ├── _try_sdk_download_url()    # SDK 下载（不绑定 UA，302 场景更优）
│   ├── _raw_download_url_encrypted() # 单次加密 API 调用（含 405→SDK 切换）
│   ├── _extract_url_info()        # 从 CDN URL 提取文件名和过期时间
│   ├── get_qrcode() / check_qrcode() # 二维码登录
│   ├── list_files()               # 目录浏览
│   ├── get_user_info()            # 用户信息
│   ├── get_storage_info()         # 存储信息
│   ├── get_filesystem()           # 文件系统快照
│   └── update_cookie()            # Cookie 热重载
```

## 关键方法

### get_download_url_with_ua(pickcode, user_agent)

下载地址获取的统一入口。采用两级策略：

1. **SDK 优先**：调用 `_try_sdk_download_url` 使用 p115client SDK，返回不绑定 UA 的 URL，更适合 302 重定向场景
2. **加密 API 降级**：SDK 失败时通过 `_raw_download_url_encrypted` 调用 Android 加密下载 API，内置 4 次阶梯重试（间隔 0s / 0.5s / 1.0s / 2.0s）
3. **405 自适应切换**：加密 API 返回 405 时，内部自动切换回 SDK 再试一次
4. **IncompleteUploadError 处理**：文件上传不完整异常触发自动重试

:param pickcode (str): 文件 pickcode，17 位字母数字
:param user_agent (str): 客户端 User-Agent；为空时使用 115 iOS 默认 UA

:return Tuple: (下载 URL, 文件名, 过期时间戳)，失败返回 None

## 依赖

- **httpx** — HTTP 客户端
- **p115cipher** — RSA 加密/解密
- **p115client** — 115 SDK（优先下载、文件浏览、二维码登录）
- **app_ver** — 115 iOS 默认 UA 生成
- **logger** — 日志

## 规范

- 所有 API 调用有超时（默认 30 秒）
- 异常统一在入口处捕获、记录日志、返回 None
- Cookie 更新通过单独的 `update_cookie()` 方法，保证线程安全