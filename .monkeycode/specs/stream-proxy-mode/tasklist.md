# 需求实施计划 — stream-proxy-mode（局域网流代理模式）

依据：`requirements.md`（R1-R7）与 `design.md`（2026-11-08 已确认三态单选/内网强制/重试默认档）。

- [x] 1. 配置模型：播放模式三态枚举与旧字段兼容迁移
  - [x] 1.1 `combined/config_manager.py` 定义 `PlaybackMode(str, Enum)`（`disabled`/`redirect`/`stream_proxy`）；`EmbyConfig` 以 `playback_mode` 枚举字段替换 `redirect_mode` 布尔字段；`model_validator(mode="before")` 将旧 JSON `redirect_mode: true|false` 映射为 `redirect|disabled`；非法取值回落 `disabled` 并记警告日志（R1.AC1、R1.AC2）
  - [x] 1.2 `combined/main.py:155` 代理启动注入改为 `playback_mode=config.get(...)`（枚举字符串）
  - [x] 1.3 配置单元测试：旧 JSON 两值映射、新字段读写、非法值回落（对齐 `test_config_manager.py` 模式）

- [x] 2. 管理 API：配置接口三态化
  - [x] 2.1 `combined/admin_api.py` `ConfigUpdate` 的 `redirect_mode: Optional[bool]` 替换为 `playback_mode: Optional[str]`；请求体含旧 `redirect_mode` 布尔字段时兼容映射为枚举值；非法取值 422（R1.AC3、R1.AC5）
  - [x] 2.2 配置保存后沿用既有 `_restart_emby_callback` 路径重建代理 app，使新播放模式对新请求即时生效（R1.AC3、R1.AC4）
  - [x] 2.3 API 兼容测试：旧字段写、新字段读，非法值 422（对齐 `test_admin_config.py` 模式）

- [x] 3. 管理面板：播放模式单选控件
  - [x] 3.1 `combined/web/index.html:226-227` 将 `emby_redirect_mode` checkbox 替换为三选项单选组（关闭 / 302 直链 / 流代理（局域网））+ 一行用途说明文字（R1.AC1）
  - [x] 3.2 `web/index.html:538/549` 配置回显与保存字段名同步为 `playback_mode`，提交走既有配置保存通道（R1.AC3、R1.AC5）

- [x] 4. proxy_app：工厂签名与媒体路由三态分发
  - [x] 4.1 `combined/proxy_app.py:516` `create_app(redirect_mode: bool = False, ...)` 改为 `create_app(playback_mode: str = ..., ...)` 并持有枚举；工厂内全部 `redirect_mode` 判断点改枚举比较（R6.AC1）
  - [x] 4.2 `_handle_media`（619 行附近）三态分发：`disabled`→`_reverse_proxy`；`redirect`→现有 `_try_media_response`；`stream_proxy`→网段门禁→`_resolve_media_target`→`_proxy_stream`（R1、R5、R6）
  - [x] 4.3 PlaybackInfo 处理器 `proxy_app.py:1110` 分支改为仅 `redirect` 态替换 MediaSources.Path 直链；`stream_proxy`/`disabled` 跳过替换；DirectPlay 强注与 STRM 源缓存回填三态保持（R6.AC1）

- [x] 5. 流代理核心：解析复用、取流转发、换链重试
  - [x] 5.1 从 `_try_media_response`（719-822 行）抽出 `_resolve_media_target(item_id, api_key, request, *, skip_final_url_cache: bool) -> Optional[str]`：保持用户缓存→`playback_url_cache`（可为跳过级）→PlaybackInfo→STRM 源缓存→`_resolve_redirect` 的次序、缓存键与 TTL 语义、`key_lock` 防击穿不变（R4.AC1、R4.AC2、P5）
  - [x] 5.2 实现 `_proxy_stream(request, direct_url)`：`http_client_no_follow` GET/HEAD 转发 `_build_forward_headers(request)`（含 Range 与播放端 UA）；响应头白名单转发（`Content-Type`/`Content-Length`/`Content-Range`/`Accept-Ranges` + 状态码），丢弃 `Content-Disposition`/`ETag`/`Last-Modified`/`Expires`/`Cache-Control`/`Set-Cookie`/传输编码头；`aiter_bytes(65536)` + `finally: resp.aclose()` 流式回传（R2；P2、P3、P4）
  - [x] 5.3 实现换链重试状态机（仅响应头产出前）：上游连接异常、403/404、带 Range 得 200 → 退避（0.5s/1.5s）→ `skip_final_url_cache=True` 重解析 → 重试；上限 `STREAM_RETRY_MAX=2`、自请求进入起预算 10 秒；超限 `JSONResponse` 502 + `logger.error`（item_id、次数、末次错误）（R3；design「半身后断」边界按设计执行）
  - [x] 5.4 流代理单元测试 `combined/test_stream_proxy_mode.py`：三态路由快照回归（disabled/redirect 行为与实施前一致，P1）；`stream_proxy` 206/Range/白名单头/丢弃头断言（P2）；无 115 域名泄漏断言（P3）（standalone 单文件、复用 `conftest.py` 桩与 `test_redirect_method.py` 本地 mock server 模式）
  - [x] 5.5 自愈与并发单元测试：403→跳缓存重解析→第二次成功（断言解析调用 2 次）；403×3→502（打桩 `asyncio.sleep`）；HEAD 空头体；同 key 并发 5 路仅 1 次解析（对齐 `test_redirect_stampede.py`）；客户端断开后上游 closed（P4）
  - [x] 5.6 配置兼容测试：旧 `redirect_mode` JSON/请求体映射、非法值回落与 422（补 1.3/2.3 未尽项）

- [x] 6. 局域网网段门禁（强制内置）
  - [x] 6.1 实现 `_is_private_source(request) -> bool`：复用既有 `_real_client_ip`（XFF/X-Real-IP 口径）+ `ipaddress.ip_address(...).is_private`（覆盖 RFC1918/环回/ULA）（R5.AC1、R5.AC3）
  - [x] 6.2 `stream_proxy` 模式非私有来源：返回 403 + 来源 IP 日志；确认配置与 API 均不新增该限制开关（R5.AC2、R5.AC4、P6）
  - [x] 6.3 网段单元测试：私有/公网/XFF 改写三类用例（P6）

- [x] 7. 检查点 — 本地门禁全绿
  - 确保所有测试通过，如有疑问请询问用户
  - 仓库根 `ruff check .`、`cd combined && python3 -m pytest -q` 全绿；`--collect-only -q` 取准确用例数

- [x] 8. 文档同步
  - [x] 8.1 四处用例数同步：README.md:67、TODO.md:37、.monkeycode/docs/INDEX.md:78、.monkeycode/docs/DEVELOPER_GUIDE.md:111（基线 229，更新为实测数）
  - [x] 8.2 `.monkeycode/docs/INTERFACES.md` 与 `DEVELOPER_GUIDE.md` 补"播放模式"三态配置说明

- [x] 9. 最终检查点 — 交付就绪
  - 确保所有测试通过，如有疑问请询问用户
  - 逐改动文件异常字符扫描（kana/U+FFFD/全角污染），git diff 复核；不提交，等用户指令
