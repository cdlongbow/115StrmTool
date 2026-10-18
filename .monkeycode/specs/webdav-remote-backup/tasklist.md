# 实施任务清单：WebDAV 远端备份

依据：本目录 `requirements.md`（六个需求，2026-10-18 决策定稿）与 `design.md`。
门禁纪律：每步完成后 `ruff check .` 可绿；T8 统一跑 `cd combined && python3 -m pytest -q` 全量绿 + 异常字符扫描。提交与 CHANGELOG 由用户确认后执行。

- [x] T1 **「已完成 2026-10-18」** 配置模型与秘密字段泛化：`config_manager.py` 新增 `WebdavBackupConfig` 挂入 `RootConfig.webdav`；`_write`/`load` 的秘密加解密由 `p115.cookie` 单字段泛化为字段表（含 `webdav.password`），存量无 `#ENC#` 前缀值按明文兼容；`mask_config` 增加 `webdav.password` 掩码。测试：默认值、落盘加密回读明文、掩码展示、旧明文兼容
- [x] T2 **「已完成 2026-10-18」** `combined/webdav_backup.py` 之 `WebdavClient`：httpx.BasicAuth、`_url` urljoin+quote、MKCOL 逐级建目录（201/204/405 宽容，403 等报错）、PROPFIND depth1 解析 multistatus 过滤档案名、`put/get/delete`、`test_connection`（建目录→列表→写探针→删探针，四步各自归类故障：网络不可达/认证失败/无写入权限）。测试：MockTransport 造 207/201/401/403/ConnectError；列表排序=名字典序
- [x] T3 **「已完成 2026-10-18」** 打包与自检：临时目录暂存、成员白名单映射（config.json、config.key 必备，checkin_state.json 可选，config_backups/config-*.json 全量入 `backups/`）、manifest（app_version/created_at/members{path,size,sha256}）、`_verify_archive`（testzip CRC + manifest 逐条 size/sha256 + 必备成员 + `..`/绝对路径/盘符拒绝）。测试：manifest 正确性、篡改字节→CRC 失败、改 size、删成员、注入穿越条目、缺 config.key 拒绝
- [x] T4 **「已完成 2026-10-18」** `run_backup` 上传闭环：非阻塞锁忙拒、自检门禁先行（失败零网络写）、同名追加 `-N` 序号、PUT→GET 大小核对、`_prune_remote` 按名排序保留 `keep_count`（清理失败软失败补偿）、成功返回 `{ok,name,size}`。测试：成功路径、核对失败零删除、清理保留 N 份、软失败、同秒撞名、并发拒绝、enabled=false 拒绝
- [x] T5 **「已完成 2026-10-18」** `restore_backup` 恢复闭环：download→`BACKUP_NAME_RE` 全名白名单校验→`_verify_archive`→guard 快照（`config_backups/restore-guard-<ts>-*`）→`os.replace` 覆盖 config.json/config.key（含则 checkin_state）→异常时 guard 整体还原；成功由路由层触发 `_restart_p115_callback/_restart_emby_callback`，下载端点同样过白名单。测试：恢复全流程、校验失败中止、覆盖异常还原、名称非法拒绝
- [x] T6 **「已完成 2026-10-18」** `admin_api.py` 路由组：GET/POST `/backup/webdav/config`（password 掩码透传保留）、POST `/backup/webdav/test`、POST `/backup/run`、GET `/backup/remote`、GET `/backup/remote/download`（附件响应）、POST `/backup/restore`（成功后调重启回调）。测试：掩码保留不覆盖真值、禁用拒绝、restore 触发回调断言
- [x] T7 **「已完成 2026-10-18」** 面板 `web/index.html` 备份标签页：设置区（含测试连接/保存）、立即备份与结果卡、远端列表（刷新、fetch→blob 下载、confirm 恢复）。完成后起服务人工验证页面渲染
- [x] T8 **「已完成 2026-10-18」** 收口：全量 `ruff check .` + `python -m pytest -q`；改动文件异常字符扫描；`INDEX.md` 模块表与用例数同步更新
