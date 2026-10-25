# LumenMind-pi 文档索引

`docs/` 按用途分层。新增文档时请放入对应目录，避免在根目录堆积散文件。

## 项目与状态

- [项目 README](../README.md)
- [开源状态与已知问题](project/OPEN_SOURCE_STATUS.md)
- [贡献指南](../CONTRIBUTING.md)
- [安全说明](../SECURITY.md)

## 设计与方案

- [Agent 记忆优化开发方案](design/AGENT_MEMORY_DEVELOPMENT.md)
- [最小订单与账单查询工程计划](design/MINIMAL_BUSINESS_PLAN.md)

## 操作与评测指南

- [Agent 评测闭环](guides/EVALUATION.md)
- [旧记忆离线迁移与隔离任务维护](guides/MEMORY_MIGRATION.md)

## 验收记录与证据

- Agent 记忆
  - [开发验收记录](acceptance/agent-memory/AGENT_MEMORY_ACCEPTANCE.md)
  - [机器可读验收证据](acceptance/agent-memory/AGENT_MEMORY_ACCEPTANCE.json)
- 最小订单与账单查询（历史业务验收）
  - [实施方验收记录](acceptance/minimal-business/MINIMAL_BUSINESS_ACCEPTANCE.md)
  - [实施方响应与 trace 快照](acceptance/minimal-business/MINIMAL_BUSINESS_ACCEPTANCE.json)
  - [独立验收报告](acceptance/minimal-business/MINIMAL_BUSINESS_INDEPENDENT_REVIEW.md)
  - [独立验收机器可读证据](acceptance/minimal-business/MINIMAL_BUSINESS_INDEPENDENT_REVIEW.json)
  - [可复核证据包](acceptance/minimal-business/MINIMAL_BUSINESS_REVIEW_EVIDENCE.zip)

## 笔记与演示

- [《Agent 评测：方法论与体系设计》五分钟速览](notes/agent-evaluation-methodology-summary.md)
- [Pi 测评 HTML 逐页讲稿](notes/pi-evaluation-walkthrough.md)
- [Pi 数据流演示](assets/pi-data-flow.html)
- [Pi Agent 测评与质量闭环演示](assets/pi-evaluation.html)

## 外部参考资料

- [《Agent 评测：方法论与体系设计》原文存档](references/Agent%20评测：方法论与体系设计.md)（简悦导出，保留来源与远程图片链接）

## 目录约定

| 目录 | 用途 |
| --- | --- |
| `design/` | 设计方案、架构说明、实施计划 |
| `guides/` | 操作手册、评测与迁移指南 |
| `acceptance/` | 验收报告、机器可读证据和证据包 |
| `notes/` | 讲稿、阅读笔记、会议记录等非正式说明 |
| `references/` | 外部文章、参考资料的原文存档 |
| `assets/` | HTML 演示、图片等文档资源 |
| `project/` | 项目状态、开源与治理信息 |
