# Agent 记忆模块开发验收记录

日期：2026-10-19（Asia/Shanghai）。依据 [开发方案](../../design/AGENT_MEMORY_DEVELOPMENT.md) 按模块实现与提交。本文描述已执行的验证及边界，不代替完整生产部署验收。

## 模块交付

| 模块 | 实现与证据 |
| --- | --- |
| 权威存储与会话 | schema 迁移、跨实例 SQLite 租约、数据库时间、持久幂等键及缺省会话映射、finish 重放、FAILED/CANCELLED/INTERRUPTED、过期输入恢复；`test_memory_repository.py`、`test_memory_recovery.py`、`memory-turn.test.ts`。 |
| 上下文与预算 | 一次 SQLite 读快照、当前请求排除、完整轮次裁剪、JSON 转义、背景与原始角色分开、Router 摘要线索、主辅及降级共享同一快照；每次 Pi 推理含工具循环/修复/Composer 检查窗口；`test_memory_context.py`、`memory-context.test.ts`、`model-budget.test.ts`、`tool-budget-integration.test.ts`。 |
| 长期事实 | user 原文固定批次、严格 Judge schema/证据、来源引用、同键最终差异、原子容量预演、revision/cursor/epoch/token 联合校验、重试退避及隔离、来源特定回执；`test_memory_facts.py`、`test_memory_facts_service.py`。 |
| 显式操作与隐私 | 确定性回执、清空/事实 ID 撤回独立事务、匿名隔离、开关 eligibility、全 scope 背景遗忘边界、原文/证据删除墓碑、敏感素材在 Judge 前排除；`memory-coordinator.test.ts`、`test_memory_api.py`、`test_memory_end_to_end.py`。 |
| 滚动摘要 | 完整终态轮次、至少保留目标最近 3 轮、旧摘要重写、结构/有界输出、代际与上下文冲突防护、摘要失败不删原文、不推进覆盖边界、敏感摘要拒收；`test_memory_compaction.py`。 |
| 情景索引 | 独立集合及哈希 ID、持久 outbox、幂等 upsert、索引候选经 SQLite 复核、范围去重、epoch 删除及迟到写入补偿、未确认远端写入保持清理 PENDING；`test_memory_episodes.py`。 |
| 迁移与维护 | 显式离线 JSON、默认 dry-run、SQLite online backup、IMPORTING 锁、稳定来源排序、无伪造轮次、旧画像 CANDIDATE、重复导入不增行、删除屏障阻止复活、隔离任务有审计重排；`test_memory_migration.py`、[操作指南](../../guides/MEMORY_MIGRATION.md)。 |
| 观测与配置 | 模型调用/usage、摘要前后体积和耗时、截断、容量、待处理与隔离任务、删除进度；无正文指标、`/memory/status`、`/memory/cleanup`、范围及超时/租约交叉校验；`test_memory_metrics.py`。 |

## 自动化结果

- Python 3.12.13：256 项 pytest 通过；保留 1 项 Starlette/AnyIO 既有弃用警告。
- Node.js 26.0.0：164 项 Vitest 通过；typecheck、build 通过。GitHub CI 使用 Node.js 24 / Python 3.12。
- Compose 配置及 Git diff 空白检查通过。
- 单元/协议测试使用临时真实 SQLite 与 fake Judge/索引，不访问日常用户库。
- 未重新运行原订单业务的全部历史线上验收；本文的测试数量属于本轮实际执行，历史报告不作为记忆完成证据。

## 真实模型与索引验证

实际模型为本地配置的 `deepseek-v4-flash`，复用现有 Anthropic-compatible 配置，无新模型 ID。

1. 电话偏好 ADD 成功；后续“不要电话，改邮件”产生替换，活动事实始终只有一条。
2. 13 轮合成登录排查对话成功压缩前 10 轮，generation=1，近期区保留 6 条消息；摘要保留“暂缓退款”及“assistant 仅建议，未执行退款或创建工单”。
3. 清空后活动事实为 0，epoch 增加，旧背景不再进入新快照。
4. 真实本地嵌入式 Chroma（默认 embedding）完成一条情景 upsert → 召回 → SQLite 复核；清空前索引数量 1、有效候选 1，清空并同步后索引数量 0、cleanup=COMPLETED。没有使用阈值或宣称召回质量达到基准。
5. 另启动隔离端口的真实 TS 与 FastAPI 服务，使用临时业务/记忆库完成 JSON 记住、缺省 conv_id 幂等重放、SSE 邮件纠正、Pi 账单工具查询及清空。最终只有 4 个 COMPLETED 轮次，重放没有多写；`list_bills` 返回演示数据且 output_valid=true。测试服务已停止。

供应商首次返回 thinking 块时，原先 text-only 解析拒绝提交，任务保留重试状态；随后增加最终 text-only JSON 提取兼容并重跑成功。没有把失败误报为已记住，也没有推进失败批次的消费位置。

合成结果见 [机器可读证据](AGENT_MEMORY_ACCEPTANCE.json)。真实模型存储 smoke 可显式重跑（会调用当前配置的付费 API，数据库始终临时隔离）：

```bash
PYTHONPATH=python-service python -m evaluation.memory_smoke \
  --real-model --report /tmp/lumenmind-memory-smoke.json
```

## 实际默认预算与限制

上下文 24,000 Unicode 码点；摘要包装 4,000、事实块 3,000、情景块 3,000；事实最多 50 条、单条最多 300 字符且最终渲染必须整体装入事实块。事实批次 3 条或最久 60 秒，单批最多 20 条/12,000 字符，超预算的合法单条保持完整并另做模型窗口检查。flush 最多 3 批且前台最多等待 5 秒；超时保留持久任务。

模型超时 30 秒，任务租约 180 秒；会话租约 90 秒，TS 根据服务端实际到期时间动态提前续租。摘要触发 18,000 字符或超过 12 个完整轮次，后台保留最近 3 轮。Pi 工具结果默认上限 8,000 字符；窗口检查采用保守估计，未将字符压缩率当作费用节省率。

剩余边界：

- Redis 版本化渲染缓存尚未启用；直接读取 SQLite，不回退旧缓存。这不影响已验证的权威存储和恢复，但不能宣称缓存模块完成。
- 本次验证的是本地真实 Chroma 与 fake 故障协议，尚无远程 Chroma HTTP 断网/重连及完整 Docker 滚动升级实测。
- 自然语言描述的模糊事实撤回需要澄清；直接事实 ID 和明确清空可执行。
- 在途远端写入没有回调时，清理持续 PENDING，需维护核验。备份仍按独立保留策略管理，不能承诺备份与全部远端副本即时物理擦除。
- 混合业务回答的记忆确认句使用规则过滤与真实回执，仍需持续模型语义回归；没有完整自然语言真实性证明。
- 没有新增认证、多主机数据库、跨运行即时撤销广播、完整 Pi 工具恢复或主推理完整费用报表。演示 scope 隔离不是鉴权。
