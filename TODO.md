# TODO

未来可做事项清单，按价值与成本排序。来源：上游 zkmydgth/MoviePilot-Plugins、pancras-loe/115-station、Ponphil/LitePan 的借鉴分析与本项目遗留项。

## 同步与风控类（来自 115-station / LitePan）

- [x] 【已完成 2026-10-05】快速全量同步模式（规格 `.monkeycode/specs/fast-recursive-scan/`）：改用 115 递归整树端点把请求数从 O(目录数) 压到个位数——`webapi.115.com/files?cid=&show_dir=0` 一次拿子树全部文件（1150/页），配 `proapi.115.com/app/chrome/downfolders` 递归拿全部目录（5000/页，参数 pickcode 可由 cid 本地换算），两边内存拼完整路径；万级库约 10 次请求 vs 现在逐目录 DFS。downfolders 属未公开端点，必须带开关且失败自动降级回现有 DFS 模式（照搬 115-station fast115 的降级纪律）
- [ ] 行为事件流增量同步（观察项，暂缓）：拉 `proapi.115.com/android/behavior/detail`（失败降级 `webapi.115.com/behavior/detail`，端点状态持久化），以单调递增事件 id 做游标 + 去重，事件自带 pick_code 时零遍历直推 STRM；已知盲区"回收站还原不产生新增事件"须靠低频全量校对兜底（参考 115-station life115/incr115 分层）。决策记录（2026-10-05）：复杂度最高（账户级事件流、目录改名级联、游标状态机），而快速整树扫描落地后"每 15 分钟约 10 个请求全库 diff"已足够便宜可靠，事件流收益缩水——待快速同步上线后按实际同步频率与风控数据再定是否启动
- [ ] 115 开放平台 OpenAPI 通道评估：OAuth PKCE 设备码登录（authDeviceCode → 轮询扫码 → deviceCodeToToken → refreshToken 自动续期，access_token 约 2h），proapi `/open/*` 属官方授权接口、无 UA 指纹风控、错误码明确（770004 频繁 / 406 额度上限 / 401401xx 需刷新 token）；前提是在 open.115.com 申请个人 AppID 且有调用额度，建议先只覆盖列表/目录类只读接口试点，Cookie 通道保留兜底（实现参考 115-station open115.go 与 LitePan drivers/115_Open）
- [ ] 缓存热度保持（可选低优先）：定时对近期播放的 STRM 目标文件做轻量 range 读（首块），保持 115CDN 边缘缓存热度、降低冷文件首播缓冲（参考 LitePan cacheretention：任务时间窗、配置指纹变更重排、与同步/整理类任务互斥调度）
- [ ] 消息通知（最低优先）：同步失败、被风控、签到异常推送 telegram/ntfy/Server酱 任一渠道；桌面工具形态优先级低于以上各项

## 功能类

- [x] 【已完成 2026-10-05】配置快照与导出config.json 滚动备份（含 Cookie，注意脱敏提示）+ 管理面板"导出配置"入口；数据库可由全量同步再生，不必备份
- [ ] WebDAV 远端备份闭环：仅在用户明确提出后立项。参考上游 ConfigBackup v3.2.0 做法——标准库实现零新增依赖、包自检通过后才上传、远端清理复用本地保留份数/天数规则、备份清单写入 parts 并支持旧包降级
- [x] ~~危险操作确认有效期（90s 一次性令牌）~~（2026-10-05 实现后同日移除：单机自用场景无实际收益、交互与普通确认难区分，已回退为浏览器弹窗直接确认）
- [ ] 残留清理连带附属文件（观察项）：开启附属元数据下载后，网盘源文件被删除时现有"同步后清理残留"只删 .strm，本地 nfo/剧照/字幕会残留；上游 115-station `1c0bc940` 的做法是删 STRM 时连带删其附属文件，待有用户反馈残留问题时再立项

## 依赖与升级类

- [ ] p115 依赖升级路线：`python-concurrenttools<0.1.9` 上限的解除以完成 p115client 0.0.9.7.x 迁移为前提（iterdir 新 API 重构，符号删改约 29 处，属较大工作量；期间维持 0.0.9.6.5.1 现状）。备选兜底：上游 `7e04f7a2` 的运行时别名方案（新名存在且旧名缺失时把 `thread_conmap/async_conmap` 别名回 `threadpool_map/taskgroup_map`，约 200 行含测试）；仅当未来需兼容无法强制降级的共享环境时再移植
- [ ] 若未来新增数据库功能：备份 SQLite 时须用 `Connection.backup()` 或先 `wal_checkpoint(TRUNCATE)`，禁止直接复制 WAL 模式下的数据库文件

## 测试类

- [ ] 变异测试套件（可选低优先）：参考上游 mutation 脚本"跳过即失败"收口模式；165 用例规模下性价比一般，用例规模明显增长后再评估