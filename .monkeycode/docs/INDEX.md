# 115网盘STRM生成与302工具 文档

本文档涵盖 115网盘STRM生成与302工具的架构、接口和开发指南。目标读者为开发者和高级用户。

**快速链接**: [架构](./ARCHITECTURE.md) | [接口](./INTERFACES.md) | [开发者指南](./DEVELOPER_GUIDE.md)

---

## 核心文档

### [架构](./ARCHITECTURE.md)
系统设计、技术栈、组件结构和数据流程。从这里开始了解工具如何运作。

### [接口](./INTERFACES.md)
REST API 端点、配置结构、STRM 文件格式和外部播放器列表。集成或使用此工具的参考。

### [开发者指南](./DEVELOPER_GUIDE.md)
环境搭建、开发工作流、编码规范和常见任务。贡献者必读。

---

## 模块

| 模块 | 描述 | 位置 |
|------|------|------|
| 应用入口与编排 | 服务启动/停止、生命周期管理 | `combined/main.py` |
| Emby 反向代理 | 媒体流代理、PlaybackInfo 拦截、JS 修补 | `combined/proxy_app.py` |
| 115 跳转服务 | pickcode 解析、UA 绑定下载、URL 缓存 | `combined/redirect_service.py` |
| STRM 文件生成器 | 目录遍历、STRM 写入、附属元数据下载 | `combined/strm_generator.py` |
| 115 客户端封装 | 加密下载 API、二维码登录、文件浏览 | `combined/p115_client_wrapper.py` |
| 管理 API | STRM 同步、二维码登录、离线下载 | `combined/api_routes.py` |
| 管理面板 API | 配置读写、服务控制、日志查看 | `combined/admin_api.py` |
| 外部播放器注入 | 14 个播放器的协议 URL 构建与 UI 注入 | `combined/external_players.py` |
| 配置管理 | JSON 配置读写、顶置规则解析 | `combined/config_manager.py` |
| 数据持久化 | SQLite 数据库、文件清单、同步历史 | `combined/database.py` |
| Windows 桌面集成 | 系统托盘、原生 WebView2 窗口 | `combined/windows_tray.py` |
| 日志设置 | RotatingFileHandler + 控制台输出 | `combined/logger.py` |

---

## 核心概念

| 概念 | 描述 |
|------|------|
| [STRM 文件](./专有概念/STRM文件.md) | Emby 媒体库的占位文件，包含一行跳转 URL |
| [302 跳转服务](./专有概念/302跳转服务.md) | 将 pickcode 解析为 115 CDN 下载地址的服务 |
| [外部播放器注入](./专有概念/外部播放器注入.md) | 在 Emby Web UI 中添加外部播放器按钮的机制 |

---

## 入门指南

### 项目新人？

按此路径学习：
1. **[架构](./ARCHITECTURE.md)** — 了解全局
2. **[核心概念](#核心概念)** — 学习领域术语
3. **[开发者指南](./DEVELOPER_GUIDE.md)** — 搭建环境
4. **[接口](./INTERFACES.md)** — 探索公开 API

### 首次贡献？

1. **[开发者指南](./DEVELOPER_GUIDE.md)** — 搭建和工作流
2. **[常见任务](./DEVELOPER_GUIDE.md#常见任务)** — 分步指南

---

## 快速参考

```bash
# 开发运行
python combined/main.py --no-tray

# 构建 exe
cd combined && python build_exe.py

# 安装依赖
pip install -r combined/requirements.txt
pip install combined/wheels/*.whl
```

### 重要文件

| 文件 | 目的 |
|------|------|
| `combined/main.py` | 应用入口 |
| `combined/proxy_app.py` | 反向代理核心（1434 行） |
| `combined/build_exe.py` | PyInstaller 构建脚本 |
| `.github/workflows/release.yml` | CI/CD 发布工作流 |
| `combined/requirements.txt` | 运行时依赖 |