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

- Python 3.12+（日常 CI 覆盖 3.12/3.14，发版流水线固定 3.14，本地环境建议对齐）
- Git
- （可选）PyInstaller — 构建单文件 exe

### 安装

```bash
git clone https://github.com/cdlongbow/115StrmTool.git
cd 115StrmTool

# 安装平台兼容的锁包
python scripts/install_local_wheels.py combined/wheels

# 安装带哈希锁定的运行时、测试与构建依赖（Windows 已包含 pystray）
python -m pip install --require-hashes --find-links combined/wheels -r combined/requirements-dev.lock
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
# PyInstaller 已包含在 requirements-dev.lock
cd combined
python build_exe.py
# 输出: combined/dist/115网盘STRM生成与302工具.exe
```

版本号说明：`combined/app_metadata.py` 在打包时读取 CI 生成的 `combined/_build_version.py`（由 release.yml 依据发布 tag 写入，不入库），本地未注入时版本显示 `dev`；概览页、启动日志与托盘悬停均使用该版本号。

## 配置

配置文件 `config.json` 在首次运行时自动生成于应用根目录。

关键配置项：

| 配置路径 | 默认值 | 说明 |
|---------|--------|------|
| `admin_host` | `127.0.0.1` | 管理 Web UI 监听地址（改 `0.0.0.0` 允许局域网访问） |
| `admin_port` | 8100 | 管理 Web UI 端口 |
| `emby.emby_host` | `http://192.168.2.100:8096` | Emby 服务器地址 |
| `emby.proxy_port` | 8097 | Emby 反向代理端口 |
| `emby.playback_mode` | `disabled` | 播放模式：`disabled` 回退 Emby 原响应 / `redirect` 302 直链 / `stream_proxy` 局域网流代理（取流转发、仅内网） |
| `p115.redirect_port` | 3333 | 302 跳转服务端口 |
| `p115.strm_url_prefix` | `http://192.168.2.100:3333` | STRM 文件中的跳转 URL 前缀 |
| `p115.rmt_mediaext` | `mp4,mkv,ts,...` | STRM 生成的媒体扩展名 |
| `p115.overwrite_mode` | `never` | STRM 覆盖模式（never/always） |
| `p115.same_playback` | `false` | 多端播放开关（并发播放时复制副本换取独立播放地址） |
| `p115.same_playback_dir` | `/多端播放` | 多端播放副本存放的网盘目录 |
| `p115.fast_scan` | `true` | 快速整树扫描开关（递归整树端点不可用时自动回退逐目录遍历） |

配置每次写盘后自动生成滚动快照（程序目录 `config_backups/`，保留最近 10 份、同一小时内去重）；管理面板"导出配置"按钮可下载当前配置文件（Cookie 保持 `#ENC#` 密文）。

配置文件中 115 Cookie 支持加密存储（`#ENC#` 前缀），管理界面读取配置时 Cookie 以掩码 `********` 显示，写回掩码值表示保持原值。旧版密文首次读取时会自动迁移为带 HMAC 的 `#ENC#v2:` 格式；迁移前会创建独立的 0600 备份，并通过 `config.key.meta` 校验密钥指纹。错误密钥、缺失密钥或旧 Cookie 结构校验失败会拒绝启动并保持文件不变。

### 管理访问令牌

首次启动时程序自动生成随机管理令牌；日志只显示前 4 位，完整值见 `config.json`：

```
管理界面访问令牌已就绪（前 4 位 MS4h...），完整值见 config.json
```

- 所有 `/api` 与 `/admin/api` 接口都要求请求头 `X-Admin-Token` 与令牌一致，否则返回 401；比较使用 `hmac.compare_digest`（恒定时间）。
- 令牌为空时仅允许本机回环访问接口，局域网/公网请求一律 401；自动接口文档（`/docs`、`/redoc`、`/openapi.json`）默认关闭。
- `/admin/api/config/export` 导出时剔除 `admin_token`，导入方首次启动会重新生成令牌。
- 管理页面首次访问会弹窗要求粘贴一次令牌，之后浏览器 localStorage 自动携带。
- 托盘"打开管理界面"会以 `/?token=...` 形式自动注入，无需手动输入。
- 令牌存放在 `config.json` 的 `admin_token` 字段（管理界面回显中掩码显示）。测试或脚本里需要带令牌调用接口时，用 `curl -H "X-Admin-Token: $(python3 -c "import json;print(json.load(open('combined/config.json'))['admin_token'])")" ...` 读取。
- 若删除该字段并重启，会重新生成新令牌。



