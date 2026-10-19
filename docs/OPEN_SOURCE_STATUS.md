# 首次开源状态

日期：2026-10-19。仓库以开发预览发布，许可证为 MIT。

## 已有实现

- TypeScript/Pi 多 Agent 编排、意图路由、只读业务工具、知识检索、SSE 和 MCP。
- Python/FastAPI 基础设施、合成订单/账单数据及离线评测。
- SQLite 记忆表、基本 begin/finish 协议、少量关键词偏好的显式写入和管理接口。

## 记忆模块的待办与已知问题

当前并未完成 [记忆开发方案](AGENT_MEMORY_DEVELOPMENT.md) 的 P0～P3 验收：

- Worker 尚未接入应用生命周期；后台抽取和自动恢复不会随服务自动运行。
- 模型 Judge、SQLite 滚动摘要、事实版本/游标/删除代际的完整提交协议尚未接通。
- 关键词规则不能完整处理偏好纠正、否定、引述、敏感信息和同批冲突。
- 会话删除尚未完整清理原文及证据，物理删除尚未保留必要屏障。
- 记忆关闭期间的消息 eligibility、匿名 context 路径和失败回执仍需修正。
- TS 定时续租、异常/取消终态、客户端幂等键和可靠重试尚待完善。
- 历史角色装配、结构化转义、完整轮次裁剪和每次模型调用预算尚未完成。
- Chroma 情景索引 outbox、召回复核、删除清理、Redis 版本化缓存和旧数据迁移尚未完成。
- 配置示例并不表示所有参数已接入运行逻辑；记忆专用故障/并发测试和观测指标待补充。

请使用合成演示数据。演示身份及 SQL scope 过滤不能代替认证和授权。

## 本次执行的检查

- Node.js 26.0.0：TypeScript typecheck、56 项 Vitest 测试、build 通过；CI 配置使用 Node.js 24。
- Python 3.12.13：独立临时环境安装 `requirements-test.txt`，58 项 pytest 通过，1 项 Starlette/AnyIO 弃用警告。
- Docker Compose 配置检查、Git diff 空白检查通过。
- 对候选提交文件和证据压缩包执行常见密钥模式、本地配置密钥匹配检查，未发现匹配项；运行文件按 `.gitignore` 排除。这不替代完整安全审计。

本次未重新执行真实模型评测、Docker 端到端验证或 Redis/Chroma 故障注入。
历史 `MINIMAL_BUSINESS_*` 报告属于旧业务验收，不作为记忆升级完成的证据。
