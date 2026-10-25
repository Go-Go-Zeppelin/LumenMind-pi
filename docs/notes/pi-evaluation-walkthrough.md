# Pi 测评 HTML 逐页讲稿

配套文件：`docs/assets/pi-evaluation.html`。

这份讲稿按 HTML 的 4 个章节（键盘 `1–4`）来写。每一页都先告诉你：

1. 屏幕上有哪几块；
2. 应该怎么点；
3. 每个英文名到底在说什么；
4. 哪些话可以直接照着讲。

演示时不要逐行念 JSON。记住一个固定句式：

> **“这个字段回答的是……问题；证据来自……；如果它是这个值，下一步就……”**

---

## 0. 先分清三种英文名

页面上的英文并不都是“评测结果字段”。看到英文名，先判断它属于哪一类：

| 类型 | 长相 | 例子 | 它是什么 | 讲的时候怎么说 |
|---|---|---|---|---|
| JSON 字段 | `snake_case` | `expected_intent`、`process_failures` | 用例、返回、Trace 或判定结果里的数据键 | 翻译成人话，不要念下划线 |
| 页面控件 / 代码变量 | `camelCase` | `includeP0`、`anyRate`、`repeatSelect` | 网页元素的 ID 或前端计算变量 | 说“这个开关控制……”，不要说成报告字段 |
| 业务缩写 | 大写或符号 | `P0`、`Top-K`、`@K`、`pp` | 业务优先级、检索前 K 条、百分点 | 先解释缩写，再讲结论 |

还有一个更重要的区分：**JSON 字段是“证据类型”，不是“得分本身”**。

### 0.1 一条主线：先标准，后返回，再 Trace，最后判定

| 证据层 | 它回答的问题 | 典型字段 |
|---|---|---|
| 验收标准 | Agent **应该**做什么？ | `expected_intent`、`expected_agent`、`required_tools`、`required_skills`、`tool_inputs` |
| Agent 返回 | Agent **说自己**做了什么？ | `response.response`、`tools_used`、`intent`、`knowledge_used` |
| 执行 Trace | Agent **实际**做了什么？ | `trace`、`tool_calls`、`tool_name`、`input`、`success`、`is_error`、`skills_used` |
| 判定结果 | 这次 **怎样判**？为什么？ | `process_failures`、`rule_violations`、`failures`、`judge_status`、`scores`、`answer_good`、`passed` |

最重要的一句话：

> `tools_used` 只是 Agent 返回里“声称/汇总”的工具名；`trace.tool_calls` 才是真实调用记录。

### 0.2 整条离线质量闭环

```text
定义候选用例与验收标准
        ↓
隔离运行真实 Pi 服务（/intent 或 /chat）
        ↓
每轮立即按 request_id 取 Trace 快照
        ↓
确定性规则：意图、工具、参数、Skill、业务边界
        ↓ 硬条件通过
四维 LLM Judge：相关性、准确性、完整性、有用性
        ↓
多轮会话目标：逐轮通过 ∧ 执行完整 ∧ 最终目标达成
        ↓
重复运行稳定性：至少一次 vs 每次都对
        ↓
失败聚簇：现象 → 候选模块 → 行动项 → 回归
        ↓
同配置基线比较 → 离线发布检查 blocked / review / passed
```

### 0.3 五页导航

| 按键 | 页面 | 这一页回答什么问题 | 主要操作 |
|---|---|---|---|
| `1` | 闭环流程 | 一次评测由哪些环节组成？ | 点 9 个节点，看右侧 `DATA SNAPSHOT` |
| `2` | 判分现场 | 为什么回答漂亮也可能 FAIL？ | 切换 5 个返回场景 |
| `3` | 稳定性与门禁 | 一次成功和次次成功有什么区别？ | 点矩阵格子、预设、重复次数、门禁开关 |
| `4` | 存档实验结果 | 指标应该怎样连同口径一起讲？ | 切 `Top-1/3/5`，打开错例弹窗 |

### 0.4 开场 30 秒，可直接说

> 这个页面展示的不是“给一句回复打个分”，而是一次 Agent 任务的离线验收闭环。我先看用例定义的业务标准，再看 Agent 返回了什么、Trace 里实际做了什么，然后先跑确定性规则，硬条件通过后再让 Judge 评答案质量。最后再看多轮目标、重复稳定性和离线发布门禁。页面里的 JSON 只是证据节选，我演示时重点讲每个字段回答什么问题。

---

## 1. 第 1 页：闭环流程

### 1.1 屏幕导览

这一页有三块：

- **左侧流程图**：9 个节点，从“定义业务验收”走到“同配置基线与离线检查”；
- **右侧 `DATA SNAPSHOT / 评测数据`**：点击节点后显示该环节的数据样张；
- **底部 cue**：一句话总结当前节点。

操作：

- 点击左侧任意节点；
- 或点“播放链路”让 9 个节点自动播放；
- 右侧 JSON 会随节点切换。

### 1.2 先按顺序记住 9 个节点

| 序号 | 节点 | 一句话作用 |
|---|---|---|
| 01 | 定义业务验收 | 先把“做对”写成可检查的标准，不是先跑分 |
| 02 | 隔离执行真实链路 | Python 负责编排，真正被测的是真实 Pi 服务 |
| 03 | 立即采集执行证据 | 用 `request_id` 把回答和真实工具调用对齐 |
| 04 | 规则与过程验收 | 确定性错误先拦，硬失败就不让 Judge 打分 |
| 05 | 四维 LLM Judge | 硬条件通过后，才评语言和答案质量 |
| 06 | 验收多轮会话目标 | 每轮都通过，不等于整段任务完成 |
| 07 | 统计重复运行稳定性 | 同时看“能做一次”和“次次都能做” |
| 08 | 证据聚簇与行动项 | 失败要变成可追踪的修复任务，不只留一个红叉 |
| 09 | 同配置基线与离线检查 | 配置一致才比较；最后给出 blocked / review / passed |

---

### 1.3 节点 01：定义业务验收

右侧 JSON：

```json
{
  "dataset_version": "lumenmind-quality-seed-v1",
  "intent_cases": 20,
  "dialog_cases": 30,
  "dialog_turns": 37,
  "multi_turn_cases": 4,
  "priority_P0": 11
}
```

字段人话：

| 字段 | 人话 | 不要讲错 |
|---|---|---|
| `dataset_version` | 这套候选测试集的版本名 | 不是模型版本，也不是 Agent 得分 |
| `intent_cases` | 意图分类用例数，20 条 | 不是“20 个用户” |
| `dialog_cases` | 对话场景数，30 个 | 一个场景可能包含多轮 |
| `dialog_turns` | 所有对话合计 37 轮 | 轮数不等于场景数 |
| `multi_turn_cases` | 其中 4 个是多轮场景 | 多轮场景才需要额外检查会话目标 |
| `priority_P0` | 11 个 P0 高优先级用例/场景 | P0 失败通常需要阻断或人工复核 |

这一页要补一句：

> 这批是候选回归集，尚未经过业务专家复核，所以不能叫“人工金标”；它用来回归和暴露问题，不是生产效果证明。