## 开发工作流

### 代码质量工具

| 工具 | 命令 | 目的 |
|------|------|------|
| 静态检查 | `ruff check .`（仓库根目录） | 未定义名、未用导入、语法错误（规则集见根目录 `ruff.toml`，仅 F + E9） |
| 单元测试 | `cd combined && python -m pytest -q` | 全量回归验证（532 个） |
| 覆盖率门禁 | `cd combined && python -m pytest --cov=. --cov-config=.coveragerc --cov-report=json:coverage.json && python ../scripts/check_coverage.py coverage.json` | 生产代码总覆盖率与关键模块阈值 |
| 真实 SDK 契约 | `python scripts/smoke_sdk_contract.py` | 校验锁包安装后的关键符号与调用签名 |
| 前端行为 | `python scripts/check_inline_js.py combined/web/index.html && node scripts/test_frontend.js` | 内联脚本语法与管理面板交互回归 |
| Python 语法检查 | `python3 -c "import ast; ast.parse(open('combined/*.py').read())"` | 语法验证 |
| 导入检查 | `python3 -c "import sys; sys.path.insert(0, 'combined'); import <module>"` | 模块导入验证 |

测试说明：日常 CI（`.github/workflows/ci.yml`）在 Python 3.12 与 3.14 矩阵上运行哈希锁定依赖安装、`pip check`、真实 SDK 契约、Ruff、Node 前端行为测试、覆盖率门禁、全量 pytest 和每个测试文件独立进程执行。发版流水线在 Windows 上重复关键门禁，冻结 exe 冒烟通过后上传 exe、SHA-256 校验文件和依赖清单，失败时保留诊断产物。测试的重量级依赖替身统一定义在 `combined/conftest.py`，仅替换确实缺失的模块，已安装或已导入的真实模块保持不变。需要真实 SDK 时执行 `bash scripts/setup_py314.sh /tmp/opencode/py314-venv --test`，脚本会按哈希安装 `combined/requirements-dev.lock` 并校验解释器版本与依赖一致性。`full_strm_sync` 只带 cp312 ABI wheel 且未发布到 PyPI，Python 3.14 下预期跳过，程序走纯 Python 生成路径；恢复 Rust 加速需先完成上游 0.1.6 配置与结果契约适配，再用真实 wheel 做非 mock 冒烟。

### 依赖自动升级

`renovate.json` 按周汇总普通 PyPI 依赖与 GitHub Actions 更新，并在 requirements 输入变化后使用 `uv` 重新生成两把哈希锁文件。仓库 `combined/wheels/` 中的 115 SDK 及传递依赖列为手动协调升级，Renovate 不会把它们替换为 PyPI 构建。Renovate 实例需允许 `postUpgradeTasks`，执行环境需支持 `installTools.uv`；使用全局二进制模式时需预装 `uv`。

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

### 播放问题诊断

播放相关诊断日志**默认关闭**：在管理面板「Emby 配置」页开启「播放诊断日志」后即时生效，无需重启；状态存于 `config.json` 的 `media_diag`，环境变量 `STRM_MEDIA_DIAG` 可作紧急覆盖。开启后写入 `logs/combined.log`（INFO 级别，管理面板 `/admin/api/logs` 可读）。直链经 `utils.describe_media_url()` 只输出域名、路径与 `t_left` 剩余秒数，请求头经 `utils.describe_headers()` 只输出 `名字(长度)`，两者都不写值。常用关键字：`签发诊断`（新签发/缓存命中/取链失败）、`302 直链下发诊断`、`流代理外发请求头诊断`（含 `sensitive=` 分组）、`流代理拒绝诊断`（含上游响应体前 200 字）、`流代理续传拒绝诊断`。完整字段说明与排查顺序见[播放模式与诊断](./专有概念/播放模式与诊断.md)。

### 修改媒体流重定向行为

**需修改的文件**：
1. `combined/proxy_app.py` — `_handle_media()`、`_resolve_media_target()`、`_try_media_response()`、`_proxy_stream()` 和 `_build_302_redirect()` 方法

