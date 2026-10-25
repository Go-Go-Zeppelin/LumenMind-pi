# LumenMind-pi Agent 评测闭环

方法论阅读：[五分钟速览](../notes/agent-evaluation-methodology-summary.md) · [文章原文存档](../references/Agent%20评测：方法论与体系设计.md)。

这次改造依据《Agent 评测：方法论与体系设计》，把现有逐轮 LLM 打分扩展为：

```text
业务验收标准 → 核心候选用例 → 隔离运行与 Trace 快照
→ 规则优先评分 → 会话目标与重复运行统计
→ 错例问题簇 / 可执行行动项 → 同配置基线比较 → 离线发布检查
```

## 业务边界

LumenMind-pi 是客服咨询、排障和交接运行时。现有工具可以检索知识、核验账单字段、比较金额文本和生成排障线索，不能访问真实订单或执行退款、改地址、开票。评测不把“已退款”“已更新收货地址”等文字当作业务成功，默认将这类无依据的完成声明记为硬失败。

多轮目标应写成“保留订单号并解释核验和支付渠道的处理路径”，不能写成“真实退款完成”。未来接入写入工具后，需要增加外部状态断言并重新设计完成声明规则。

## 已落地的检查

| 文章的方法 | 本项目实现 |
|---|---|
| Turn / Session / Trace / Outcome | 保留逐轮 results，新增 sessions；立即采集对应请求 Trace；使用最终答复规则或整段会话 Judge 检查目标 |
| 规则优先，模型辅助 | 先检查业务禁用行为、意图、输出契约、工具与 Skill；硬失败跳过 Judge；`judge_mode=rules` 可只跑确定性检查 |
| 稳定性 | 相同场景重复运行，统计至少一次成功率、全部运行成功率、会话通过率与按场景计算的 Wilson 区间 |
| 用例隔离 | 每次运行、每个场景、每次重复生成独立 user_id / conv_id；同一会话内保留相同身份 |
| 问题现象与根因分离 | findings 记录现象、候选模块、证据、建议动作、owner_role 和验收方式；根因状态为 `needs_evidence_review` |
| 修复与回归资产 | 显式开启 persist_report 后保存报告和错例问题簇；不会自动修改基线或把失败样本升级为金标 |
| 发布检查 | 空数据、意图准确率不足、业务硬违规、P0 场景失败、全部运行成功率不足及可比基线回退会阻止发布 |

Trace 补充了工具真实返回、开始/结束时间、已注入 Skill 名称、请求级降级标记、运行时与模型标识。工具顺序检查按开始时间排序，并检查前置阶段结束后后续阶段才开始，避免把并行工具的完成顺序当作调用顺序。请求 ID 使用完整 UUID。

Pi 进程内 Trace 仍按 PI_TOOL_TRACE_MAX 保留，重启后消失。离线评测报告会立即保存对应 Trace 快照；这不是线上全量持久化审计系统。

## 核心候选用例集

`data/eval/golden_set_v1.json` 包含 20 条意图样本和 30 个对话场景，其中 4 个为多轮。覆盖退款核验、已发货退款、重复扣款、发票、工具参数、工具顺序、RAG、敏感凭证、人工交接、会话目标切换和订单切换。

该集基于现有业务 Skill 和工具边界编写，**尚未经业务专家复核，是 seed candidate，不能宣称为人工金标或生产效果基准**。用例要求明确调用工具时才设置 required_tools；一般咨询不强制某条工具路径。字面 required_points 适合硬条件，语义同义表达交给参考答案和 Judge。

意图样本使用默认 `/intent` 规则入口，不评测完整 Embedding + LLM 路由；对话样本走真实 `/chat` 链路。

## 本地验证与运行

从 `LumenMind-pi/python-service` 运行：

```bash
# 新环境准备轻量评测测试依赖
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt

# 只验证数据契约，不调用服务、模型或记忆
.venv/bin/python -m evaluation.run_suite --dry-run

# 单个场景的确定性检查：被测 Agent 仍可能调用模型
.venv/bin/python -m evaluation.run_suite \
  --case-id billing-refund-fields --rules-only --repeats 3

# 在独立评测基础设施上运行全量，包括 LLM Judge
.venv/bin/python -m evaluation.run_suite --repeats 3 \
  --url http://localhost:8000 --output ../data/eval/latest-quality-report.json

# 无外部依赖服务的自动测试
.venv/bin/python -m pytest tests -q
```