可直接说：

> 我先定义“做对”是什么。不仅标预期意图，还要写必要信息、禁用行为、必须调用的工具和参数、需要的 Skill，以及多轮会话最终要达成的目标。这里展示的是测试集规模和版本，不是 Agent 成绩。

---

### 1.4 节点 02：隔离执行真实链路

右侧 JSON：

```json
{
  "runner": "Python PiServiceEvaluator",
  "intent_path": "/intent（默认规则）",
  "dialog_path": "/chat（真实 Pi）",
  "user_id": "eval-user-{run}-{case}-{repeat}",
  "conv_id": "eval-dialog-{run}-{case}-{repeat}",
  "same_session": "同一会话各轮共用身份"
}
```

字段人话：

| 字段 | 人话 |
|---|---|
| `runner` | 谁来编排这次评测；这里是 Python 的 `PiServiceEvaluator`，不是被测模型 |
| `intent_path` | 意图样本请求哪个入口；这里是 `/intent`，主要走默认规则 |
| `dialog_path` | 对话样本请求哪个入口；这里是 `/chat`，走完整 Pi 聊天链路 |
| `user_id` | 每次运行生成的独立用户身份；防止不同场景记忆串场 |
| `conv_id` | 每次运行生成的独立会话身份；防止不同对话混在一起 |
| `same_session` | 同一多轮会话的各轮必须共用同一个身份 |

`{run}-{case}-{repeat}` 是模板，实际会替换成：

- `run`：这一次评测运行；
- `case`：第几个用例；
- `repeat`：第几次重复。

`user_id` 和 `conv_id` 的区别：

- `user_id` 是“谁”；
- `conv_id` 是“哪一段对话”。

可直接说：

> 评测器在 Python，但真正被测的是 Pi 服务链路。每个场景、每次重复都用独立身份；同一多轮会话内保持身份一致，避免记忆串场。身份隔离不等于基础设施隔离，缓存、路由统计和容量仍然需要在独立评测环境里看。

---

### 1.5 节点 03：立即采集执行证据

右侧 JSON：

```json
{
  "request_id": "本轮完整 UUID",
  "verify": ["request_id", "user_id", "conv_id"],
  "snapshot": [
    "tool_calls",
    "skills_used",
    "fallback_used",
    "model_id",
    "runtime_version"
  ]
}
```

字段人话：

| 字段 | 人话 |
|---|---|
| `request_id` | 当前请求的唯一编号，用来把返回和 Trace 对起来 |
| `verify` | 取到 Trace 后要核对的三个身份字段，防止拿错证据 |
| `snapshot` | 评测报告要立即保存下来的 Trace 内容 |

`snapshot` 里的名字：

| 字段 | 人话 |
|---|---|
| `tool_calls` | 实际调用了哪些工具、入参、出参、成功状态和时间 |
| `skills_used` | 这一轮实际注入或使用了哪些 Skill |
| `fallback_used` | 是否走了降级路径 |
| `model_id` | 实际使用的模型标识 |
| `runtime_version` | 被测运行时版本 |

页面标签还有 `GET /trace/tool/{request_id}`：这是按请求号取 Trace 的接口。Trace 默认可能只在 Pi 进程内，所以评测要在每轮结束后马上保存快照。

可直接说：

> 回答里写“我查过了”不能算证据。我会按 `request_id` 取 Trace，并核对 `user_id` 和 `conv_id` 是否属于这次请求，再保存工具调用、Skill、降级、模型和运行时版本。只有 Trace 才能证明工具真的调用过。

---

### 1.6 节点 04：规则与过程验收

右侧 JSON：

```json
{
  "checks": [
    "意图与输出契约",
    "必要信息与禁用声明",
    "工具成功 / 参数 / 顺序",
    "Skill / 降级 / 延迟预算"
  ],
  "hard_failure": "skipped_hard_failure",
  "answer_good": null
}
```

#### `checks` 是检查项目，不是检查结果

| `checks` 项 | 具体查什么 |
|---|---|
| 意图与输出契约 | 意图/角色是否符合预期；返回结构是否合法；必需字段是否存在 |
| 必要信息与禁用声明 | 必含信息是否出现；禁用话术/越权承诺是否出现 |
| 工具成功 / 参数 / 顺序 | 必需工具是否真实成功调用；参数是否匹配；有依赖时调用顺序是否正确 |
| Skill / 降级 / 延迟预算 | 必需 Skill 是否出现；是否走了不允许的降级；耗时是否超预算 |

#### `hard_failure` 是“硬失败后 Judge 怎么处理”

这里的值 `skipped_hard_failure` 白话就是：

> 已经发现确定性的硬错误，所以跳过 LLM Judge。

硬错误包括：漏调必需工具、工具参数错误、Trace 缺失、工具顺序不对、输出契约失败、业务禁用规则被触发等。

注意：

> 真实单轮结果中，更常见的状态字段是 `judge_status`。`skipped_hard_failure` 只是状态值之一，不是分数，也不代表 Judge 给了 0 分。

#### `answer_good` 是三态值，不是普通的真假

| 值 | 人话 |
|---|---|
| `true` | Judge 有效运行，答案质量达到阈值 |
| `false` | Judge 有效运行，但答案质量没达到阈值 |
| `null` | 没有拿到有效答案质量结论：例如硬失败跳过 Judge，或 Judge 异常 |

`answer_good: null` 的意思是“未判定”，不是“零分”。

可直接说：

> 这个节点是规则验收摘要，不是 Trace 本身。`checks` 列出要查的维度；出现硬错误就跳过 Judge；`answer_good: null` 表示没有有效的语义评分证据。规则优先的目的是防止流畅回答掩盖漏调工具、参数错误或越权承诺。

---

### 1.7 节点 05：四维 LLM Judge

右侧 JSON：

```json
{
  "relevance": 1,
  "accuracy": 0.9,
  "completeness": 0.9,
  "helpfulness": 1,
  "overall": 0.95,
  "pass_threshold": 0.7,
  "reason": "符合事实与能力边界，给出可执行下一步"
}
```

字段人话：

| 字段 | 人话 |
|---|---|
| `relevance` | 有没有回应用户当前真正的问题 |
| `accuracy` | 是否符合事实、参考信息和 Agent 的真实能力边界 |
| `completeness` | 是否覆盖必要信息、条件和下一步 |
| `helpfulness` | 是否给出可执行且有边界的建议 |
| `overall` | 四个维度分数的算术平均；示例是 `(1+0.9+0.9+1)/4 = 0.95` |
| `pass_threshold` | 平均分通过线，示例是 `0.70` |
| `reason` | Judge 必须说明的判定依据 |

Judge 的三个底线：

1. 只在规则硬条件通过后运行；
2. 不替代 Trace，也不覆盖业务硬规则；
3. 返回无效 JSON、分数不在 0–1、缺少 `reason`，都算 Judge 失败，**不能按 0 分处理**。

可直接说：

> 规则通过后，才让 Judge 看答案语义质量。四个维度等权平均达到 0.70 才算过。Judge 只能回答“答案写得好不好”，不能证明工具真的调用了，也不能推翻业务边界。

