# p115_client_wrapper

115 网盘 API 客户端封装层，统一调用入口。负责所有与 115 网盘的 HTTP 通信，包括下载 URL 获取、二维码登录、文件浏览、用户信息查询等。

## 结构

```
p115_client_wrapper.py
├── 常量         # API 端点、默认 UA
├── P115ClientWrapper（class）
│   ├── get_download_url_with_ua() # 加密下载 API（核心方法）
│   ├── get_download_url()          # p115client 原生下载
│   ├── get_qrcode() / check_qrcode() # 二维码登录
│   ├── list_files()               # 目录浏览
│   ├── get_user_info()            # 用户信息
│   ├── get_storage_info()         # 存储信息
│   ├── get_filesystem()           # 文件系统快照
│   └── update_cookie()            # Cookie 热重载
```

## 关键方法

### get_download_url_with_ua(pickcode, user_agent)

系统中最关键的 115 API 调用方法。使用 `p115rsacipher` 对 `{"pick_code":"XXXX"}` 进行 RSA 加密，POST 到 Android 加密 API 端点，携带客户端的 User-Agent。响应经解密后提取 CDN URL 和过期时间戳。

**为什么需要 UA 参数**：115 CDN 的下载 URL 与请求时的 User-Agent 绑定。将客户端 UA 透传到 115 API 确保返回的 URL 对客户端可用。

**返回值**：`(cdn_url, file_name, expires_timestamp)` 或 None。

## 依赖

- **httpx** — HTTP 客户端
- **p115rsacipher** — RSA 加密/解密
- **p115client** — 备用下载方法
- **logger** — 日志

## 规范

- 所有 API 调用有超时（默认 30 秒）
- 异常统一在入口处捕获、记录日志、返回 None
- Cookie 更新通过单独的 `update_cookie()` 方法，保证线程安全