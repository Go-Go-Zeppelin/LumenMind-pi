# 迁移说明

## 代码映射

| 原实现 | 新实现 | 状态 |
|---|---|---|
| `agents/agent_orchestrator.py` | `agent-service/src/agents/orchestrator.ts` | 已切换到 Pi，支持主/辅助 Agent、领域评分、可解释路由、并行协作和失败降级 |
| Python `GeneralAgent` | `PiAgentExecutor("general")` + General Skill | 已迁移 |
| Python `TechnicalAgent` | `PiAgentExecutor("technical")` + Technical Skill | 已迁移 |
| Python `BillingAgent` | `PiAgentExecutor("billing")` + Billing Skill | 已迁移 |
| `core/intent_recognizer.py` | `intent-router.ts` | 细粒度规则快速路径 + Pi LLM 歧义路由 + 上下文缓存 + 规则实体抽取 |
| `core/skill_loader.py` | `skills/loader.ts` | 已迁移，仍支持 `SKILL.md` front matter 和热加载 |
| `mcp/knowledge_base.py` | Python 原实现 + HTTP `/knowledge/search` | 保留 |
| `mcp/tool_manager.py` | Python 可选 query rewrite/rerank；Pi `AgentTool` | 保留并适配 |
| `memory/conversation_memory.py` | Python 内部接口 | 保留；Redis 使用 asyncio 客户端，ChromaDB 同步操作转线程池 |
| `evaluation/evaluator.py` | `evaluation/pi_evaluator.py` | Python 评测保留，被测对象改为 Pi HTTP 服务 |
| FastAPI `/chat` | 代理到 Pi `/chat` | 保留兼容，不再执行旧 Agent |

## 关键行为

1. 每个业务 Agent 都由 Pi `Agent` 实例执行；Pi 自己维护单轮中的 assistant/toolResult 循环。
2. Redis/ChromaDB 仍是跨请求的事实来源。为避免同一会话乱序，Pi 服务按 `user_id + conv_id` 串行执行。
3. 技术和账单关键词同时命中时，两个 Pi Agent 并行运行，结果带领域标签合并。
4. 专属 Agent 失败会降级到 General；若模型服务整体不可用，返回明确的人工升级提示。
5. Skills 在每次执行前按 Agent 类型和关键词选择，`POST /skills/reload` 无需重启。
6. MCP 与 Pi 原生工具共用同一个 Python HTTP 检索契约，避免两套知识库逻辑漂移。
7. 细粒度意图保留 `intent_group` 兼容大类统计；低置信度 `other` 先澄清，不直接误路由。
8. 路由响应公开主 Agent、辅助 Agent、路由理由、结构化实体和各来源置信度。
9. Pi 仍采用原生工具循环；`knowledge_search` 作为业务 Agent 的共享只读工具由 Agent 自主决定是否调用，不会退回预先拼接 RAG 上下文。

10. 专业 Agent 的 `monitor_penalty` 达到阈值时从本次路由候选中移除；同类实例全部降权时回退到 General，并在 `routing_reason` 中记录 `monitor_fallback`。

11. 知识库写入默认使用稳定 ID + `upsert`，相同文档重复导入不会因 ChromaDB ID 冲突失败。

## 2026-09-28 运行时同步

- Agent 工具状态改为单次执行局部数据；失败或降级时保留已经发生的工具调用轨迹。
- RAG 不再由 API 预先拼接上下文，业务 Agent 共享 `knowledge_search` 并在工具循环中自主决定是否调用。
- 监控降权达到阈值时从专业路由候选中移除，单实例也会回退到 General。
- 知识库导入改为稳定 ID + `upsert`，重复导入和同 ID 内容更新均可安全处理。

## 2026-08-10 运行时同步

本次同步包含以下运行时更新：

- 新增 9 个细粒度意图及大类映射。
- 新增订单号、日期、金额、错误码的确定性实体提取，避免额外 LLM 调用。
- 路由从无主次关键词拼接升级为结构化领域评分和主/辅助 Agent。
- 新增低置信度澄清、路由理由和意图来源分数。
- Python 侧 Redis 全异步化，ChromaDB 调用通过 `asyncio.to_thread` 避免阻塞事件循环。
- 知识库导入、查询、统计和服务关闭改为异步安全接口。

## 建议发布顺序

1. 旁路部署：保持旧服务不变，启动本目录的 3000/8000 服务。
2. 回放评测：用脱敏历史问题调用 Python `/eval/run`，记录路由准确率、质量和延迟。
3. 小流量：网关把少量 `/chat` 流量切到新的 FastAPI 兼容入口。
4. 扩大流量：观察 `/monitor`、`/metrics`、工具失败率和人工升级率。
5. 决策：只有在管理、记忆、评测均有稳定替代后，才移除 Python FastAPI。

## 回滚

该重构位于独立目录，不改动既有服务。回滚只需把网关或调用方指回旧服务；Redis 与 ChromaDB 数据结构仍兼容原 Python 实现。