---

### 1.8 节点 06：验收多轮会话目标

右侧 JSON：

```json
{
  "turns_passed": [true, true, true],
  "session_complete": true,
  "expected_outcome": "保留订单号并解释核验与支付渠道的处理路径",
  "outcome_checked": true,
  "session_passed": true
}
```

字段人话：

| 字段 | 人话 |
|---|---|
| `turns_passed` | 每一轮各自有没有通过 |
| `session_complete` | 预期轮数是不是都执行完了 |
| `expected_outcome` | 整段会话最终要达成的自然语言目标 |
| `outcome_checked` | 最终目标有没有被真正检查；它只说明“检查过”，不说明“达成了” |
| `session_passed` | 整段会话的最终结论 |

会话通过的完整条件：

```text
所有轮次都通过
∧ 会话执行完整
∧ 最终目标被检查且达成
∧ 没有会话级 failures
```

注意：页面上的 `turns_passed / session_complete / session_passed` 是便于讲解的结构；真实报告里会话最终看 `sessions[].passed`，逐轮结果在 `turn_ids` 指向的单轮结果里，未完成或未验收会进入 `failures`。

可直接说：

> 多轮不是每轮平均分高就算过。三轮都很礼貌，但最后丢了订单号，或者没说明处理路径，会话仍然失败。当前系统没有真正退款的写入工具，所以目标只能写成“核验并解释路径”，不能写“退款已经完成”。

---

### 1.9 节点 07：统计重复运行稳定性

右侧 JSON：

```json
{
  "repeats": 3,
  "unit": "case / 固定候选集",
  "at_least_once_success_rate": "每场景至少一次通过",
  "all_runs_success_rate": "每场景所有重复均通过",
  "session_success_rate": "全部会话的通过比例",
  "interval": "按场景计算 Wilson 95%"
}
```

字段人话：

| 字段 | 人话 |
|---|---|
| `repeats` | 每个场景重复跑几次；页面默认 3 次 |
| `unit` | 统计单位：这里是固定候选场景，不是线上用户 |
| `at_least_once_success_rate` | 一个场景只要有一次通过，就算这个场景成功 |
| `all_runs_success_rate` | 一个场景所有重复都通过，才算这个场景成功 |
| `session_success_rate` | 所有“场景 × 重复”的会话中，最终通过的比例 |
| `interval` | 对“全部运行成功率”按场景数计算 Wilson 95% 区间 |

这三个率回答的问题不同：

- 至少一次成功：**有没有能力做成？**
- 全部运行成功：**是不是次次都能做成？**
- 会话通过率：**所有执行里成功占比多少？**

页面上的 `interval` 实际计算的是 `all_runs_success_interval_95`。它按固定场景数算，不是按重复次数算，更不代表线上用户分布。

可直接说：

> 一次成功只能说明“能做”，全部运行成功才接近“可靠”。发布门禁主要看全部运行成功率；至少一次成功率适合定位能力上限，不能拿来掩盖偶发失败。Wilson 区间是按固定场景数算的，不能外推成生产保证。

---

### 1.10 节点 08：证据聚簇与行动项

右侧 JSON：

```json
{
  "failure_type": "tool_input_mismatch",
  "candidate_modules": ["entity_extraction", "agent_executor"],
  "root_cause_status": "needs_evidence_review",
  "action": "对照 Trace 修正参数构造",
  "acceptance": "重跑对应场景及同类回归集"
}
```

字段人话：

| 字段 | 人话 |
|---|---|
| `failure_type` | 观察到的失败类型；这里是工具参数不匹配 |
| `candidate_modules` | 可能相关的模块，只是候选，不是已确认根因 |
| `root_cause_status` | 根因调查状态；`needs_evidence_review` 表示还要复核 |
| `action` | 下一步修复动作 |
| `acceptance` | 修复后怎样验证算完成 |

完整 finding 还会有 `evidence` 数组，里面记录用例、`request_id`、失败原因和出现次数。它的用途是让失败可以按现象聚合、追踪到证据、再分配行动项。

页面底部还提到 `persist_report=true`：保存 `report.json` 和 `badcases.json`，但不会自动覆盖基线，也不会自动把错例升级为人工金标。

可直接说：

> 评测系统只负责从证据提出候选模块，不自动归责。比如参数错误，只能先怀疑实体提取和 Agent 执行器，还要看 Trace 和日志确认。修复后要重跑原场景和同类回归，而不是只把一条用例改绿。

---

### 1.11 节点 09：同配置基线与离线检查

右侧 JSON：

```json
{
  "compare": [
    "数据集版本与指纹",
    "重复次数 / 评分器版本",
    "Judge 模式与模型"
  ],
  "regression": "答案指标相对下降 > 5%，或全部运行成功率绝对下降 > 5pp",
  "status": ["blocked", "review", "passed"],
  "significance": "not_tested"
}
```

字段人话：

| 字段 | 人话 |
|---|---|
| `compare` | 只有这些配置一致，当前结果和旧基线才可比较 |
| `regression` | 工程上定义的回退阈值 |
| `status` | 门禁的三种输出：`blocked`、`review`、`passed` |
| `significance` | 是否做统计显著性检验；`not_tested` 表示没有做 |

补充：`compare` 和 `regression` 是页面上的汇总示意；真实报告里要细看 `baseline_comparison` 的 `status`、`regressions`、`reason`，以及 `release_gate` 里的 `blocked_reasons` 和 `review_reasons`。

三种状态：

- `blocked`：存在明确阻断项，例如业务硬违规、P0 失败、全部运行成功率低于 90%、可比基线回退；
- `review`：证据不足或需要人工，例如 P0 复核、Judge 异常、重复少于 3 次、只有规则评测、没有可比基线；
- `passed`：当前离线检查通过，但仍不等于获准生产发布。

`pp` 是 percentage point，百分点。比如从 90% 掉到 84%，是下降 6 个百分点，不是相对下降 6%。

可直接说：

> 先确认配置一致，再比较指标。数据集、重复次数、评分器、Judge 模式或模型不同，数字差异不能叫回退。当前是工程阈值，没做显著性检验；离线 `passed` 之后还要做线上灰度。

---

### 1.12 第 1 页完整串讲

> 这一页是一条闭环。先用用例定义“做对”的标准，再让 Python 评测器隔离运行真实 Pi 服务，并在每轮结束立刻按 `request_id` 采 Trace。随后先跑确定性规则，硬错误直接失败并跳过 Judge；硬条件通过后才用四维 Judge 评答案。多轮会话还要看最终目标是否达成，重复运行要同时看“至少一次”和“每次都对”。失败不是停在某个分数上，而是聚成带证据、候选模块、行动项和验收方式的问题簇。最后先确认基线配置一致，再做离线发布检查；`passed` 也只是进入线上灰度前的起点。

---

## 2. 第 2 页：判分现场

### 2.1 屏幕导览

这一页是“同一个退款用例，五种返回场景”：

```text
01 验收标准          02 返回内容 + 执行证据          03 规则优先判分
应该做什么             说了什么 / 实际做了什么          为什么 PASS/FAIL
```

