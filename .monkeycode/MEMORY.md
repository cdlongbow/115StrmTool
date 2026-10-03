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
- Date: 2026-08-27
- Context: 用户明确指示
- Instructions:
  - 不要擅自改动代码，除非用户明确指示
  - Git 提交和推送：完成开发任务后，直接提交并推送到 main 分支，无需创建功能分支或 PR
  - 提交推送前，自动将本次改动写入 CHANGELOG.md 顶部未发版条目（当天条目存在则追加，不存在则新建日期条目），内容面向用户可感知的变化

### 提交前本地质量门禁
- Date: 2026-10-02
- Context: 讨论 CI 把关方案后用户采纳建议（"听你的"）
- Instructions:
  - 每次代码改动提交前，必须先本地跑通：`ruff check .`（仓库根目录）和 `cd combined && python -m pytest -q`，两者全绿才允许提交推送
  - 日常 CI（.github/workflows/ci.yml，push/PR 触发）已承担同样门禁，但不以此替代本地验证

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
