# 开发者指南

## 项目目的

115网盘STRM生成与302工具是一个 Windows 桌面工具，让 Emby 媒体服务器能够直接播放 115 网盘中的视频文件，无需下载到本地。

**核心职责**:
- 将 115 网盘目录结构映射为本地媒体库路径
- 生成 STRM 占位文件供 Emby 扫描
- 在播放时通过 302 重定向让客户端直连 115 CDN 获取媒体流
- 提供管理 Web UI 控制整条链路

**相关系统**:
- **Emby 媒体服务器** — 媒体库管理与客户端播放
- **115 网盘** — 云存储源
- **外部播放器** — PotPlayer、VLC、IINA 等

## 环境搭建

### 前置条件

- Python 3.12+
- Git
- （可选）PyInstaller — 构建单文件 exe

### 安装

```bash
git clone https://github.com/cdlongbow/MoviePilot-Windows.git
cd MoviePilot-Windows

# 安装运行时依赖
pip install fastapi>=0.110.0 uvicorn>=0.27.0 httpx[http2]>=0.27.0 websockets>=12.0

# 安装 115 SDK（从 wheels 目录）
python -c "import subprocess, pathlib; [subprocess.run(['pip', 'install', str(f)], check=True) for f in sorted(pathlib.Path('combined/wheels').glob('*.whl'))]"

# （可选）安装桌面集成
pip install pystray Pillow pywebview pywin32
```

### 运行

```bash
# 开发模式（直接运行 Python）
python combined/main.py

# 带 --no-tray 参数（无系统托盘，直接显示控制台）
python combined/main.py --no-tray
```

### 构建 exe

```bash
pip install pyinstaller
cd combined
python build_exe.py
# 输出: combined/dist/115网盘STRM生成与302工具.exe
```

## 配置

配置文件 `config.json` 在首次运行时自动生成于应用根目录。

关键配置项：

| 配置路径 | 默认值 | 说明 |
|---------|--------|------|
| `admin_port` | 8100 | 管理 Web UI 端口 |
| `emby.emby_host` | `http://192.168.2.100:8096` | Emby 服务器地址 |
| `emby.proxy_port` | 8097 | Emby 反向代理端口 |
| `p115.redirect_port` | 3333 | 302 跳转服务端口 |
| `p115.strm_url_prefix` | `http://192.168.2.100:3333` | STRM 文件中的跳转 URL 前缀 |
| `p115.rmt_mediaext` | `mp4,mkv,ts,...` | STRM 生成的媒体扩展名 |
| `p115.overwrite_mode` | `never` | STRM 覆盖模式（never/always） |

## 开发工作流

### 代码质量工具

| 工具 | 命令 | 目的 |
|------|------|------|
| Python 语法检查 | `python3 -c "import ast; ast.parse(open('combined/*.py').read())"` | 语法验证 |
| 导入检查 | `python3 -c "import sys; sys.path.insert(0, 'combined'); import <module>"` | 模块导入验证 |

### 分支策略

- `main` — MoviePilot 插件版（原始项目，参考用）
- `Windows` — Windows 独立工具版（当前开发分支）

### 提交规范

提交信息格式：

```
<type>(<scope>): <subject>

[optional body]

Co-authored-by: <AI Name> <email>
```

类型参考：feat（新功能）、fix（Bug 修复）、refactor（重构）、chore（构建/工具链）。

## 常见任务

### 修复 115 下载 URL 解析失败

**问题表现**：日志出现 `int() argument must be a string, a bytes-like object or a real number, not 'list'`

**根因**：`urllib.parse.parse_qs` 返回值是 `{'key': ['val']}` 格式，直接用 `int(v)` 报错。

**修复位置**：`combined/p115_client_wrapper.py:134-139`，将 `v` 改为 `v[0]` 取列表第一项。

### 添加新外部播放器

**需修改的文件**：
1. `combined/external_players.py` — 在 `ALL_EXTERNAL_PLAYER_KEYS` 和 `EXTERNAL_PLAYERS` 中添加新条目

**步骤**：
1. 在 `EXTERNAL_PLAYERS` 字典中添加播放器名称和平台元数据
2. 在 `_build_player_target_url` 中添加对应平台的 URL 构建逻辑
3. 在 `build_external_player_script` 中添加 JS 按钮模板

### 修改媒体流重定向行为

**需修改的文件**：
1. `combined/proxy_app.py` — `_try_media_response()` 和 `_build_302_redirect()` 方法

**关键点**：
- `_try_media_response()` 通过三级缓存（已解析 URL 缓存 / PlaybackInfo API / STRM 源缓存）解析 STRM 跳转链，最终解析为 CDN 直链
- `_build_302_redirect()` 构建 302 响应，设置 `Location` 头指向 CDN URL
- `_stream_from_cdn()` 保留作为流式代理备选方案

### 修改流式代理备选行为

**需修改的文件**：
1. `combined/proxy_app.py` — `_stream_from_cdn()` 方法

**关键点**：
- 使用 `request.app.state.http_client_no_follow` 发起 GET 请求
- 通过 `_build_forward_headers(request)` 转发客户端原始头（含 Range、User-Agent）
- 用 `aiter_bytes(chunk_size=65536)` 逐块流式传输
- 排除 `HOP_BY_HOP_HEADERS` 和 `content-encoding/transfer-encoding`

## 编码规范

**文件组织**：
- 每个 .py 文件一个主要类/函数集合
- 配置 manager 使用单例模式
- 数据库使用线程本地连接

**命名**：

| 类型 | 约定 | 示例 |
|------|------|------|
| 模块 | snake_case | `proxy_app.py` |
| 类 | PascalCase | `P115ClientWrapper` |
| 函数 | snake_case | `get_download_url_with_ua` |
| 常量 | UPPER_SNAKE | `CACHE_TTL_DEFAULT` |

**日志**：
- 使用 `logger`（`from logger import logger`）
- 日志级别：DEBUG（开发详情）、INFO（正常操作）、WARNING（可恢复问题）、ERROR（需要关注的故障）
- 包含上下文：`logger.info("用户创建成功", extra={"user_id": uid})`
- 异常时使用 `exc_info=True`：`logger.error("API 调用失败", exc_info=True)`

**错误处理**：
- 避免 bare `except:`
- 网络操作设置超时
- 关键 API 调用包装在 try/except 中并记录详细上下文