顶部有 5 个 Tab：

1. 合格答案
2. 漏调工具
3. 参数错误
4. 无依据完成
5. 裁判异常

每次切 Tab，中间回答和 Trace 变化，右侧的规则检查、Judge 状态和最终结论跟着变化。

页面使用说明：输入和 Trace 是演示样例；硬规则与过程结论参考项目 Python 评测器计算；Judge 分数是示例，页面没有调用模型。

---

### 2.2 左栏：验收标准里的字段

左栏显示的是：

```json
{
  "expected_intent": "refund",
  "expected_agent": "billing",
  "required_skills": ["账单退款处理规范"],
  "required_tools": ["check_billing_fields"],
  "tool_inputs": {
    "check_billing_fields": {"order_id": "A-1234"}
  }
}
```

| 字段 | 人话 | 备注 |
|---|---|---|
| `expected_intent` | 预期意图是退款 | 不是 Agent 实际预测值 |
| `expected_agent` | 预期由 `billing` 角色处理 | 实际值看返回里的 `agent_type` |
| `required_skills` | 必须出现“账单退款处理规范”这个 Skill | 以 Trace 的 `skills_used` 为准 |
| `required_tools` | 必须成功调用 `check_billing_fields` | 名称出现不等于成功调用 |
| `tool_inputs` | 指定工具必须匹配的入参 | 只断言写出来的字段；本页断言的是 `order_id=A-1234` |

左栏上方文字里还有：

- `billing-field-tool`：用例 `case_id`；
- `P1`：优先级；
- “退款申请……”：用例问题文本；
- “支付时间未提供”：这条用例的业务条件。

重点区分：

- `expected_*`：期望是什么；
- `required_*`：必须出现或必须发生；
- `tool_inputs`：只检查写明的参数字段，不是自动逐项比对工具收到的整个对象。

---

### 2.3 中栏：返回内容和执行证据

页面中间分三层：

1. Agent 的文字回答：`response.response`
2. 返回声称用了哪些工具：`response.tools_used`
3. 实际 Trace 节选：`trace.tool_calls[0]` 和 `skills_used`

#### `response.tools_used` 是什么

它是 Agent 返回对象中列出的工具名，可能是 Agent 自己汇总的，也可能是流程回填的。

它回答：**Agent 说自己用了什么工具？**

它不能回答：**工具真的成功调用了吗？参数对吗？**

#### Trace 里的字段

| 字段 | 人话 |
|---|---|
| `trace.tool_calls` | 真实工具调用列表；空列表就是没有工具执行证据 |
| `tool_name` | 实际调用的工具名 |
| `input` | 工具实际收到的参数 |
| `output` | 工具实际返回的结果；页面表格有时只节选前几项 |
| `success` | 工具执行是否成功；是执行层标志 |
| `is_error` | 是否被标记为错误；只有 `success=true` 且 `is_error=false` 通常才算成功调用 |
| `skills_used` | Trace 中实际出现的 Skill |
| `started_at` / `finished_at` | 需要检查工具依赖顺序时使用的时间证据 |

关键提醒：

> Trace 是节选，页面可能只展示第一个工具调用的 `tool_name/input/success/is_error`，完整报告里还会保留 `output`、时间、`request_id` 等信息。

可直接说：

> `tools_used` 是“声称”，`trace.tool_calls` 是“证据”。`success=true` 只说明工具执行没有报错，不说明订单号传对了，也不说明业务任务完成。

---

### 2.4 右栏：规则优先判分

右栏不是原始 JSON，而是把判定拆成三块：

#### 第一块：三条检查行

| 页面检查行 | 背后看什么 |
|---|---|
| 意图 / 角色 / Skill | `expected_intent`、`expected_agent`、`required_skills` 相关失败 |
| 工具必须成功、参数必须匹配 | `required_tools`、`tool_inputs` 相关失败 |
| 能力边界与业务规则 | `rule_violations` 是否为空 |

#### 第二块：Judge 状态和四维分数

| 字段 | 人话 |
|---|---|
| `judge_status` | Judge 这次处于什么状态 |
| `scores` | 四维分数对象；Judge 未运行或失败时为空 |
| `overall` | 四维均分；页面示例显示 0.95 |
| `answer_good` | 答案质量结论：`true / false / null` |

`judge_status` 的可能值：

| 值 | 人话 |
|---|---|
| `completed` | Judge 正常返回了有效 JSON，有分数 |
| `skipped_hard_failure` | 先有硬失败，Judge 没运行 |
| `failed` | Judge 运行异常、返回无效 JSON、分数非法或缺少 `reason` |
| `rules_only` | 只跑规则、不评语义质量；页面五个 Tab 中没有这个场景，但真实报告可能出现 |

再次强调：

> `judge_status=skipped_hard_failure` 或 `failed` 时，`answer_good=null`，`scores` 为空。这不是 0 分，而是“缺少有效评分证据”。

#### 第三块：失败原因和最终结论

| 字段 | 人话 |
|---|---|
| `process_failures` | 过程检查失败，例如漏调工具、参数不匹配、Skill 缺失、Trace 缺失、顺序错误、延迟超预算 |
| `rule_violations` | 业务/输出规则违规，例如无依据完成声明、未经核验的资金承诺、敏感信息风险 |
| `failures` | 上面这些失败 + 意图错误 + Judge 失败等的汇总列表，页面下方红字就是它 |
| `passed` | 当前单轮最终是否通过 |

一条单轮结果通常不是“分数高就 `passed`”，而是：

```text
没有 process_failures
∧ 没有 rule_violations
∧ 没有其他 failures
∧ answer_good === true
```

因此：

- 规则失败，Judge 分数再高也不能过；
- Judge 没运行，`answer_good=null`，不能过；
- Judge 有效但不达标，`answer_good=false`，不能过。

---

### 2.5 五个 Tab 分别说明什么

| Tab | 中间证据 | `process_failures` / `rule_violations` | `judge_status` | `answer_good` | `passed` |
|---|---|---|---|---|---|
| 合格答案 | 工具真实调用，`order_id=A-1234`，成功；Skill 命中 | 都为空 | `completed` | `true` | `true` |
| 漏调工具 | `tools_used` 有名字，但 `trace.tool_calls=[]` | `required_tool_missing`、`tool_input_mismatch` | `skipped_hard_failure` | `null` | `false` |
| 参数错误 | 工具成功执行，但 `order_id=B-5678` | `tool_input_mismatch` | `skipped_hard_failure` | `null` | `false` |
| 无依据完成 | 工具只核验字段，回答却说“已退款完成” | `rule_violations: claimed_completed_operation` | `skipped_hard_failure` | `null` | `false` |
| 裁判异常 | 规则通过，但 Judge 无效 JSON / 缺 `reason` | 为空，`failures` 含 `judge_failed` | `failed` | `null` | `false` |

每个场景的讲法：

- **合格答案**：不是“文字看起来不错”，而是工具、参数、Skill、业务边界都过了，最后 Judge 也通过。
- **漏调工具**：`tools_used` 里有工具名，但 Trace 没有调用记录，所以不能证明工具真的执行过。
- **参数错误**：工具确实成功，但查错了订单。`success:true` 不能替代参数正确性。
- **无依据完成**：当前能力没有退款写入工具，回答却声称退款完成，属于业务硬违规。
- **裁判异常**：不是答案一定差，而是缺少有效评分证据，所以本轮不能算通过。

