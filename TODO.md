# TODO

未来可做事项清单，按价值与成本排序。来源：上游 zkmydgth/MoviePilot-Plugins、pancras-loe/115-station、Ponphil/LitePan 的借鉴分析与本项目遗留项。

## 同步与风控类（来自 115-station / LitePan）

- [x] 【已完成 2026-10-05】快速全量同步模式（规格 `.monkeycode/specs/fast-recursive-scan/`）：改用 115 递归整树端点把请求数从 O(目录数) 压到个位数——`webapi.115.com/files?cid=&show_dir=0` 一次拿子树全部文件（1150/页），配 `proapi.115.com/app/chrome/downfolders` 递归拿全部目录（5000/页，参数 pickcode 可由 cid 本地换算），两边内存拼完整路径；万级库约 10 次请求 vs 现在逐目录 DFS。downfolders 属未公开端点，必须带开关且失败自动降级回现有 DFS 模式（照搬 115-station fast115 的降级纪律）
- [x] 【已完成 2026-11-25】目录级改名迁移与增量删除开关（八巡 115-station `d4251d00`）：增量按 pickcode 识别改名，目录级改名时整目录搬移（含字幕、NFO、剧照等侧车）并清理空父目录；目标目录已存在、目录内仍有未改名文件、一对多或多对一映射时退回逐文件迁移；增量删除改为受 `cleanup_deleted_strm` 开关控制，关闭时只提示不删除
  - 验证：新增 8 个用例（目录搬移侧车、目标冲突回退、部分改名保护、目录+文件双改名、未开开关不删、同轮新增文件、失败根、父子目标不一致）；3.11 stub 与 3.14 真实 SDK 全量 301 用例全绿
- [ ] 行为事件流增量同步（观察项，暂缓）：拉 `proapi.115.com/android/behavior/detail`（失败降级 `webapi.115.com/behavior/detail`，端点状态持久化），以单调递增事件 id 做游标 + 去重，事件自带 pick_code 时零遍历直推 STRM；已知盲区"回收站还原不产生新增事件"须靠低频全量校对兜底（参考 115-station life115/incr115 分层）。决策记录（2026-10-05）：复杂度最高（账户级事件流、目录改名级联、游标状态机），而快速整树扫描落地后"每 15 分钟约 10 个请求全库 diff"已足够便宜可靠，事件流收益缩水——待快速同步上线后按实际同步频率与风控数据再定是否启动。实施红线（115-station `4096552b`+`c96883ef` 两起现场事故背书）：删除类事件只有能定位到具体路径才允许删，推不出位置一律不删、漏删交给下轮全量校对；"台账/记录中同名唯一"不构成删除依据；路径前缀查询禁用未转义 LIKE（`_`/`%` 通配泄漏），改精确等值 + 字节区间
- [ ] 115 开放平台 OpenAPI 通道评估：OAuth PKCE 设备码登录（authDeviceCode → 轮询扫码 → deviceCodeToToken → refreshToken 自动续期，access_token 约 2h），proapi `/open/*` 属官方授权接口、无 UA 指纹风控、错误码明确（770004 频繁 / 406 额度上限 / 401401xx 需刷新 token）；前提是在 open.115.com 申请个人 AppID 且有调用额度，建议先只覆盖列表/目录类只读接口试点，Cookie 通道保留兜底（实现参考 115-station open115.go 与 LitePan drivers/115_Open）
- [ ] 缓存热度保持（可选低优先）：定时对近期播放的 STRM 目标文件做轻量 range 读（首块），保持 115CDN 边缘缓存热度、降低冷文件首播缓冲（参考 LitePan cacheretention：任务时间窗、配置指纹变更重排、与同步/整理类任务互斥调度）
- [ ] 消息通知（最低优先）：同步失败、被风控、签到异常推送 telegram/ntfy/Server酱 任一渠道；桌面工具形态优先级低于以上各项
- [ ] 115 限频响应识别与写操作退避（观察项）：115-station `103f6925`（2026-10-14）实践——限频判定认错误码 190044 且兼容 error 为空只给编号、`errno`/`errNo`/`error_code` 大小写变体的兜底，触发后间隔重试、失败上报透出 115 中文原话；并把真正承压的写端点（如分享转存）补进写频控间隔名单。本仓库现状对限频错误码无专项识别（仅 state=false 通用分支）；当多端播放副本、快速扫描或签到出现频繁撞限频时，参考该提交做限频码集中识别 + 写操作退避，暂不动手。**2026-11-10 实测更新**：流代理模式高密度取流时 115 CDN 返回 `403 / 115 pmt 3-2`，与直链有效期、UA、请求头均无关，与单位时间请求密度强相关；诊断日志（管理面板「播放诊断日志」开关，默认关闭）可记录响应体、直链剩余有效期与外发请求头，限频码识别与退避策略待定，仍按观察处理（详见 `.monkeycode/docs/专有概念/播放模式与诊断.md`）