**关键点**：
- `_handle_media()` 按 `playback_mode` 三态分发：`stream_proxy` 先过 `_is_private_source()` 网段门禁并走 `_proxy_stream()` 取流回传；`redirect` 走 302；`disabled` 回退通用反向代理
- `_resolve_media_target()` 通过已解析 URL 缓存 / PlaybackInfo API / STRM 源缓存 / 本请求保存的源地址（`request.state.strm_source_url`，供续传在缓存过期后仍可解析）解析 STRM 跳转链，返回 `(最终 URL, 是否解析成功)`，302 与流代理共用；`skip_final_url_cache=True` 时跳过已解析缓存、给跳转地址追加 `refresh=1` 强制 302 服务重新签发（直链失效自愈场景）
- `_resolve_redirect()` 返回 `(最终 URL, 是否成功解析)` 元组；解析失败时缓存仅保留 5 秒，避免坏 URL 长期驻留
- `_build_302_redirect()` 构建 302 响应，设置 `Location` 头指向 CDN URL，自动对非 ASCII 字符做百分号编码
- `_proxy_stream()` 向 115 CDN 带 Range 取流并以白名单响应头流式回传；上游 403/404/5xx/3xx 或非起始 Range（如 `bytes=5-`）被忽略返回 200 时，在响应头产出前换新链重试（次数与时长受 `STREAM_RETRY_*` 常量约束），耗尽返回 502；`bytes=0-` 这类起始 Range 的 200 属合法整段返回，直接转发不重试

### 新增数据库字段

数据库使用 `PRAGMA user_version` 版本号驱动增量迁移，禁止直接修改 `files` 建表语句后不管旧库。

**步骤**（`combined/database.py`）：
1. 在 `_BASE_SCHEMA` 的建表语句中加入新列（保证全新安装的库直接带列）
2. 编写迁移函数写入缺失列，注册到 `_MIGRATIONS` 列表并把 `_SCHEMA_VERSION` 加一（保证旧库升级补列）
3. 若新列需要索引，加入 `_POST_MIGRATION_STATEMENTS`（在迁移完成后执行，兼容旧库补列场景）

**注意**：迁移函数必须幂等（先查 `PRAGMA table_info` 再 `ALTER TABLE`），旧库上重复执行不能报错。

约束类变更同样要写迁移，不能只改建表语句：SQLite 无法给已有表追加约束，需用 `CREATE UNIQUE INDEX` 补唯一性（唯一索引即可作为 UPSERT 的 `ON CONFLICT` 目标）。建索引前先清理历史重复行，否则索引创建会失败——参考 `_migration_2_ensure_pickcode_unique`。

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

## 前端调试与手工测试

### fetch / 轮询 helper 用法

`combined/web/index.html` 内定义了三个全局 helper，新增页面逻辑必须使用它们，不再直接调 `fetch`/`setInterval`：

- `apiFetch(url, opts)`：沿用 bootstrap 注入的 `X-Admin-Token` 请求头；HTTP 非 2xx 时抛出带 `status` 与响应 `detail` 的 Error，调用方 `try/catch` 或 `.catch` 即可统一报错。
- `apiJson(url, opts)`：`apiFetch` + `r.json()` 组合，直接得到 JSON 数据。
- `startPolling(poll, interval, onData)`：递归定时器代替 `setInterval`。`poll` 为零参 async 函数（返回 Promise 数据）；页面切到后台自动挂起；连续失败按 2 的幂退避（上限 30 秒），连续失败 10 次自动停止。返回对象含 `stop()`；在 `onData(data, handle)` 内可调用 `handle.stop()` 终止。`stopPolling(handle)` 是"停止并置空"的便捷封装。

使用要点：手工调用一次性 `startPolling` 的地方保存返回句柄（如 `_syncProgressTimer`、`_qrcodeTimer`），重入场景先 `stopPolling` 再启动，防止定时器堆叠。

### 浏览器手工测试路径

代码层由 Ruff、pytest、`python3 scripts/check_inline_js.py combined/web/index.html` 与 `node scripts/test_frontend.js` 把关，涉及管理面板交互的改动建议按下述路径点一遍：

1. 启动 `python combined/main.py --no-tray`，从 `config.json` 的 `admin_token` 字段或托盘自动注入获取令牌，浏览器打开 `http://127.0.0.1:8100/`，弹窗粘贴令牌。
2. 概览页两张状态卡在无需操作时应每 10 秒保持刷新；断网/杀进程后页面不崩溃，恢复后数字重新变化。
3. P115 配置页点"扫码登录"，获取二维码后直接关闭弹窗，确认没有后台持续轮询（DevTools Network 停止刷屏）。
4. Emby 配置页故意填错服务地址保存再点保存+重启组合，确认失败提示出现且不再触发重启调用。
5. 同步页启动全量同步观察进度条秒级刷新，完成后 5 秒隐藏。
