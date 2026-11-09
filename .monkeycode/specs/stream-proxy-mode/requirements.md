# Requirements Document

## Introduction

为家庭局域网场景新增"流代理模式"开关。现状 302 直链模式下，播放端（Emby 第三方客户端如 OK 影视、默影视）拿到 115 CDN 签名直链后直接连接 CDN；直链有效期有限（分钟级），播放器暂停超过时效后恢复播放、或长时间暂停后 seek，会拿着已失效的直链重连 CDN 导致卡死，必须退出重播（重新走完整的取链链路）才能恢复。

流代理模式把"发直链给播放端"改成"工具自己取流转发"：播放端始终只面对工具代理口的稳定地址，暂停再久、seek 到任何位置都重新向工具发起 Range 请求，工具负责向 115 取新直链续传，对播放端透明。115 域名只出现在工具与 115 之间，播放端全程接触不到 115。

本特性面向家庭局域网自用场景：播放端数量少（1~5 路并发）、全部处于可信内网、Emby 服务器部署不变、播放端不可配置（第三方 App 无法修改行为）。

## Glossary

- **System**: 115StrmTool（本项目管理面板 + 302 跳转服务 + Emby 前置代理的整体）
- **播放模式（playback mode）**: 面板上的三态互斥单选配置（`disabled` / `redirect` / `stream_proxy`），决定媒体路由的响应方式；流代理模式与 302 直链模式由此枚举承载，同一时刻至多一种媒体处理方式生效
- **流代理模式（stream proxy mode）**: 播放模式取值之一，媒体流请求由工具代理回传字节的运行模式
- **302 直链模式（redirect mode）**: 播放模式取值之一，对应现有行为，工具向播放端下发 302 响应，播放端直连 115 CDN；配置字段沿用现有 `redirect_mode` 语义来源（true 映射 `redirect`，false 映射 `disabled`）
- **直链（direct URL）**: 115 签名下载地址，绑定 UA、有效期分钟级、绑定客户端 IP
- **播放端 / 客户端**: 电视上的 Emby 第三方客户端（OK 影视、默影视等，播放内核为 ijkplayer 或 ExoPlayer）
- **上游**: 工具取流时的数据来源，即 115 CDN
- **Range 请求 / 206 响应**: HTTP 分段请求与部分内容响应，播放器 seek 与断点续传的机制
- **解析链**: 工具内"已解析 URL 缓存 → PlaybackInfo → STRM 源缓存"的既有三级媒体地址解析策略
- **媒体路由**: 工具前置代理中命中媒体流的请求路径（`/Videos/{id}/stream` 等 MEDIA_ROUTES 集合）

## Requirements

### Requirement 1: 播放模式三态单选开关

**User Story:** AS 家庭局域网用户, I want 在管理面板用一个三态单选（关闭 / 302 直链 / 流代理模式）决定播放方式, so that 任意时刻只有一种媒体处理方式生效，配置组合语义唯一。

#### Acceptance Criteria

1. THE System SHALL 以三态互斥单选配置项（`disabled` / `redirect` / `stream_proxy`）承载媒体处理方式，面板呈现为单选控件（取代现有 `redirect_mode` 布尔开关的独立呈现）。
2. WHEN 用户加载既有配置且该配置仅含 `redirect_mode` 布尔字段, the System SHALL 按 `true` 映射 `redirect`、`false` 映射 `disabled` 完成读取与展示，保存时写入新枚举字段。
3. WHEN 用户在管理面板保存"播放模式=流代理模式", the System SHALL 使该配置对后续到达的媒体路由请求即时生效，无需重启进程。
4. WHILE 播放模式取值 `disabled` 或 `redirect`, the System SHALL 处理媒体路由请求的行为与本次特性实施前的既有行为逐项一致（含 302 下发、三级解析链、缓存击穿防护、非媒体请求的反向代理透传）。
5. WHEN 配置项保存成功, the System SHALL 将该配置项随现有配置一同持久化，并纳入配置快照与备份成员范围。

### Requirement 2: 流代理取流转发

**User Story:** AS 播放端, I want 向工具的稳定地址请求媒体流时拿到字节正确、语义正确的 HTTP 响应, so that 任意暂停时长与任意 seek 位置都能正常续播。

#### Acceptance Criteria

1. WHEN 播放端在流代理模式下以 GET 请求命中媒体路由（含 `Range` 请求头）, the System SHALL 经解析链取得 115 直链，以相同 `Range` 值向 115 CDN 发起请求，并将上游响应状态码、`Content-Range`、`Content-Length`、`Content-Type` 及响应体逐块转发给播放端。
2. WHEN 播放端发起的媒体流请求不含 `Range` 请求头, the System SHALL 向上游发起无 Range 请求并流式转发完整响应。
3. WHEN 播放端以 HEAD 方法命中媒体路由, the System SHALL 向上游转发 HEAD 请求并回传响应头（响应体固定为空），不拉取媒体字节。
4. WHILE 工具与 115 CDN 之间的媒体传输在进行, IF 播放端断开连接或取消请求, the System SHALL 立即关闭与该请求关联的全部上游连接并释放关联资源。
5. IF 上游响应包含 `Content-Disposition: attachment` 或其他会诱导播放端将媒体流按文件下载处理的响应头, the System SHALL 在转发给播放端的响应中移除该响应头。
6. IF 上游响应包含 `ETag`、`Last-Modified` 等源自 115 CDN 的响应校验头, the System SHALL 在转发给播放端的响应中移除该响应头，播放端凭 `Content-Length` 与 `Content-Range` 校验分段。

