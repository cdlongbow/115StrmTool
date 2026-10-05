# Requirements Document

## Introduction

快速整树扫描（fast-recursive-scan）：将 115StrmTool 的全量/增量同步依赖的网盘遍历，从"逐目录 DFS（每个目录一次请求）"升级为"递归整树端点（文件整树 + 目录整树两条分页流，内存拼路径）"，把万级文件库的请求数压到约 10 次量级，并在递归端点失效时无缝降级回现有 DFS 遍历。

参考实现：115-station `fast115.go`（递归端点组合与降级纪律）、`pickcode115.go`（cid 与 pickcode 本地换算，本项目以既有依赖 `p115pickcode` 等价实现）。

## Glossary

- **同步根（Sync Root）**: 用户在配置中声明的一个"网盘目录 → 本地目录"映射中的网盘侧目录（以 cid 标识）
- **DFS 遍历（Legacy Scan）**: 现有 `_iter_files_115`，逐目录调用 Android API 递归产出文件属性
- **快速扫描（Fast Scan）**: 本特性新增的遍历模式，由"递归文件列表 + 递归目录表"两条端点流合成文件属性
- **不动点（Stable Point）**: 同一账号固定的 4 位串，用于 cid 与 pickcode 本地互转，可从任意已知 pickcode 反推
- **降级（Fallback）**: 快速扫描不可用时，对受影响同步根改走 DFS 遍历
- **属性字典（Attr Dict）**: 遍历产出的统一文件属性结构（文件名、大小、sha1、pickcode、父路径等），供同步 diff 与 STRM 写入消费

## Requirements

### Requirement 1: 递归整树扫描

**User Story:** AS 媒体库用户, I want 全量或增量同步以更少的 API 请求完成扫描, so that 万级文件库的同步从数十分钟缩短到分钟级且降低风控暴露

#### Acceptance Criteria

1. WHEN 同步启动且快速扫描开关开启, 系统 SHALL 对每个同步根使用"递归文件列表（show_dir=0，1150 条/页）+ 递归目录表（downfolders，5000 条/页）"获取整棵子树数据
2. WHEN 快速扫描完成一个同步根的拉取, 系统 SHALL 将两条端点流合并为与 DFS 遍历字段一致的属性字典（文件名、大小、sha1、pickcode、网盘完整路径）
3. WHILE 快速扫描正在拉取, 系统 SHALL 以已收文件数更新现有进度上报，并 SHALL 在收到取消信号后停止后续分页请求
4. IF 递归分页出现空页且未到声明总数, 系统 SHALL 以已收条目结束该端点流并记录 WARNING（防御 115 端计数漂移）
5. WHERE 快速扫描的分页大小与最大页数, 系统 SHALL 使用防御性上限（文件页上限按 count 收敛，目录页上限 500 页）

### Requirement 2: 目录 pickcode 本地换算

**User Story:** AS 媒体库用户, I want 快速扫描不额外消耗 API 配额获取目录 pickcode, so that 递归目录表端点可直接命中

#### Acceptance Criteria

1. WHEN 快速扫描需要同步根的目录 pickcode, 系统 SHALL 从首文件页任一条目的 pickcode 反推不动点，再以既有依赖 `p115pickcode` 由 cid 换算目录 pickcode（零额外请求）
2. IF 首文件页为空或所有条目均无法反推不动点, 系统 SHALL 对该同步根执行降级
3. IF 换算逻辑抛出异常, 系统 SHALL 捕获并对该同步根执行降级（异常入日志）

### Requirement 3: 无缝降级

**User Story:** AS 媒体库用户, I want 未公开递归端点失效时同步照常完成, so that 一次同步不因快速扫描不可用而失败

#### Acceptance Criteria

1. IF 快速扫描任一请求失败、被拒（state=false、4xx、405）或响应结构无法解析, 系统 SHALL 放弃该同步根的快速扫描并对该根执行降级
2. WHEN 系统对同步根执行降级, 系统 SHALL 记录 WARNING（含失败端点与错误摘要）并写入该轮同步历史的 message 字段
3. WHEN 系统执行降级, 同步 SHALL 以 DFS 遍历完成该同步根，本轮结果与其余同步根继续正常处理
4. IF 配置中快速扫描开关关闭, 系统 SHALL 直接使用 DFS 遍历（跳过快速扫描全部逻辑）

### Requirement 4: 配置与可观测

**User Story:** AS 管理面板用户, I want 控制并看到扫描模式, so that 端点变动时可自行关闭特性并定位原因

#### Acceptance Criteria

1. WHERE 快速扫描配置项, 系统 SHALL 提供布尔配置 `fast_scan`，默认开启，随配置持久化并经现有配置接口读写
2. WHEN 一轮同步结束, 系统 SHALL 在同步历史消息中体现各同步根实际使用的扫描模式（快速或降级）
3. WHEN 快速扫描被使用, 系统 SHALL 在 INFO 日志输出每个同步根的文件数、目录数与请求次数

### Requirement 5: 行为等价性

**User Story:** AS 媒体库用户, I want 快速扫描与 DFS 遍历产生一致结果, so that 开启特性不改变同步语义

#### Acceptance Criteria

1. GIVEN 同一网盘子树的固定快照, 系统 SHALL 使快速扫描与 DFS 遍历产出相同的"pickcode → (文件名, 大小, sha1, 网盘完整路径)"集合
2. WHEN 快速扫描属性字典流入同步 diff, 同步 SHALL 复用现有的新增/变更/删除判定、STRM 写入与数据库 upsert 逻辑（本特性对 diff 与写库零改动）
3. WHERE 非媒体扩展名文件与目录节点, 快速扫描过滤行为 SHALL 与 DFS 遍历一致（目录只参与路径拼接，文件按现有扩展名规则处理）
