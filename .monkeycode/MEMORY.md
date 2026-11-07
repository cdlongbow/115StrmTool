# 用户指令记忆

本文件记录了用户的指令、偏好和教导，用于在未来的交互中提供参考。

## 格式

### 用户指令条目
用户指令条目应遵循以下格式：

[用户指令摘要]
- Date: [YYYY-MM-DD]
- Context: [提及的场景或时间]
- Instructions:
  - [用户教导或指示的内容，逐行描述]

### 项目知识条目
Agent 在任务执行过程中发现的条目应遵循以下格式：

[项目知识摘要]
- Date: [YYYY-MM-DD]
- Context: Agent 在执行 [具体任务描述] 时发现
- Category: [运维部署|构建方法|测试方法|排错调试|工作流协作|环境配置]
- Instructions:
  - [具体的知识点，逐行描述]

## 去重策略
- 添加新条目前，检查是否存在相似或相同的指令
- 若发现重复，跳过新条目或与已有条目合并
- 合并时，更新上下文或日期信息
- 这有助于避免冗余条目，保持记忆文件整洁

## 条目

### 代码改动和提交流程约束
- Date: 2026-08-27（2026-10-15 修订提交推送条款）
- Context: 用户明确指示
- Instructions:
  - 不要擅自改动代码，除非用户明确指示
  - **不要擅自提交和推送**：完成改动后停下等用户明确说提交/推送才执行；提交时直接提交并推送到 main 分支，无需创建功能分支或 PR
  - 经用户确认后的提交推送，自动将本次改动写入 CHANGELOG.md 顶部未发版条目（当天条目存在则追加，不存在则新建日期条目），内容面向用户可感知的变化

### 写入后必须回读与异常字符扫描
- Date: 2026-10-15
- Context: 用户明确指示；会话有随机字符注入史（工具回显与模型输出中出现过丢字、全角字符替换等污染）
- Instructions:
  - 任何对文件的写入/编辑完成后，必须用读取或 grep 回读硬验证文件真实状态再信任改动成功，不得只凭工具调用返回"成功"就认定内容正确
  - 写入涉及代码或文档时，提交/交付前对改动文件执行异常字符扫描（如 kana/emoji/U+FFFD/全角符号 `[\x{3040}-\x{30FF}\x{FFFD}\x{FF01}-\x{FF5E}]` 等），发现污染字符先修复再继续
  - 模型自身输出被污染时（如 import 语句出现乱码），以文件真实字节为准，回读确认后修复而非照抄污染回显

### 提交前本地质量门禁
- Date: 2026-10-02
- Context: 讨论 CI 把关方案后用户采纳建议（"听你的"）
- Instructions:
  - 每次代码改动提交前，必须先本地跑通：`ruff check .`（仓库根目录）和 `cd combined && python -m pytest -q`，两者全绿才允许提交推送
  - 日常 CI（.github/workflows/ci.yml，push/PR 触发）已承担同样门禁，但不以此替代本地验证
  - 用例数在四处重复维护：README.md「运行单元测试（N 个用例）」、TODO.md 变异测试条目、.monkeycode/docs/INDEX.md 命令注释、.monkeycode/docs/DEVELOPER_GUIDE.md 工具表；新增或删除测试后用 `python -m pytest --collect-only -q` 取准确数并同步这四处

### 回复语言偏好
- Date: 2026-10-03
- Context: 用户在分析报告后明确要求
- Instructions:
  - 所有回复与推理过程使用简体中文

### 115 SDK 依赖升级约束
- Date: 2026-10-03
- Context: Agent 在执行 zkmydgth 上游仓库借鉴分析时发现
- Category: 环境配置
- Instructions:
  - p115client 禁止升级到 0.0.9.7.x：iterdir 大重构删除约 29 个符号（iter_file_list、iter_files_with_path_skim、traverse_tree_with_path 等），与本仓库 p115_client_wrapper 不兼容，上游同版本升级当天即回滚
  - python-concurrenttools 必须 <0.1.9（0.1.9 将 threadpool_map 改名 thread_conmap，p115client 0.0.9.6.5.1 导入即崩）；requirements.txt 已钉上限
  - 上游本地 wheels + --find-links 不防 pip 拉远端高版本，钉版必须写进 requirements 显式约束
  - 2026-10-09 更新：两个上游已完成 p115client 0.0.9.7.2 适配（DDSRem `7ddd3ec3`、zkmydgth v2.7.19），本仓库 wrapper 迁移可对照其 diff 进行；在未完成迁移核对前，本仓库仍维持 0.0.9.6.5.1 钉版不动

