# TODO

未来可做事项清单，按价值与成本排序。来源：上游 zkmydgth/MoviePilot-Plugins 借鉴分析与本项目遗留项。

## 功能类

- [ ] 配置备份（轻量版）：config.json 滚动备份（含 Cookie，注意脱敏提示）+ 管理面板"导出配置"入口；数据库可由全量同步再生，不必备份
- [ ] WebDAV 远端备份闭环：仅在用户明确提出后立项。参考上游 ConfigBackup v3.2.0 做法——标准库实现零新增依赖、包自检通过后才上传、远端清理复用本地保留份数/天数规则、备份清单写入 parts 并支持旧包降级
- [ ] 危险操作确认过期：管理面板删除类/覆盖类操作的确认窗口加有效期，超时须重新确认（参考上游 ConfigBackup v3.1.1）

## 依赖与升级类

- [ ] p115 依赖升级路线：`python-concurrenttools<0.1.9` 上限的解除以完成 p115client 0.0.9.7.x 迁移为前提（iterdir 新 API 重构，符号删改约 29 处，属较大工作量；期间维持 0.0.9.6.5.1 现状）。备选兜底：上游 `7e04f7a2` 的运行时别名方案（新名存在且旧名缺失时把 `thread_conmap/async_conmap` 别名回 `threadpool_map/taskgroup_map`，约 200 行含测试）；仅当未来需兼容无法强制降级的共享环境时再移植
- [ ] 若未来新增数据库功能：备份 SQLite 时须用 `Connection.backup()` 或先 `wal_checkpoint(TRUNCATE)`，禁止直接复制 WAL 模式下的 db 文件

## 测试类

- [ ] 变异测试套件（可选低优先）：参考上游 mutation 脚本"跳过即失败"收口模式；140 用例规模下性价比一般，用例规模明显增长后再评估
