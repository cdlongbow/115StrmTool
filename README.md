# 115网盘STRM生成与302工具

115 网盘 STRM 生成 + 媒体流代理 + Emby 反向代理 Windows 桌面工具。让 Emby 直接播放 115 网盘中的视频，无需中转下载。
基于[https://github.com/DDSRem-Dev/MoviePilot-Plugins)]：115网盘STRM助手和Emby 302 反向代理代码生成。
## 功能

- **STRM 文件生成**：扫描 115 网盘目录，自动为媒体文件生成 STRM 占位文件
- **Emby 反向代理**：代理 Emby 请求，拦截 PlaybackInfo 强制 DirectPlay，流式代理 115 CDN 媒体流到客户端，消除 CORS 和 UA 绑定问题
- **外部播放器注入**：支持 PotPlayer、VLC、IINA、Infuse 等 14 款播放器一键调用
- **扫码登录**：支持支付宝小程序/微信小程序扫码，Cookie 自动填入
- **开机自启**：后台托盘运行，Windows 注册表自启
- **隐藏到托盘**：关闭窗口缩小到系统托盘

## 架构

```
Emby 客户端 → 反向代理 (:8097) → Emby 服务器 (:8096)
                           ↘ 115 CDN（流式代理，不经过浏览器）
```

播放时代理直接从 115 CDN 拉取媒体数据流式返回客户端，客户端不直接访问 CDN，彻底消除跨域问题。

详见 [架构文档](.monkeycode/docs/ARCHITECTURE.md)。

## 使用

直接运行 `115网盘STRM生成与302工具.exe`，弹出原生管理窗口。

管理界面 http://localhost:8100

三个服务端口：

| 服务 | 端口 | 说明 |
|------|------|------|
| 管理 Web UI | 8100 | 配置、同步、日志 |
| Emby 反向代理 | 8097 | 客户端通过此端口访问 Emby |
| 302 跳转服务 | 3333 | STRM 文件中引用的跳转地址 |

## 开发

```bash
cd combined
pip install -r requirements.txt
pip install wheels/*.whl
python main.py              # 控制台模式
python main.py --no-tray    # Windows 强制控制台模式（无系统托盘）
```

## 打包

```bash
cd combined
python build_exe.py
```

生成 `dist/115网盘STRM生成与302工具.exe`。

GitHub Actions 自动构建：推送 `v*` tag 或手动触发 workflow_dispatch。

## 技术栈

- Python 3.12 + FastAPI + Uvicorn
- httpx（异步 HTTP/2 客户端）
- HTML + JavaScript 单页 Web UI
- pystray + pywebview（系统托盘 + 原生窗口）
- PyInstaller（单文件 exe，--noconsole）
- p115client + p115rsacipher（115 网盘 SDK）
- SQLite（文件清单与历史记录）
- GitHub Actions（CI/CD 发布）

## 详细文档

- [系统架构](.monkeycode/docs/ARCHITECTURE.md)
- [接口文档](.monkeycode/docs/INTERFACES.md)
- [开发者指南](.monkeycode/docs/DEVELOPER_GUIDE.md)
- [STRM 文件机制](.monkeycode/docs/专有概念/STRM文件.md)
- [302 跳转服务](.monkeycode/docs/专有概念/302跳转服务.md)
- [外部播放器注入](.monkeycode/docs/专有概念/外部播放器注入.md)