- [ ] 流代理高密度取流 403（观察项，2026-11-10）：`115 pmt 3-2` 集中出现在 60 秒窗口内取流请求 ≥11 次时；低密度时段与 302 对照时段均未复现工具侧错误。当前按观察处理，不改播放逻辑；候选处置为限频码识别 + 退避/节流、降低 403 后重试与强制换链强度（避免自激放大密度），或继续使用 302 模式规避。需先做等密度对照实验以分离「模式差异」与「密度差异」。

## 功能类

- [x] 【已完成 2026-10-05】配置快照与导出config.json 滚动备份（含 Cookie，注意脱敏提示）+ 管理面板"导出配置"入口；数据库可由全量同步再生，不必备份
- [x] 【已完成 2026-10-18】WebDAV 远端备份闭环（规格 `.monkeycode/specs/webdav-remote-backup/`）：面板手动触发，打包→manifest 自检（CRC+逐条 size/sha256+穿越拒绝）通过后才上传、远端按保留份数滚动清理（软失败补偿）、恢复前 guard 快照+失败回滚；口令加密与周期自动备份按决策暂不做；数据库可由全量同步再生不入库
- [x] ~~危险操作确认有效期（90s 一次性令牌）~~（2026-10-05 实现后同日移除：单机自用场景无实际收益、交互与普通确认难区分，已回退为浏览器弹窗直接确认）
- [ ] 残留清理连带附属文件（观察项）：开启附属元数据下载后，网盘源文件被删除时现有"同步后清理残留"只删 .strm，本地 nfo/剧照/字幕会残留；上游已有实现先例——DDSRem 版 p115strmhelper `b69861b3`（2026-10-09，orphan 刮削产物清理）与 115-station `1c0bc940`（删 STRM 连带删集 NFO 与剧照），待有用户反馈残留问题时参考立项
- [ ] 直链获取兜底链加强（观察项）：115-station `f06973fc`（2026-10-09）给 CDN 直链解析新增"网页端加密 POST"兜底路径；本项目 redirect_service 现有链路为加密下载 API 优先 + SDK 下载兜底，若实测出现直链获取频繁失败，参考该提交评估补第三条链路

## 依赖与升级类

- [x] 【已完成 2026-11-23】p115 依赖升级路线：已对照 DDSRem `7ddd3ec3` + zkmydgth v2.7.19 适配 diff 完成迁移并推送——`p115client==0.0.9.7.2`、`p115cipher>=0.0.6`、解除 `python-concurrenttools<0.1.9` 上限（改钉 `>=0.1.9`）、新增 `call_sleep==0.0.1`；`combined/wheels/` 锁包已同步替换（新增 6 个 wheel，旧版 5 个移入 `_obsolete/` 本地备份，目录已加 .gitignore）
  - 迁移触点（唯一破坏性改动）：`strm_generator.py` 的 `iter_files_with_path` 改为 `iter_files(..., with_path=True)`（import + 调用各 1 处）；wrapper 用到的 14 个实例方法与 `check_response`/`ID_TO_DIRNODE_CACHE`/`get_webapi_origin` 等符号全部零改动存活；`normalize_attr` 删除 `dict_cls` 参数对本仓库无影响
  - 验证：沙箱 3.11（stub 注入）与 uv 3.14 venv（真实 SDK）各跑全量 pytest，289 用例全绿；环境重建已固化为 `scripts/setup_py314.sh`
  - 后续观察：`full_strm_sync` 仍只有 cp312 wheel；2026-11-25 复核确认上游虽已提供 cp314 wheel，但 0.1.6 集成本身需先重写，见下方「full_strm_sync 恢复 Rust 加速」条目
- [x] 【已完成 2026-11-25】p115client 升级 0.0.9.7.2→0.0.9.7.8.3（八巡 DDSRem `d20046b5` + `5eb564d2` 修补 wheel）：同步替换 `python-httpfile` 0.0.6.3、`python-http_request` 0.1.7.2、`python-dictattr` 0.0.5 四个 wheel（旧版移入 `combined/wheels/_obsolete/`）；兼容审计破坏点（`ensure_path` 位置参数、`fs_label_set` 改 `label_id`、`life_recent_operation_items` 默认值、顶层 fs 导出）本仓库均未命中
  - 顺带修复附属文件下载丢弃 `P115URL.headers`：`strm_generator._download_aux_file` 现在把直链绑定头（UA/Cookie）随下载请求转发，避免 115 CDN 按直链 `f` 参数拒绝；新增 3 个用例覆盖 P115URL/dict/无头三种形态
  - 验证：沙箱 3.11（stub 注入）与 uv 3.14 venv（真实 SDK，装的是 `combined/wheels/` 内实际 wheel）均 292 用例全绿，3.14 下逐文件 standalone 全绿
