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
git clone https://github.com/cdlongbow/115StrmTool.git
cd 115StrmTool

# 安装运行时依赖
pip install fastapi uvicorn "httpx[http2]" websockets qrcode Pillow

# 安装 115 SDK（从 wheels 目录；包含 PyPI 上缺失的离线依赖）
python -c "import subprocess, pathlib; [subprocess.run(['pip', 'install', str(f)], check=True) for f in sorted(pathlib.Path('combined/wheels').glob('*.whl'))]"

# （可选）安装桌面集成（仅 Windows）
pip install pystray
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
| `admin_host` | `127.0.0.1` | 管理 Web UI 监听地址（改 `0.0.0.0` 允许局域网访问） |
| `admin_port` | 8100 | 管理 Web UI 端口 |
| `emby.emby_host` | `http://192.168.2.100:8096` | Emby 服务器地址 |
| `emby.proxy_port` | 8097 | Emby 反向代理端口 |
| `emby.redirect_mode` | `false` | 是否开启 302 直链（关闭则回退 Emby 原响应） |
| `p115.redirect_port` | 3333 | 302 跳转服务端口 |
| `p115.strm_url_prefix` | `http://192.168.2.100:3333` | STRM 文件中的跳转 URL 前缀 |
| `p115.rmt_mediaext` | `mp4,mkv,ts,...` | STRM 生成的媒体扩展名 |
| `p115.overwrite_mode` | `never` | STRM 覆盖模式（never/always） |

配置文件中 115 Cookie 支持加密存储（`#ENC#` 前缀），管理界面读取配置时 Cookie 以掩码 `********` 显示，写回掩码值表示保持原值。

## 开发工作流

### 代码质量工具

| 工具 | 命令 | 目的 |
|------|------|------|
| 单元测试 | `cd combined && python -m pytest -q` | 全量回归验证（113 个用例） |
| Python 语法检查 | `python3 -c "import ast; ast.parse(open('combined/*.py').read())"` | 语法验证 |
| 导入检查 | `python3 -c "import sys; sys.path.insert(0, 'combined'); import <module>"` | 模块导入验证 |

测试说明：CI 在打包 exe 前会运行全部测试，测试失败则构建中止。个别测试模块通过 `patch.dict(sys.modules, ...)` 隔离重量级依赖，无需真实 Cookie 或网络即可运行。

### 分支策略

- `main` — 主开发分支，所有提交直接进入并触发后续发版流程
- 功能修复分支按需创建，完成后合回 `main`

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
- `_resolve_redirect()` 返回 `(最终 URL, 是否成功解析)` 元组；解析失败时缓存仅保留 5 秒，避免坏 URL 长期驻留
- `_build_302_redirect()` 构建 302 响应，设置 `Location` 头指向 CDN URL，自动对非 ASCII 字符做百分号编码
- `redirect_mode=false` 时媒体路由回退到通用反向代理转发 Emby 响应，不再单独走 CDN 流式拉取

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