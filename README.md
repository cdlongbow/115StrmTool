# 115网盘STRM生成、签到与Emby 302工具

115 网盘 STRM 生成 + 签到 + Emby 反向代理 Windows 桌面工具。让 Emby 直接播放 115 网盘中的视频，无需中转下载。

基于 [DDSRem-Dev/MoviePilot-Plugins](https://github.com/DDSRem-Dev/MoviePilot-Plugins) 的 115 网盘 STRM 助手和 Emby 302 反向代理移植为独立 Windows 桌面工具。

## 功能

- **STRM 文件生成**：全量同步扫描 115 网盘目录，增量同步对比 SHA1 仅处理差异（首次自动降级全量），支持 Rust 加速批量处理
- **Emby 反向代理**：代理 Emby 请求，拦截 PlaybackInfo 强制 DirectPlay，流式代理 115 CDN 媒体流到客户端，消除 CORS 和 UA 绑定问题
- **115 每日签到**：在指定时间段内随机时刻自动签到，支持手动签到，获取连续签到积分
- **外部播放器注入**：支持 PotPlayer、VLC、IINA、Infuse 等 14 款播放器一键调用
- **扫码登录**：支持支付宝小程序/微信小程序扫码，本地生成二维码，Cookie 自动填入
- **记录管理**：支持一键清空同步日志和 STRM 文件记录，不删除磁盘文件
- **开机自启**：后台托盘运行，Windows 注册表自启
- **系统托盘**：启动后自动隐藏到右下角托盘图标，右键菜单支持全量同步、增量同步、立即签到、打开日志目录

## 架构

```
Emby 客户端 → 反向代理 (:8097) → Emby 服务器 (:8096)
                           ↘ 115 CDN（流式代理，不经过浏览器）
```

播放时代理直接从 115 CDN 拉取媒体数据流式返回客户端，客户端不直接访问 CDN，彻底消除跨域问题。

详见 [架构文档](.monkeycode/docs/ARCHITECTURE.md)。

## 使用

直接运行 `115网盘STRM生成与302工具.exe`，启动后自动隐藏到系统托盘。

托盘右键菜单：
- **打开管理界面** — 默认浏览器打开管理页面
- **全量同步** — 立即执行 STRM 全量同步
- **增量同步** — 对比上次同步 SHA1，仅处理新增/变更/删除文件
- **立即签到** — 手动触发 115 每日签到
- **打开日志目录** — 用资源管理器打开日志文件夹
- **退出** — 停止所有服务并退出

管理界面 http://localhost:8100

| 服务 | 端口 | 说明 |
|------|------|------|
| 管理 Web UI | 8100 | 配置、同步、签到、日志 |
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

GitHub Actions 自动构建：推送 `v*` tag 或手动触发 workflow_dispatch（需输入版本号）。

## 技术栈

- Python 3.12 + FastAPI + Uvicorn
- httpx（异步 HTTP/2 客户端）
- HTML + JavaScript 单页 Web UI
- pystray + Pillow（系统托盘）
- Tkinter（原生 Windows 目录选择器）
- PyInstaller（单文件 exe，--noconsole）
- p115client + p115cipher（115 网盘 SDK）
- full_strm_sync（STRM 生成 Rust 加速）
- SQLite + JSON（文件清单、同步记录、签到状态）
- GitHub Actions（CI/CD 发布）

## 详细文档

- [系统架构](.monkeycode/docs/ARCHITECTURE.md)
- [接口文档](.monkeycode/docs/INTERFACES.md)
- [开发者指南](.monkeycode/docs/DEVELOPER_GUIDE.md)
- [STRM 文件机制](.monkeycode/docs/专有概念/STRM文件.md)
- [302 跳转服务](.monkeycode/docs/专有概念/302跳转服务.md)
- [外部播放器注入](.monkeycode/docs/专有概念/外部播放器注入.md)
