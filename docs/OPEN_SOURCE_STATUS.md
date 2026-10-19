# 开源开发状态

更新：2026-10-19。许可证 MIT，当前仍为开发预览。

已接通 SQLite 会话协议、动态续租与恢复 Worker、统一上下文预算及历史角色、模型事实 Judge 与纠正、遗忘屏障、滚动摘要、Chroma 索引 outbox、备份优先的离线旧数据导入和无正文运行指标。

本轮验证：256 项 Python 测试、164 项 TS 测试、类型检查、构建、Compose 配置通过；真实 `deepseek-v4-flash`、临时真实 Chroma，以及隔离 TS/Python HTTP 主链路通过。完整范围及证据见 [记忆验收记录](AGENT_MEMORY_ACCEPTANCE.md)。

仍未完成 Redis 版本化渲染缓存、远程 Chroma HTTP 故障实测、完整 Docker 滚动升级及生产安全建设。模糊事实撤回需要澄清；没有回调的在途索引写入会保持清理 PENDING。本文不将完整 P0～P3 验收矩阵标记为通过。

源码与部署说明见 [README](../README.md)；贡献与安全边界见 [CONTRIBUTING](../CONTRIBUTING.md)、[SECURITY](../SECURITY.md)。原 `MINIMAL_BUSINESS_*` 只作为注明日期的历史业务验收材料。

首次开源时的基础检查为 58 项 Python、56 项 TS；后续记忆测试数量以本次验收记录为准。