补充一：`process_failures` 里常见的失败码，可以快速定位执行阶段：

| `process_failures` 中的失败码 | 人话 |
|---|---|
| `trace_missing` | 没取到 Trace |
| `required_tool_missing:<工具名>` | 必需工具没有成功调用 |
| `tool_input_mismatch:<工具名>` | 工具调用了，但指定参数不匹配 |
| `required_skill_missing:<Skill名>` | 必需 Skill 没有出现在 Trace |
| `tool_order_mismatch` | 有依赖的工具顺序不对 |
| `output_contract_invalid` | 输出结构不符合契约 |
| `expected_agent_mismatch` | 实际处理角色和预期不符 |
| `require_knowledge_mismatch` | 该用知识检索但没用，或状态不符 |
| `fallback_not_allowed` | 用例不允许降级，但实际降级了 |
| `latency_budget_exceeded` | 超时或超过延迟预算 |

补充二：`rule_violations` 保存业务规则名，例如 `claimed_completed_operation`（无依据的完成声明）。页面下方红字来自 `failures`，它是更上层的汇总列表，还可能出现：

| `failures` 中的条目 | 人话 |
|---|---|
| `intent_mismatch` | 预测意图和预期意图不一致 |
| `missing_required_point:<内容>` | 回答缺少必含信息 |
| `judge_failed` | Judge 运行异常或返回无效结果 |
| `answer_quality_below_threshold` | Judge 有效运行，但均分低于通过线 |

也就是说：`process_failures` 和 `rule_violations` 是两类原始判定；`failures` 是把它们和其他问题合在一起的最终失败清单。

---

### 2.6 第 2 页完整讲法

> 这一页展示同一个退款用例的五种结果。左边是“应该怎么做”，中间是“Agent 说了什么、Trace 里实际做了什么”，右边是“为什么通过或不通过”。合格答案必须同时满足预期意图、工具真实成功调用、参数正确、Skill 记录和业务边界。`tools_used` 只有名字不能证明调用；`success:true` 也不能证明参数对。发现漏调、参数错误或无依据完成声明时，规则直接硬失败并跳过 Judge。只有硬条件通过后，才用 Judge 评语言质量。Judge 异常时 `answer_good` 是 `null`，表示缺少评分证据，不是零分。

---

## 3. 第 3 页：稳定性与离线发布检查

### 3.1 屏幕导览

这一页左右两块：

- **左侧：重复运行矩阵**。6 个示例场景 × 每场景 N 次重复；每个格子是一轮会话的最终验收。
- **右侧：离线发布检查沙盘**。根据矩阵和几个开关，动态给出 `blocked / review / passed`。

页面明确写着：这里是 6 个示例场景的门禁沙盘，不是现场重跑真实 Agent，也不会部署。

### 3.2 矩阵和三个成功率

矩阵单元格：

- `✓ PASS`：该场景该次重复的会话通过；
- `× FAIL`：该场景该次重复的会话失败；
- 点格子可以切换状态，用来演示。

顶部按钮：

| 按钮 | 作用 |
|---|---|
| 全过 | 把 6 个场景的所有重复都设为通过 |
| 偶发失败 | 把第 2、第 5 个场景的第一次重复设为失败 |
| P0 失败 | 把第一个 P0 场景的第一次重复设为失败，并勾选包含 P0 |

重复次数下拉框 `repeatSelect` 可选 1/2/3；页面默认 3。它改变的是每个场景重复几次，不是改变场景数。

三个指标的精确定义：

```text
至少一次成功率 = 至少有一轮通过的场景数 / 场景总数
全部运行成功率 = 所有重复都通过的场景数 / 场景总数
会话通过率 = 通过的会话数 / 所有会话数
```

以“偶发失败”为例，6 个场景、每个 3 次：

| 指标 | 计算 | 结果 |
|---|---|---|
| 至少一次成功 | 6 个场景都至少成功过一次：`6/6` | 100% |
| 全部运行成功 | 只有 4 个场景三轮全过：`4/6` | 66.67% |
| 会话通过率 | 18 次中 16 次通过：`16/18` | 88.89% |

这三个数放在一起看，才能区分：

- 有能力做成：至少一次成功；
- 重复可靠性：全部运行成功；
- 每次执行的成功占比：会话通过率。

### 3.3 Wilson 95% 区间

页面显示的 `interval` 是：

> 全部运行成功率的 Wilson 95% 区间：xx%–xx%。按 6 个固定示例场景计算，不是 18 个独立线上用户样本。

在这个页面里：

- `n` 是场景数 6，不是重复次数 18；
- 它表示对“全部运行成功率”这个比例的统计不确定区间；
- 样本是固定候选场景，所以不能外推成生产效果。

“偶发失败”时是 4/6，约等于 30.0%–90.3%。区间很宽，说明用 6 个固定场景估可靠性只能作为工程演示。

### 3.4 右侧门禁：先分清控件名和业务名

右侧开关的 HTML 控件 ID 是 camelCase，**不是评测报告 JSON 字段**。演示时讲中文标签即可：

| 控件 ID | 页面标签 | 勾选后代表什么 |
|---|---|---|
| `includeP0` | 包含 P0 场景 | 第一个场景带 P0 标记；勾选后会加入 P0 风险复核条件 |
| `businessViolation` | 业务硬违规 | 模拟出现业务禁用行为，直接阻断 |
| `intentLow` | 意图准确率 < 90% | 模拟意图准确率不达标，直接阻断 |
| `judgeUnavailable` | 额外人工复核标记 | 模拟额外人工复核要求，触发 `review`，不单独阻断；控件 ID 叫 judgeUnavailable |
| `rulesOnly` | 仅规则评测 | 不评语义质量，进入 review |
| `baselineSelect` | 基线比较 | 选择旧基线是否兼容、是否回退 |

`baselineSelect` 的选项：

| 值 | 人话 |
|---|---|
| `incompatible` | 现有旧基线和当前数据集/重复次数/评分器/Judge 配置不一致，不能比较 |
| `missing` | 没有基线 |
| `comparable` | 同配置可比，未发现回退 |
| `regression` | 同配置可比，但出现回退 |

### 3.5 门禁状态和原因码

门禁逻辑：

```text
有任一 blocked 原因 → blocked
否则有任一 review 原因 → review
否则 → passed
```

也就是说：`blocked` 优先级高于 `review`。

| 状态 | 什么时候出现 | 怎么讲 |
|---|---|---|
| `blocked` | 没有评测数据、意图准确率不足、业务硬违规、P0 会话失败、全部运行成功率 < 90%、可比基线回退 | 有明确阻断项，先修再谈发布 |
| `review` | P0/裁判异常/证据不足需要人工、仅规则、重复少于 3 次、没有可比基线 | 不是通过，也不是失败，而是证据不够或需要人判断 |
| `passed` | 没有阻断、也没有待复核项 | 仅表示当前离线检查满足条件，仍需线上灰度 |