### Requirement 3: 直链失效自愈

**User Story:** AS 家庭局域网用户, I want 工具在 115 直链失效后自动换新链续传, so that 播放端拿着失效票据重连时依然播放流畅，无需退出重播。

#### Acceptance Criteria

1. IF 流代理模式下工具对 115 CDN 的上游连接失败、收到 HTTP 403/404 或收到与请求 Range 不匹配的状态码, the System SHALL 以绕过"已解析 URL 缓存"的方式重新执行解析链获取新直链，并向新直链重试原请求（含原 Range 值）。
2. IF 上述重取链重试在 2 次上限内成功, the System SHALL 继续正常传输，播放端在该过程中收到的是连续或重试后完整的响应，无需重新发起播放。
3. IF 重取链重试达到上限后仍失败, the System SHALL 向播放端返回 HTTP 502 并在日志记录媒体项标识、失败链路与尝试次数。
4. WHILE System 执行重取链重试, 单次播放请求因重连累计的额外首字节延迟 SHALL 控制在 10 秒以内（含退避等待，超限即按重连失败处理）。
5. WHILE 流代理已向播放端产出响应头与部分响应体, IF 上游媒体传输在半身后中断, the System SHALL 保持已产出的响应头不变，按已交付给播放端的字节偏移换新直链以 `Range: bytes=<偏移>-` 续传，使播放端收到的字节序列保持连续无重复；续传在单次中断 3 次尝试与 8 秒预算内仍未成功时 SHALL 结束响应体并记录日志，由播放端按截断重新发起 Range 请求。

### Requirement 4: 解析与并发复用

**User Story:** AS 多终端家庭用户, I want 流代理模式复用既有的地址解析缓存与击穿防护, so that 多台电视同时看片不产生对 Emby 与 115 接口的重复请求。

#### Acceptance Criteria

1. WHILE 流代理模式开启, the System SHALL 通过既有三级解析链（已解析 URL 缓存 → PlaybackInfo → STRM 源缓存）获取 115 直链，沿用其缓存键构成与 TTL 语义（含按 `expires_time` 收敛 TTL 的既有规则）。
2. WHEN 同一缓存键的媒体流请求并发到达, the System SHALL 对解析链执行与 302 直链模式同级的击穿防护：单轮解析只执行一次，其余额求复用同一结果。
3. WHILE 多个播放端分别建立流代理连接, 每条连接 SHALL 持有各自独立的上游响应对象，单条连接的关闭、失败、重连对同片其他连接的影响范围限定在"共享解析链缓存结果"。

### Requirement 5: 局域网安全边界

**User Story:** AS 无公网的家庭用户, I want 流代理只服务内网播放端, so that 该能力不会被无意间暴露到公网造成裸奔。

#### Acceptance Criteria

1. WHILE 流代理模式开启, the System SHALL 仅对来源地址属于私有网段（RFC 1918 IPv4 `10/8`、`172.16/12`、`192.168/16`，IPv6 ULA `fc00::/7`，及环回地址）的播放端请求回传媒体流。
2. IF 非私有网段来源请求命中处于流代理模式的媒体路由, the System SHALL 返回 HTTP 403 并记录来源 IP 日志。
3. IF 请求经过反向代理带来 `X-Forwarded-For`/`X-Real-IP` 头, the System SHALL 沿用既有真实客户端 IP 识别规则判定网段归属。
4. THE System SHALL 将该私有网段限制作为流代理模式的固定属性提供（配置项与管理面板均无关闭该限制的入口）。

### Requirement 6: Emby 服务器零配置依赖

**User Story:** AS 家庭局域网用户, I want 开启流代理模式后 Emby 服务器端保持原样, so that 不学习 Emby 高级设置、不改 Emby 任何配置。

#### Acceptance Criteria

1. THE System SHALL 在流代理模式开启时对 Emby 服务器的全部请求行为（含 PlaybackInfo 强制 DirectPlay 注入、STRM 源缓存回填、WS 代理、非媒体路由透传）与流代理模式关闭时逐项一致。

### Requirement 7: 与既有特性兼容

**User Story:** AS 开启过多端播放副本/外部播放器功能的用户, I want 流代理模式与这些既有功能共存, so that 开启开关后其他功能行为不变。

#### Acceptance Criteria

1. WHEN 多端播放副本（same_playback）开启且播放端请求命中多端并发场景, the System SHALL 沿用既有的副本创建与清理逻辑，流代理取流对副本 pickcode 的直链处理与对原文件的直链处理行为一致。
2. WHEN 外部播放器链接解码路由收到请求, the System SHALL 保持既有 302 解码跳转行为不受流代理模式影响。

## 已确认决策（2026-11-08 与用户确认）

1. 面板组织：`redirect_mode` 布尔开关与"流代理模式"合并为三态互斥单选（关闭 / 302 直链 / 流代理模式），老配置按 R1.AC2 映射兼容。
2. 内网保护：私有网段限制为流代理模式固定属性，强制内置，无关闭入口（R5.AC4）。
3. 直链失效自愈重试：默认档——重新取链最多 2 次、单次播放请求累计额外首字节延迟上限 10 秒、超限返回 HTTP 502 并记日志（R3）。