本次建立的 `.venv` 用于测试评测模块与 API。完整服务按 requirements.txt 安装依赖或使用 Docker Compose。`--rules-only` 仅关闭裁判模型，不关闭被测 Agent 的模型调用。身份隔离不隔离服务的路由统计、知识缓存和基础设施容量，因此真实评测应使用独立环境，不能用独立 user_id 代替环境隔离。

CLI 会设置 `record_metrics=false` 与 `persist_report=true`。CLI 只在离线门禁状态为 passed 时返回 0；review 和 blocked 均返回 1，不执行部署。

## HTTP 输入示例

```json
{
  "dataset_version": "refund-flow-v1",
  "repeats": 3,
  "judge_mode": "llm",
  "record_metrics": false,
  "persist_report": true,
  "dialog_cases": [{
    "case_id": "refund-fields",
    "category": "billing",
    "priority": "P0",
    "question": "退款申请，请调用 check_billing_fields 核验订单号A-1234",
    "expected_intent": "refund",
    "required_points": ["核验"],
    "process": {
      "expected_agent": "billing",
      "required_tools": ["check_billing_fields"],
      "tool_inputs": {"check_billing_fields": {"order_id": "A-1234"}},
      "required_skills": ["账单退款处理规范"],
      "allow_fallback": false
    }
  }]
}
```

提交到 `POST /eval/run`。多轮使用 turns；逐轮 expected_intents、reference_answers、turn_process 必须与 turns 等长，未标注意图可用 null。单个 process 默认作用于所有轮次，逐轮不同要求使用 turn_process。

多轮必须给出明确的 expected_outcome 或 outcome_required_points / outcome_forbidden_patterns，缺失时不能自动判为会话成功。只提供 expected_outcome 且采用 rules 模式时，语义目标未被检查，会话进入复核。

拼写错误字段、非法正则、空会话、逐轮标注长度错误会返回 422，避免静默忽略验收条件。工具必须实际成功调用，tools_used 的名称本身不能代替 Trace 证据。知识库降级结果不能满足 require_knowledge=true。

## 报告怎么读

优先看 release_gate、sessions、stability、findings，然后再看 avg_scores 和逐轮 results。

- total / passed / pass_rate 沿用“结果条数”的兼容口径：意图汇总一条，每轮对话一条；sample_counts 给出实际意图样本数、对话场景数、轮数与会话数。总体结果比例不等同于业务任务成功率。
- sessions.passed 要求所有轮次通过、执行完整且会话目标通过；不能用各轮平均分掩盖最终失败。
- 至少一次成功率看能力可能性，全部运行成功率看重复运行可靠性；两者均基于当前固定候选集。置信区间按场景计算，不把重复轮次当作独立线上用户样本。
- Judge 不可用、返回无效分数或缺少判定依据，记为缺少评分证据，不混入答案事实性平均分。
- 四象限只统计同时有意图标注与已判定答案质量的样本；跳过 Judge 的硬失败记为 answer_not_judged。
- findings 是诊断入口和结构化行动项，不是自动确证的根因或具体人员归责。

报告存到 `data/eval/runs/<run_id>/report.json`，问题簇存到同目录 `badcases.json`。修复后用 `--case-id` 重跑证据对应场景，并跑同类回归。新错例进入正式回归集前，先确认期望行为、代表性、可复现证据和业务口径。

## 基线与门禁的边界

只有数据集版本与指纹、重复次数、评分器版本、裁判模式和裁判模型一致，才比较基线。旧 baseline.json 缺少这些信息会明确标为 incompatible，保留原文件。人工认可报告后，可将对应 report.json 保存为 baseline.json；正常评测不会覆盖它。

当前回退检查：答案指标相对下降超过 5%，或全部运行成功率绝对下降超过 5 个百分点。这是工程观察阈值，**没有执行显著性检验**，报告明确返回 not_tested。

P0 样本、裁判异常、缺少证据或未验收会话目标要求人工复核；纯规则对话评测、少于 3 次重复和缺少可比基线会进入 review。因此初次运行或含 P0 用例的报告即使样本都通过，也不会自动给出生产发布许可。

本轮实现的是可运行的离线质量基础。线上灰度指标、自动回滚、人工复核工单、Judge 校准金标和跨版本统计检验仍需后续接入。离线通过后，应按文章要求验证线上任务完成率、重复追问率、转人工率及投诉变化。