### 发版流程与测试环境约束
- Date: 2026-10-05
- Context: Agent 在执行 20261003 发版与 CI 加固任务时发现
- Category: 运维部署
- Instructions:
  - 发版动作 = 打日期 tag：`git tag YYYYMMDD && git push origin <tag>`，触发 release workflow 自动构建并上传 GitHub Release，产物 exe 命名 `115.STRM.302.exe`
  - release workflow 首步版本一致性校验：tag 名（去 v/-/_ 归一）必须等于 CHANGELOG 顶部 `## [YYYY-MM-DD]` 日期段日期，否则流水线失败；发版前确认顶部未发布日期段就是目标日期
  - 同日多次发布用序号 tag（`YYYYMMDD-2` 起，序号 1-99 且从 1 开始），校验只比对主日期；同日新改动追加进 CHANGELOG 同一日期节（Release 说明取顶部日期节全文，天然包含累计内容）
  - 发版或推送后应检查失败 job 日志定位根因，勿假定流水线必绿
  - 本地沙箱为 Python 3.11，p115client 系列依赖要求 setuptools>=77（需 3.12）装不上真实 SDK；测试涉及 p115client 时一律用 patch.dict / conftest 的 stub_modules 注入假模块，不得在沙箱 pip install 真实 SDK
  - 新会话沙箱默认没有门禁工具链：`ruff`、`pytest`、`pytest-asyncio`、`httpx`、`fastapi`、`uvicorn`、`websockets`、`qrcode`、`Pillow` 都要先装（`pip install --break-system-packages`），否则 ruff 报 command not found、pytest 收集阶段直接 ModuleNotFoundError；local-only 依赖（p115client、p115cipher、p115pickcode、full_strm_sync）跳过不装
  - 需要在不装真实 SDK 的前提下端到端验证 admin 端点时，可注入 p115client/p115cipher/p115pickcode/full_strm_sync 桩后直接 `create_admin_app()` 起 TestClient 或 uvicorn（`main()` 会写入 admin_token，走 create_admin_app 可保持令牌为空、跳过鉴权）

