# 115网盘STRM生成、签到与Emby 302反代工具

115 网盘 STRM 生成 + 签到 + Emby 反向代理 Windows 桌面工具。让 Emby 直接播放 115 网盘中的视频，无需中转下载。

基于 [DDSRem-Dev/MoviePilot-Plugins](https://github.com/DDSRem-Dev/MoviePilot-Plugins) 的 115 网盘 STRM 助手和 Emby 302 反向代理移植为独立 Windows 桌面工具。

## 功能

- **STRM 文件生成**：全量同步扫描 115 网盘目录，增量同步对比 SHA1 仅处理差异（首次自动降级全量），支持 Rust 加速批量处理，实时进度条显示，可随时取消
- **快速整树扫描**：全量/增量扫描使用 115 递归整树接口一次获取整棵子树，万级文件库的扫描请求数降为个位数；接口不可用时自动回退逐目录遍历（默认开启，可在 P115 配置关闭）
- **配置快照与导出**：配置每次写盘自动生成滚动快照（保留最近 10 份），面板可一键导出当前配置文件（Cookie 保持加密形态）
- **接口鉴权与危险操作确认**：管理界面与全部接口需要访问令牌；重置同步数据、清空记录/日志等不可恢复操作使用 90 秒一次性确认令牌，过期或重复使用会被拒绝
- **Emby 反向代理**：代理 Emby 请求，拦截 PlaybackInfo 强制 DirectPlay，支持 302 直链（默认关闭）和回退 Emby 原响应两种模式可切换；开启 302 后客户端直连 115 CDN，消除中转带宽消耗
- **多端播放**：多台设备同时播放同一文件时自动复制网盘副本换取独立播放地址，规避 115 单文件并发下载限制，副本用后即删（默认关闭，可在 P115 配置页开启）
- **同步后清理残留**：全量同步后自动删除目标目录中已不存在的 STRM 文件（默认关闭，可在 UI 开启）
- **115 每日签到**：在指定时间段内随机时刻自动签到，支持手动签到，获取连续签到积分
- **外部播放器注入**：支持 PotPlayer、VLC、IINA、Infuse 等 14 款播放器一键调用
- **扫码登录**：支持支付宝小程序/微信小程序扫码，本地生成二维码，Cookie 自动填入（优先选小程序端，网页端易触发 115 的"IP 登录异常"限制）
- **记录管理**：支持一键清空同步日志，STRM 文件列表支持隐藏，不删除数据库记录和磁盘文件
- **开机自启**：后台托盘运行，Windows 注册表自启
- **系统托盘**：启动后自动隐藏到右下角托盘图标，右键菜单支持全量同步、增量同步、立即签到、打开日志目录

## 架构

```
Emby 客户端 → 反向代理 (:8097) → Emby 服务器 (:8096)
                            ↘ 115 CDN（302 重定向，客户端直连）
```

开启 302 直链后，代理返回 302 跳转让客户端直连 115 CDN，媒体流量不经过代理服务器；关闭时媒体请求回退到通用反向代理，由 Emby 按原响应处理。跨域问题通过注入 crossOrigin 拦截脚本解决。

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

管理界面默认 http://127.0.0.1:8100（默认仅本机访问，可在设置中改为局域网监听）

| 服务 | 端口 | 说明 |
|------|------|------|
| 管理 Web UI | 8100 | 配置、同步、签到、日志（默认绑定 127.0.0.1） |
| Emby 反向代理 | 8097 | 客户端通过此端口访问 Emby |
| 302 跳转服务 | 3333 | STRM 文件中引用的跳转地址 |

## 开发

```bash
cd combined
pip install -r requirements.txt
python main.py              # 控制台模式
python main.py --no-tray    # Windows 强制控制台模式（无系统托盘）
```

`combined/wheels/` 是发布构建使用的锁包快照（含平台专用 wheel，不能整目录直接安装）；发版流水线会自动按平台挑选安装。

运行单元测试（165 个用例，需 Python 3.12+ 环境）：

```bash
cd combined
python -m pytest -q
```

## 打包

```bash
cd combined
python build_exe.py
```

生成 `dist/115网盘STRM生成与302工具.exe`。

GitHub Actions 自动构建：推送日期格式 tag（如 `20261003`）或手动触发 workflow_dispatch（输入版本号）。发版流水线开头会校验发布号与 CHANGELOG 顶部条目标题一致，不一致直接失败。构建流程先运行全部单元测试，测试通过后才打包 exe 并创建 Release。Release 说明自动取自 CHANGELOG.md 最顶部条目，发版前请先更新它。

## 技术栈

- Python 3.12 + FastAPI + Uvicorn + Pydantic
- httpx（异步 HTTP/2 客户端）
- HTML + JavaScript 单页 Web UI
- pystray + Pillow（系统托盘）
- Tkinter（原生 Windows 目录选择器）
- PyInstaller（单文件 exe，--noconsole）
- p115client + p115cipher（115 网盘 SDK）
- full_strm_sync（STRM 生成 Rust 加速）
- SQLite + JSON（文件清单、同步记录、签到状态）
- pytest（单元测试）
- GitHub Actions（CI/CD 发布）

## 详细文档

- [系统架构](.monkeycode/docs/ARCHITECTURE.md)
- [接口文档](.monkeycode/docs/INTERFACES.md)
- [开发者指南](.monkeycode/docs/DEVELOPER_GUIDE.md)
- [STRM 文件机制](.monkeycode/docs/专有概念/STRM文件.md)
- [302 跳转服务](.monkeycode/docs/专有概念/302跳转服务.md)
- [外部播放器注入](.monkeycode/docs/专有概念/外部播放器注入.md)
