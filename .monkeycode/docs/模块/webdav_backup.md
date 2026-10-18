# webdav_backup

配置远端备份模块。将 `config.json`、`config.key` 与签到状态打包为带清单的 zip 档案上传到 WebDAV 服务器，支持列表、下载与一键恢复；打包后先做完整性自检再上传，恢复前保留当前配置快照、失败自动回滚。当前为手动触发（面板"备份"页），不做周期自动备份。

## 结构

```
webdav_backup.py
├── _friendly_http_error()      # WebDAV 状态码归类为人话（401/403/404/409/507 等）
├── _parse_int_lenient()        # 宽容解析数字（非法值回退 0）
├── WebdavClient（class）       # httpx + BasicAuth 的轻量 WebDAV 客户端
│   ├── _url()                  # urljoin + 逐段 quote（中文目录安全）
│   ├── _request()              # 统一请求封装，异常包装为 ServiceError
│   ├── test_connection()       # 建目录→列表→写探针→删探针 四步连通性检查
│   ├── ensure_dir()            # 逐级 MKCOL（201/204/405 宽容）
│   ├── list_archives()         # PROPFIND depth=1 解析 multistatus，过滤档案名按字典序排序
│   ├── put() / get() / delete()
├── BackupService（class）
│   ├── run_backup()            # 备份闭环：非阻塞锁→打包→自检→命名去重→PUT→GET 核对→滚动清理
│   ├── restore_backup()        # 恢复闭环：白名单→下载→自检→guard 快照→原子覆盖，失败回滚
│   ├── list_remote() / download_remote()  # 远端列表与附件下载（同样过名称白名单）
│   ├── _create_archive()       # 临时目录打包（成员白名单 + manifest 记录 size/sha256）
│   ├── _verify_archive()       # CRC + 清单逐条核对 + 路径穿越拒绝 + 清单外文件拒绝
│   ├── _resolve_remote_name()  # 同秒撞名追加 -2..-99 序号
│   ├── _prune()                # 按名排序保留 keep_count 份（软失败，下轮补偿）
│   ├── _atomic_write_bytes()   # 同目录临时文件 + os.replace 原子写
│   └── _rotate_guards()        # 恢复前 guard 快照保留最近 5 组
└── backup_service              # 模块级单例（admin_api 路由持有）
```

## 关键设计

- **档案命名**：`115strmtool-backup-<YYYYmmdd-HHMMSS>[-N].zip`，字典序即时间序，排序不依赖服务器 modified
- **完整性**：档案内含 `manifest.json`（app_version/created_at/members 的 size+sha256）；上传后 GET 回读核对大小，不一致则本轮不做清理；恢复前再次全量自检
- **恢复安全**：guard 快照命名 `restore-guard-<timestamp>.json/.key`（避开 `config-*.json` 快照 glob），覆盖写全程持有 `config_manager._lock`，任何一步失败整体回滚到写入前状态；成功后热重载路由层触发 p115/Emby 重建
- **秘密字段**：`webdav.password` 与 `p115.cookie` 共用 `_SECRET_FIELDS` 加密机制（`#ENC#` 前缀落盘），`mask_config` 统一脱敏
- **可测试性**：`BackupService._transport` 可注入 `httpx.MockTransport` 做端到端测试；`_create_archive`/`_atomic_write_bytes` 可 monkeypatch 做故障注入

## 错误语义

- 未启用/未配置地址/名称非法 → `BadRequestError`（400）
- 任务互斥占用 → `ServiceError`（409）
- 网络、认证、权限、校验失败 → `ServiceError`（携带归类后的中文提示）