### 上游借鉴来源与跟踪重点
- Date: 2026-10-14
- Context: Agent 在周期性巡查四仓更新时发现
- Category: 工作流协作
- Instructions:
  - 四个参考上游：DDSRem-Dev/MoviePilot-Plugins（本项目移植来源，README 记载）、zkmydgth/MoviePilot-Plugins（同源活跃 fork）、pancras-loe/115-station、Ponphil/LitePan
  - **P115StrmHelper 参考主线在 DDSRem 本家**：zkmydgth 已于 2026-10-14（`db8ea296`）把该插件归档并从市场下架（package.v3.json 撤条目）；查该插件演进只看 DDSRem
  - 巡查结论收窄：115-station 主线已分化到 NAS 整理/刮削/Emby 对账/订阅，本项目为桌面轻量定位，只关注其 115 端点变动与同步/清理类修复；LitePan 长期无提交
  - 115 响应数字字段类型不可信（115-station `36e654f` 2026-10-17 事故：index_info 容量带小数/科学计数/字符串形态，严格解析整段失败被误报 Cookie 失效）；凡解析 115 返回的 size/count 类数值必须宽容处理（float 化再向零取整、识别失败按 0 或 None 降级，绝不让单字段拖垮整段解析）
  - 2026-10-16 巡查补充：DDSRem 的 P115StrmHelper 已拆 V2/V3 双线（V3 要求 Python>=3.14，worker 队列/decision register/分享上报重架构，v3-decision-register.md 记录决策），V2 侧仍有独立修复；借鉴参考盯 V2 侧修复与 V3 设计走向即可，本仓库无需跟进 V3 架构
  - 巡查方法用 GitHub API（需带 git credential 里的 token 以避开匿名限流），起点接上次巡查末条提交日期；历史巡查：2026-10-18 二巡（当日基线后再巡）：DDSRem `cf48b39c` STRM 空地址防护已借鉴落地本仓库（配置 trim 归一 + 同步入口前置校验 + 写盘兜底报错），另 V2 2.8.79/V3 3.0.2 为发版 chore 无新逻辑；115-station 当日 4 提交均为 RE0/癫影站外分享生态（本项目无此功能面，不借鉴）；zkmydgth 与 LitePan 无新提交；无遗留待评估项
  - 2026-10-31 三巡：DDSRem 基线后仅 2 条新增，`3d8c5160` 是 p115center license 版本号 chore，`ff22d879` 为 P115Client 目录节点缓存清理（遍历 `p115client.const.ID_TO_DIRNODE_CACHE.values()` 原地 clear，先 `tuple()` 物化再清避免 defaultdict 边遍历边增长；原地清空是为了让运行中任务持有的缓存引用同步失效）；本仓库钉版 0.0.9.6.5.1 的 `const.py:263` 确有该符号（`defaultdict(dict)`，值为 `IdToDirnode`，走 MutableMapping MixIn 的 clear），符号可用；本仓库当前无任何缓存清理入口且托盘常驻 + wrapper 重度使用 `fs_dir_getid`，节点缓存只增不减，落地建议是在 admin_api 加一个清理端点
  - 三巡其余收窄结论：115-station 命中口径的只有 `bbd3e960`（手动增量排队被自动轮询抢占时补齐回报，本仓库无自动轮询增量，无对应场景）与 `0f502c34`（Go 写的本机中转播放/连接上限/分段换链，2600+ 行，功能面与桌面轻量定位分叉，暂不借鉴）；`0d5830af` 扫码登录默认改支付宝小程序与本仓库 README 记载一致，已对齐；zkmydgth 基线后 50+ 提交全在 TgOfflineBot/保种空间守护/TgSignin/ConfigBackup/VarietyGuard 等非本仓库功能面；LitePan 无新提交（最新仍为 2026-10-02）
  - 三巡待用户决策项：一是 DDSRem `ff22d879` 的 P115Client 缓存清理是否落地本仓库；二是由 zkmydgth `3d90702b`（ConfigBackup 备份排除 Telegram `.session` 授权文件）触发的自查——本仓库 WebDAV 备份包 `_REQUIRED_MEMBERS` 含 `config.key`（与 config.json 同目录的解密密钥），远端只存密文但密钥同包，等效远端可解出 Cookie，是否改为密钥随包分离需用户定夺
  - 三巡决策结果：缓存清理已落地（`c209eed`，端点 + 面板按钮 + 5 单测，随 `20261031` 发版）；WebDAV 备份密钥分离不改，用户主要传本地 WebDAV
  - 2026-11-03 四巡：DDSRem 与 LitePan 均无新提交；zkmydgth 仅 `3ea551a2`/`500a8e75` SeedSpaceGuard v3.3.0（保种空间面，不借鉴）；115-station 基线后 31 条，命中收窄口径的实质内容有两条 115 端点行为知识——`9c381499` 分享失效不能看 `state`，过期/取消的分享 `/share/snap` 照样 `state=true` 且文件列得出来，要看 `shareinfo.share_state`（1 正常、4 取消、7 过期），取链 `POST proapi /app/share/downurl` 的错误码 4100008 访问码错、4100010 已取消、4100018 已过期、`data=[]`/`url=false` 表示文件不在分享里；`75af5c6b` 分享列举原 300 目录 / 4 层上限会让整库分享一律报「分享太大」，已改为不限目录数与层数，且遇风控整轮停下按退避重试、不能跳过继续列（半份清单会把没列到的误判为分享里没了）。本仓库无任何 115 分享功能面（grep 无 share_code/snap），两条仅作知识记录不落地
  - 四巡待用户决策项：由 zkmydgth `11638b17`（TgSignin v1.2.3 签到互斥）触发的自查——本仓库 `checkin_scheduler` 的定时 `_loop` 与 `manual_checkin` 共用 `run_p115_checkin_once` 但签到动作本身无互斥（`self._lock` 只保护状态文件读写），手动点击与自动触发同时发生会并发签到；上游做法是模块级非阻塞 `threading.Lock`，定时/手动/命令统一 `acquire(blocking=False)`，抢不到就跳过并记来源日志
  - 四巡决策结果：签到互斥已落地（checkin_scheduler 模块级 `_CHECKIN_LOCK`，`_tick` 与 `manual_checkin` 统一 `acquire(blocking=False)`，手动抢不到返回"签到正在执行，请稍后重试"、自动抢不到跳过并记日志且不动状态；5 个用例见 `test_checkin_mutex.py`）；该类测试须 monkeypatch `checkin_scheduler.CHECKIN_STATE_FILE` 到 tmp_path，否则会往 combined 目录写 checkin_state.json