- [x] 【已完成 2026-10-09】发版同日序号版本：现版本一致性校验要求 tag 与 CHANGELOG 顶部日期相等，日内二次发布无路可走；115-station `6f9264b9` 采用"日期版本 + 同日 `-N` 序号当正式版"写法。候选方案：release.yml 校验放行 `YYYYMMDD-N` tag（归一后比对日期部分），配套 CHANGELOG 同日多节或节内追加约定
- [ ] 若未来新增数据库功能：备份 SQLite 时须用 `Connection.backup()` 或先 `wal_checkpoint(TRUNCATE)`，禁止直接复制 WAL 模式下的数据库文件
- [ ] full_strm_sync 恢复 Rust 加速（2026-11-25 复核）：上游 DDSRem V2 wheels 已提供 `full_strm_sync 0.1.6` 的 cp314 ABI wheel，但 0.1.6 的配置与结果契约已变（Processor 需 10 字段配置，返回 `strm_results`/`download_results` 由调用方写盘），本仓库现有集成会在初始化时失败并回退纯 Python。恢复步骤：先重写集成（10 字段配置 + 按结果对象由 Python 写 STRM/附属下载）并用真实 wheel 做非 mock 冒烟，再复制 cp314 wheels 进 `combined/wheels/`；当前 UI 开关已隐藏，启动时固定 `use_rust=False`

## 部署与分发类

- [ ] Docker 化（观察项，待有用户提出 NAS/容器部署需求时再立项落地）：把三个 uvicorn 服务（管理 UI 8100 / Emby 反代 8097 / 302 跳转 3333）容器化。2026-11-16 预研结论如下
  - 现成基础：`main.py --no-tray` 已是前台阻塞 + SIGINT/SIGTERM 优雅退出的标准容器主进程；pystray/Tkinter 均为可选或仅托盘场景；`full_strm_sync` 装不上自动回退纯 Python；STRM 文件内容是 302 跳转 URL（`strm_generator.py` `_write_strm`），不写本地路径，容器路径映射不污染内容
  - 核心前置改动：`_BASE_DIR` 环境变量化（如 `APP_DATA_DIR`）——config_manager.py、database.py、logger.py、checkin_scheduler.py 四处同模式重复定义（frozen exe 用 exe 目录，否则用源码目录），config.json/config.key/data/strm.db/logs/checkin_state.json/config_backups 全落于此；收敛为一处并支持环境变量覆盖，容器内数据卷挂 `/app/data`；webdav_backup.py 经 `CONFIG_FILE.parent` 引用会自动跟随
  - 镜像方案：基底 `python:3.14-slim`，pip 安装 requirements.txt（p115client 系有 Linux x86_64 wheel），`full_strm_sync` 沿用 CI 的 per-wheel 容错 SKIP 模式；`CMD ["python", "main.py", "--no-tray"]`；不把 config.key/数据文件打进镜像层
  - 编排要点：暴露 8100/8097/3333 三端口（302 模式下播放客户端须能直连容器 IP）；STRM 输出目录与 Emby 同源挂载（工具写、Emby 读）；`TZ=Asia/Shanghai`（签到按指定时段时间窗随机触发，容器默认 UTC 会错档）；管理界面目录浏览/路径填写接口确认指向容器内挂载路径
  - GUI 功能降级：托盘、注册表开机自启、外部播放器一键调用在容器内无意义，README 的 Docker 章节需注明
  - 发布渠道：**无 Docker Hub 账号，镜像发布走 GitHub Container Registry**（`ghcr.io/cdlongbow/115strmtool`），在 release workflow 里用 `GITHUB_TOKEN` 推送，与现有日期 tag 发版流程串联；镜像 tag 与版本 tag 对应
  - 风险评估：业务逻辑零改动，改动集中在 `_BASE_DIR` 收敛 + Dockerfile/compose + README，风险低；与 Windows exe 定位互补（exe 保桌面托盘体验，Docker 补 NAS/服务器场景）

## 测试类

- [ ] 变异测试套件（可选低优先）：参考上游 mutation 脚本"跳过即失败"收口模式；508 用例规模下性价比一般，用例规模明显增长后再评估