真实报告里的对应原因包括：

- `no_evaluation_data`：没有评测数据；
- `intent_accuracy_below_threshold`：意图准确率低于阈值；
- `business_rule_violation`：业务硬违规；
- `p0_session_failed`：P0 会话失败；
- `all_runs_success_rate_below_0.9`：全部运行成功率低于 90%；
- `baseline_regression`：同配置基线发生回退；
- `manual_review_required`：需要人工复核；
- `semantic_quality_not_evaluated`：语义质量未评；
- `fewer_than_3_repetitions`：重复次数少于 3；
- `no_comparable_baseline`：没有配置一致的可比基线。

几个容易讲错的点：

1. **默认“全过”不一定是 `passed`**。页面默认勾选 P0、且旧基线不兼容，所以通常会进入 `review`；这是保守门禁，不是程序坏了。
2. **P0 场景即使这次全过，也可能要求人工复核**。因为页面把 P0 作为高优先级风险处理。
3. **“偶发失败”会被 `blocked`**。因为全部运行成功率 66.67% 低于 90%。
4. **只点矩阵不能发布**。页面只是门禁沙盘，不会修改真正 baseline，也不会部署。
5. 想把沙盘演示到 `passed`：保持全过、重复次数 3、取消 P0、选择“同配置可比，无回退”，并关闭其他阻断/复核开关。

### 3.6 第 3 页完整讲法

> 这一页把“能做”和“稳定做成”分开。至少一次成功率看能力可能性，全部运行成功率看重复可靠性，会话通过率看所有执行的成功占比。一个场景失败一次，这个场景就不再属于全部运行成功。门禁优先阻断业务硬违规、P0 失败、全部运行成功率低于 90% 和基线回退；证据不足或需要人工判断时进入 review。`passed` 仅表示离线检查通过，不代表生产发布许可。

---

## 4. 第 4 页：存档实验结果

### 4.1 屏幕导览

这一页展示仓库里已经保存的专项实验 JSON：

```text
data/eval/benchmark/benchmark_report.json
```

它不是第 3 页门禁沙盘的运行结果，也不是本次现场重跑。时间字段是 `2026-09-07`，面试时要主动说明口径。

页面主体有三部分：

1. 顶部四个指标卡；
2. 第二行三块：意图分类、RAG 检索、RAG 答案；
3. 底部来源说明。

另外，右下角可以点“查看 4 个存档错例”打开弹窗。

### 4.2 顶部四个指标卡

| 页面指标 | 值 | 人话 |
|---|---:|---|
| 意图 Accuracy · 425 / 500 | 85.00% | 500 条意图样本里 425 条分对 |
| 意图 Macro-F1 | 0.8492 | 先算 19 个类别各自的 F1，再等权平均 |
| 检索 Hit@3 | 81.67% | 120 道检索题里，Top-3 至少命中一个相关标题的比例 |
| 答案严格联合通过 | 86.67% | 30 道答案题里，事实、Judge、业务规则同时通过的比例 |

为什么 Accuracy 和 Macro-F1 都要看：

- `Accuracy`：按样本计数，大类样本多时更容易好看；
- `Macro-F1`：每个类别权重相同，更能暴露小类/弱类问题。

### 4.3 意图分类面板

页面背景 JSON 的主要字段：

```json
{
  "total": 500,
  "correct": 425,
  "accuracy": 0.85,
  "macro_f1": 0.8492,
  "per_class": {}
}
```

| 字段 | 人话 |
|---|---|
| `total` | 意图分类样本总数 |
| `correct` | 分类正确的样本数 |
| `accuracy` | 正确率，`correct / total` |
| `macro_f1` | 19 个类别 F1 的等权平均 |
| `per_class` | 每个类别的 precision、recall、f1、support |

页面只画了 5 个相对较弱类别的 F1 条，不是全部 19 类。英文标签对应：

| 标签 | 中文意思 |
|---|---|
| `escalation` | 升级/上报 |
| `query` | 查询 |
| `payment_issue` | 支付问题 |
| `request` | 请求办理 |
| `human_handoff` | 转人工 |

如果展开完整 JSON，会看到 `per_class[类别]` 下的：

- `precision`：预测成该类别的样本里，有多少真的属于该类别；
- `recall`：真实属于该类别的样本里，有多少被找出来；
- `f1`：precision 和 recall 的调和平均；
- `support`：该类别的真实样本数量。

`confusion_matrix` 是混淆矩阵：看“真实类别 → 预测类别”的分布，对角线附近是分对的，非对角线能看出哪两类容易混。

意图面板还显示：

> Accuracy Wilson 95% 区间：81.60%–87.86%

这是对这 500 条模板化合成样本的统计不确定区间，不代表真实用户分布，也不是人工金标。

### 4.4 RAG 检索面板

背景字段：

```json
{
  "total": 120,
  "multi_label_cases": 43,
  "k": {
    "1": {"hit_at_k": 0.425, "recall_at_k": 0.3375, "all_relevant_at_k": 0.2583, "errors": 0},
    "3": {"hit_at_k": 0.6917, "recall_at_k": 0.5847, "all_relevant_at_k": 0.4833, "errors": 0},
    "5": {"hit_at_k": 0.8167, "recall_at_k": 0.7028, "all_relevant_at_k": 0.5917, "errors": 0}
  }
}
```

| 字段 | 人话 |
|---|---|
| `total` | 检索题数，120 |
| `multi_label_cases` | 其中 43 道题有多个相关标题，所以 Hit 和 Recall 不一定相同 |
| `k` | 取前 1、3、5 条结果进行评测 |
| `hit_at_k` | Top-K 中至少命中一个相关标题的题目比例 |
| `recall_at_k` | Top-K 命中的相关标题占全部相关标题的比例 |
| `all_relevant_at_k` | Top-K 是否把该题所有相关标题都找齐 |
| `errors` | 该 K 值下检索调用失败数；此存档中都是 0 |

页面三档数值：

| Top-K | Hit@K | Recall@K | 全部相关标题命中 |
|---|---:|---:|---:|
| Top-1 | 42.50% | 33.75% | 25.83% |
| Top-3 | 69.17% | 58.47% | 48.33% |
| Top-5 | 81.67% | 70.28% | 59.17% |

例子：相关标题是 A、B，检索取回 A、C、D：

- `Hit@K = 1`，因为至少命中一个；
- `Recall@K = 1/2 = 0.5`，因为只覆盖两个相关标题中的一个；
- `all_relevant_at_k = 0`，因为 B 没找到。

### 4.5 RAG 答案面板

页面显示三个比例：

| 页面指标 | 字段 | 值 | 人话 |
|---|---|---:|---|
| 必含事实覆盖 | `fact_pass_rate` | 93.33% | 必含事实是否都出现 |
| Judge 通过 | `judge_pass_rate` | 96.67% | Judge 总分是否达到门槛 |
| 严格联合通过 | `strict_answer_pass_rate` | 86.67% | 事实 ∧ Judge ∧ 业务规则全部通过 |

严格联合通过的公式：