[CI 构建运行时升级到 Python 3.14]
- Date: 2026-11-03
- Context: Discovered by Agent while performing Python 3.14 build runtime upgrade
- Category: Build Methods
- Instructions:
  - ci.yml 与 release.yml 的 python-version 已切 `3.14`（20261031 之后生效，下一版 exe 内嵌 CPython 3.14）；沙箱本地仍是 3.11，本地门禁不代表 3.14 行为，最终以 CI 为准
  - `full_strm_sync` 是唯一不在 PyPI 的依赖，wheels 目录只有 cp312 ABI；已从 requirements.txt 移除该硬锁（否则 pip 解析 3.14 时找不到它直接失败），改由 CI 从 combined/wheels/ 容错安装——release.yml 的安装步已对齐 ci.yml 的 per-wheel OK/SKIP 模式（弃用 check=True）
  - 3.14 下 Rust 加速缺位属预期降级（exe 走纯 Python 生成路径，与默认 `use_rust=False` 行为一致），恢复条件见 TODO「full_strm_sync 恢复 Rust 加速」
  - 2026-11-07：GitHub Actions storage 看 artifacts/Packages；public repo 的标准 runner 分钟免费，artifact 仍会吃账户 Actions 存储。发布 exe 已由 softprops/action-gh-release 作为 Release Assets 上传，release.yml 不再用 upload-artifact；未来若需要 workflow artifacts，优先短保留或只在调试分支开启

[CPython 3.14 下反复重装导入链导致 GC 段错误]
- Date: 2026-11-03
- Context: Discovered by Agent while fixing CI failure after Python 3.14 upgrade
- Category: Troubleshooting & Debugging
- Instructions:
  - CPython 3.14 下，夹具按用例反复"装桩导入项目模块 → patch.dict 退出把 fastapi/starlette/opentelemetry 等传递模块一并从 sys.modules 删除"，旧模块对象仅剩悬空引用，后续某次导入触发 GC 时段错误（全量单进程跑可能不触发，standalone-per-file 先暴露，日志特征是进度点打满后 exit 139 无 traceback——faulthandler 需 `PYTHONFAULTHANDLER=1` 重跑才给栈，崩点在 GC/abc 注册处）
  - 修复模式：夹具内用模块级缓存只带桩导入一次、复用模块对象；桩（如 tkinter）由被测函数运行时再查 sys.modules，隔离语义不变。超时用例顺带保留 `threading.Event` 取代 sleep 并按线程名 join 的收口写法
  - 本地复现 3.14 行为的方法：`uv venv` 用 uv 托管的 3.14 运行时 + `uv pip install --python <venv的python>` 装 requirements（uv venv 不带 pip），本地装不上的平台 wheel 直接跳过；standalone 循环用 `timeout 90 <venv-python> -m pytest "$f" -q`，沙箱 3.11 门禁不能替代 3.14 验证

[沙箱重建后的 Python 环境重建配方]
- Date: 2026-11-03
- Context: Discovered by Agent while discussing sandbox lifetime with the user
- Category: Environment Configuration
- Instructions:
  - 沙箱重开不会自动装新版 Python：系统 `python3` 只读基础镜像且随重建漂移（已观察到 3.11 与 3.12 两种），会话内自装的 conda/uv 运行时、venv、`pip --break-system-packages` 包在重开后全部丢失，按下方步骤重装即可
  - 快速就绪（对齐 CI 3.14，含真实 SDK 与门禁工具）：`pip install --break-system-packages -q uv` → `uv python install 3.14` → `uv venv <dir>` → `uv pip install --python <dir>/bin/python -r combined/requirements.txt pytest pytest-asyncio`；uv venv 不含 pip，uv 托管解释器拒绝被直接修改，一律走 `uv pip install --python`；`full_strm_sync` 无 3.14 wheel 属预期 SKIP，桩测试不依赖它
  - 轻量就绪（仅跑门禁，容忍装不上真实 SDK 时）：照 2026-10-09 条目直接 pip 装 ruff/pytest 等纯 Python 工具即可
