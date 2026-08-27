# p115_client_wrapper

115 网盘 API 客户端封装层，统一调用入口。采用加密 API 优先（UA 绑定）+ SDK 降级的下载策略，内置重试和 405 自适应切换机制。同时负责二维码登录、用户信息查询、存储查询等。

## 结构

```
p115_client_wrapper.py
├── 常量 / 异常        # 下载重试延迟、API 端点、频率控制、IncompleteUploadError
├── P115ClientWrapper（class）
│   ├── get_download_url_with_ua() # 下载入口：加密 API 优先 + SDK 降级 + 内置重试
│   ├── _try_sdk_download_url()    # SDK 下载（不绑定 UA，作为降级方案）
│   ├── _raw_download_url_encrypted() # 单次加密 API 调用（含 405→SDK 切换）
│   ├── _extract_url_info()        # 从 CDN URL 提取文件名和过期时间
│   ├── get_qrcode() / check_qrcode() # 二维码登录
│   ├── get_user_info()            # 用户信息
│   ├── get_storage_info()         # 存储信息
│   └── update_cookie()            # Cookie 热重载
```

## 关键方法

### get_download_url_with_ua(pickcode, user_agent)

下载地址获取的统一入口。采用两级策略：

1. **加密 API 优先**：通过 `_raw_download_url_encrypted` 调用 Android 加密下载 API，返回绑定指定 UA 的 URL，浏览器跟随 302 时 UA 匹配、CDN 不会拒绝，内置 4 次阶梯重试（间隔 0s / 0.5s / 1.0s / 2.0s）
2. **SDK 降级**：加密 API 全部重试失败后调用 `_try_sdk_download_url`，返回不绑定 UA 的 URL
3. **405 自适应切换**：加密 API 返回 405 时，内部自动切换回 SDK 再试一次
4. **IncompleteUploadError 处理**：文件上传不完整异常触发自动重试

:param pickcode (str): 文件 pickcode，17 位字母数字
:param user_agent (str): 客户端 User-Agent；为空时使用 115 iOS 默认 UA

:return Tuple: (下载 URL, 文件名, 过期时间戳)，失败返回 None

## 依赖

- **httpx** — HTTP 客户端
- **p115cipher** — RSA 加密/解密
- **p115client** — 115 SDK（降级下载、二维码登录）
- **app_ver** — 115 iOS 默认 UA 生成
- **logger** — 日志

## 规范

- SDK 方法调用默认注入超时（连接 10s / 读取 60s / 写入 30s / 连接池 10s），调用方可通过 `timeout` 参数覆盖
- 模块自身 httpx 调用超时 10 秒（连接 5 秒）
- 异常统一在入口处捕获、记录日志、返回 None
- Cookie 更新通过单独的 `update_cookie()` 方法，保证线程安全