```text
strict_pass = fact_pass ∧ judge_pass ∧ rules_pass
```

所以不能只看 Judge 高分：Judge 通过但漏了必含事实，或者业务规则失败，严格结果仍然不通过。

完整存档 JSON 里还有：

| 字段 | 人话 |
|---|---|
| `total` | 答案题数，30 |
| `knowledge_used_rate` | 使用知识检索的比例，93.33% |
| `intent_accuracy` | 这 30 题上的意图准确率，86.67% |
| `average_scores` | 相关性、准确性、完整性、有用性、overall 的平均分 |
| `judge_failures` | Judge 运行失败次数，存档里是 0 |

### 4.6 来源口径和限制

页面底部明确写了几个限制，面试要主动承认：

1. 来源是 `data/eval/benchmark/benchmark_report.json`，不是本次重跑；
2. 数据是专项实验，不能等同于核心候选回归集，也不是独立人工金标；
3. 说明 Markdown 和 JSON 的 methodology 里还保留“60 道检索题”等旧文字，但当前页面按 JSON 的详细结果展示，检索题数是 120；
4. 这批结果不能直接外推成生产效果；
5. Judge 和 Agent 可能来自同一模型供应链，仍需要独立 Judge、人工一致性和更有代表性的样本。

可直接说：

> 这页的指标要连同样本和判定口径一起讲。85% 准确率来自 500 条模板化合成样本，不是人工金标；Hit、Recall、全部相关命中回答的是不同问题；严格联合通过比 Judge 通过低，是因为它还要求事实和业务规则都通过。另外我知道存档里存在 60 题/120 题的口径不一致，面试里以 JSON 的 120 题为准，不混用两套数字。

### 4.7 错例弹窗

点“查看 4 个存档错例”，每个 Tab 的标题是 `case_id`，例如 `rag-answer-002`。弹窗中展示：

| 字段 | 人话 |
|---|---|
| `case_id` | 错例编号 |
| `expected_intent` | 用例期望的意图 |
| `predicted_intent` | 模型实际预测的意图 |
| `knowledge_used` | 是否使用了知识检索 |
| `missing_facts` | 漏掉的必含事实列表 |
| `fact_pass` | 必含事实是否全部覆盖 |
| `judge_pass` | Judge 是否通过 |
| `rules_pass` | 业务规则是否通过 |
| `strict_pass` | 严格联合是否通过 |

如果展开完整 benchmark JSON，错例里还会有 `scores`（Judge 各维分数）、`judge_failed`（Judge 是否失败）和 `judge_error`（失败原因）。弹窗为了聚焦，只展示判定结果字段。

四个错例的讲法：

| 错例 | 表面现象 | 应该怎么分析 |
|---|---|---|
| `rag-answer-002` | 事实和 Judge 都通过，但 `rules_pass=false` | 先看规则命中的原文片段和上下文，确认是否规则误报；不能为了过线手动改高正式通过率 |
| `rag-answer-015` | 安全类回答，事实/Judge 通过但规则失败 | 看否定语境，区分“拒绝索取敏感信息”与“真的发生了敏感信息风险”；关键词命中不能直接证明风险 |
| `rag-answer-020` | Judge 通过，但漏了必含事实“稍后重试” | 先核对参考标签是否合理，再检查生成约束和检索结果，补回回归 |
| `rag-answer-027` | 漏了必含事实“99元”，Judge 也未通过 | 检查是否调用了检索、是否取回正确片段、回答是否忠于证据 |

错例的双重价值：

- 一方面暴露 Agent 的系统问题；
- 另一方面暴露评分器的规则误报、同义词漏判或 Judge 异常。

终态动作不是“把这一条手动标成通过”，而是：

```text
定位现象 → 区分系统问题 / 评分器问题
→ 修复系统或规则
→ 重跑原场景 + 同类回归
→ 人工认可后才更新基线或补充校准集
```

---

## 5. 补充：复现与答辩准备（不在 HTML 页面展示）

### 5.1 屏幕导览

左侧是面试讲解稿和四个 Q&A；右侧是复现命令和代码位置。

这一页不是新的评测结果，而是把前四页压缩成：

1. 一分钟能讲清的主线；
2. 四个常见追问；
3. 可以现场执行的复现命令；
4. 代码和职责边界。

### 5.2 四个 Q&A 的人话版

| 追问 | 可直接回答 |
|---|---|
| 为什么还需要规则？ | 模型可能偏好流畅回答。漏调工具、参数错误、无依据的退款完成声明，必须在 Judge 之前确定性拦截，否则语言质量会掩盖执行错误。 |
| 怎么发现根因？ | 用 `request_id` 串起输入、路由、工具、Skill 和 Trace。`findings` 只给候选模块和行动项，根因仍要证据复核，不能自动归责。 |
| 如何避免记忆串场？ | 用 run / case / repeat 生成独立身份；同一会话各轮保持身份一致。真正严格的评测还要使用独立基础设施，避免共享缓存、统计和容量干扰。 |
| 怎样对待高分？ | 先核对样本和评分器。候选集还没有业务复核，Judge 还需要独立模型和人工一致性校准，存档指标不能直接外推线上。 |

### 5.3 复现命令和参数

在 `python-service` 目录执行：

```bash
# 只校验用例，不调用服务或模型
.venv/bin/python -m evaluation.run_suite --dry-run

# 在独立评测环境跑真实 Agent 和 Judge
.venv/bin/python -m evaluation.run_suite --repeats 3

# 修复后只回归目标场景
.venv/bin/python -m evaluation.run_suite \
  --case-id billing-field-tool --repeats 3
```

| 参数 / 文件 | 人话 |
|---|---|
| `--dry-run` | 只校验用例格式，不调用 Agent 或模型 |
| `--repeats 3` | 每个场景跑 3 次，用来观察稳定性 |
| `--case-id billing-field-tool` | 只重跑目标用例；修复后做定向回归 |
| `--rules-only` | 关闭 Judge，但被测 Agent 仍可能调用模型；不是“完全离线不调模型” |
| `report.json` | 完整评测报告快照 |
| `badcases.json` | 从失败聚簇得到的错例/问题项 |
| `persist_report=true` | 显式保存报告和 badcases；不会自动覆盖基线 |
| `passed` 退出码 | CLI 退出 0 |
| `review / blocked` 退出码 | CLI 退出 1，不执行部署 |

### 5.4 代码位置怎么对应

| 内容 | 代码位置 |
|---|---|
| 输入字段、正则、逐轮长度校验 | `evaluation/contracts.py` |
| 隔离运行、Trace 采集、Judge、多轮目标、基线比较 | `evaluation/pi_evaluator.py` |
| 确定性 Trace 验收、稳定性、问题簇、离线门禁 | `evaluation/quality.py` |
| HTTP 入口与候选回归集 | `api/main.py`、`golden_set_v1.json` |

可直接说：

> 复现入口是 Python 的评测 CLI。`--dry-run` 只校验数据格式；正常运行会调用真实 Pi 和 Judge。修复后可以按 `case-id` 只回归目标场景，但最终还要看同类回归集和同配置基线。CLI 对 `review`/`blocked` 返回非零退出码，并且不会执行部署。

---

## 6. 一分钟总讲稿

> 我把客服 Agent 的评测做成规则优先的离线质量闭环。先用候选用例定义意图、必要信息、工具与 Skill、业务边界和多轮会话目标；然后由 Python 评测器隔离运行真实 Pi 链路，并在每轮结束立即按 `request_id` 保存 Trace。判定时先用确定性规则查工具是否真实成功、参数是否匹配、Skill 是否触发、顺序和业务边界是否符合；硬条件失败就直接拦截，不交给 Judge。规则通过后再用四维 Judge 评相关性、准确性、完整性和有用性。多轮会话还要看最终目标是否真正达成，重复运行看全部运行成功率，而不是只看一次成功。失败会按证据聚成问题簇，给出候选模块、行动项和验收方式；修复后重跑目标场景及同类回归，并且只有配置一致时才比较基线。当前离线结果是候选集上的质量基础，Judge 还需要独立模型和人工校准，`passed` 之后仍要做线上灰度验证。

---

## 7. 字段速查和常见追问

### 7.1 字段名总表

| 字段 / 前缀 | 一句话翻译 |
|---|---|
| `expected_*` | 期望值：应该是什么 |
| `required_*` | 必须满足或必须出现 |
| `forbidden_*` | 不允许出现 |
| `tool_inputs` | 指定工具必须匹配的入参片段 |
| `response` | Agent 返回对象；其中 `response.response` 是回答文本 |
| `tools_used` | 返回中声称/汇总的工具名 |
| `trace` | 实际执行轨迹 |
| `tool_calls` | 真实工具调用列表 |
| `tool_name` | 实际调用的工具名 |
| `input` | 工具实际收到的入参 |
| `output` | 工具实际返回的结果 |
| `success` | 工具执行是否成功 |
| `is_error` | 是否被标记为错误 |
| `process_failures` | 过程检查失败 |
| `rule_violations` | 业务或输出规则违规 |
| `failures` | 汇总失败原因 |
| `judge_status` | Judge 的状态：完成、跳过、失败或仅规则 |
| `scores` | Judge 各维度分数 |
| `answer_good` | 答案质量结论；`null` 是未判定，不是零分 |
| `passed` | 单轮/会话结果：最终是否通过 |
| `turns_passed` | 多轮逐轮是否通过；页面示意结构 |
| `session_complete` | 预期轮数是否执行完整 |
| `expected_outcome` | 多轮最终要达成的目标 |
| `outcome_checked` | 最终目标是否被检查 |
| `session_passed` | 会话最终结论；真实报告通常看 `passed` |
| `at_least_once_success_rate` | 场景至少一次通过的比例 |
| `all_runs_success_rate` | 场景所有重复都通过的比例 |
| `session_success_rate` | 会话层面的通过率 |
| `interval` | 统计区间；页面指 Wilson 95% |
| `failure_type` | 失败类型/失败码 |
| `candidate_modules` | 候选责任模块，不是已确认根因 |
| `root_cause_status` | 根因调查状态；`needs_evidence_review` 表示还没确认根因 |
| `action` | 建议的修复动作 |
| `acceptance` | 修复后如何回归验收 |
| `human_review` | 是否需要人工复核；P0、Judge 异常、Trace 异常等可能触发 |
| `baseline_comparison` | 与历史基线是否可比的比较结果；常见状态有 `missing`、`incompatible`、`comparable`、`invalid` |
| `regressions` | 可比基线中检测到的回退项 |
| `release_gate.status` | 离线发布检查结论：`blocked` / `review` / `passed` |
| `blocked_reasons` / `review_reasons` | 门禁命中的阻断原因/复核原因列表 |
| `blocked` | 明确阻断，优先级高于 `review` |
| `review` | 需要补充证据或人工复核 |
| `passed`（门禁状态） | 离线检查通过，不等于生产发布 |

### 7.2 五个最容易讲错的点

1. **“`tools_used` 里有工具名”不等于工具真的调用。**
   证据看 `trace.tool_calls`，还要看 `success` 和 `is_error`。

2. **“`success=true`”不等于参数正确。**
   它只说明工具执行成功。输入是否匹配要看 `input` 和 `tool_inputs`。

3. **“Judge 跳过”不等于“0 分”。**
   `judge_status=skipped_hard_failure` 或 `failed`，`answer_good=null`，属于没有有效评分证据。

4. **“每轮都通过”不等于“会话通过”。**
   还要看轮数是否完整、最终目标是否被检查并达成。

5. **“离线 `passed`”不等于“可以发布”。**
   它只是离线质量门禁通过，后面仍然需要线上灰度、监控和人工判断。

### 7.3 面试官可能继续追问

**问：为什么不直接让 LLM 打分？**

> 因为 LLM 可能被流畅表达带偏，也可能看不到工具真实执行情况。漏调工具、参数错误、越权声明必须用确定性规则先拦；Judge 只负责规则无法覆盖的语义质量。

**问：为什么 `answer_good:null`？**

> 因为 Judge 没拿到有效结论。可能是硬失败跳过，也可能是 Judge 返回了无效 JSON。`null` 表示未判定，不是质量为零，不能把它混入均分。

**问：为什么全部运行成功率比至少一次成功率更适合门禁？**

> 至少一次只能说明“有能力做成”，不能说明可靠。线上用户不会只在你运气好的那次调用你；门禁要的是重复执行也稳定。

**问：`P0` 是什么？**

> 最高业务优先级的用例或场景。P0 失败通常阻断或要求人工复核；页面里的 `includeP0` 只是门禁沙盘的开关，不是报告字段。

**问：为什么有 4 个错例还要看规则？**

> 因为错例可能同时暴露系统错误和评分器错误。Judge 通过但规则失败，可能是规则误报；Judge 通过但事实缺失，说明语义判断不能替代事实检查。最终要区分系统问题、规则问题和 Judge 问题。

### 7.4 五分钟演示路线

如果面试时间有限，按这个动线讲：

1. 开场：一句话说明“不是打分，是任务验收闭环”；
2. 第 1 页：切到节点 1、3、4、5、7、9，讲完标准、Trace、规则、Judge、稳定性和门禁；
3. 第 2 页：切“合格答案 → 漏调工具 → 参数错误 → 无依据完成 → 裁判异常”，重点讲 `tools_used` 与 `trace.tool_calls` 的差别；
4. 第 3 页：先按“偶发失败”，再讲三个成功率；然后说明 `blocked` 优先于 `review`；
5. 第 4 页：说清存档数据口径，点一个错例，强调错例同时改系统和评分器；
6. 用一分钟总讲稿收尾，补一句“离线通过仍要做线上灰度”。

---

## 8. 快捷键

- `1–4`：切换四个章节；
- `← / →`：翻页；
- 空格：第 1 页播放/暂停链路；
- `F`：全屏；
- 第 2 页点击顶部 Tab 切换场景；
- 第 3 页点击矩阵格子、预设按钮和开关改变门禁；
- 第 4 页切换 `Top-1/3/5`，点击错例按钮；
- `Esc`：关闭错例弹窗。
