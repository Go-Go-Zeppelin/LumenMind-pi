import type { AgentType } from "../types.js";

export interface AgentProfile {
  role: string;
  mission: string;
  workflow: string[];
  input_contract: string[];
  output_contract: string[];
  output_fields: string[];
  handoff_conditions: string[];
  tool_scope: string[];
  temperature: number;
  max_tokens: number;
}

export const AGENT_PROFILES: Record<AgentType, AgentProfile> = {
  general: {
    role: "通用客服分诊 Agent",
    mission: "通过只读工具查询当前用户的演示订单和账单，处理通用咨询及跨领域问题。",
    workflow: ["理解用户问题", "个人记录先调用业务查询工具，列表不要求编号", "政策查询知识库", "按工具结果回复，必要时升级"],
    input_contract: ["用户消息", "本次用户身份", "结构化实体", "记忆上下文", "意图和紧急度"],
    output_contract: ["直接回复", "待补充字段", "安全边界", "升级建议"],
    output_fields: ["response", "steps", "required_info", "escalate"],
    handoff_conditions: ["信息不足无法继续", "涉及真实后台操作", "用户明确要求人工"],
    tool_scope: ["knowledge_search", "inspect_request_context", "suggest_required_fields", "list_orders", "get_order", "list_bills", "get_bill"],
    temperature: 0.25,
    max_tokens: 1024,
  },
  technical: {
    role: "技术支持 Agent",
    mission: "诊断登录、崩溃、错误码和接口接入问题，提供低风险排障步骤。",
    workflow: ["识别故障类型", "解释错误含义", "给出分步排查", "确认是否需要日志或权限"],
    input_contract: ["用户消息", "错误码实体", "设备/版本线索", "记忆上下文"],
    output_contract: ["原因假设", "排查步骤", "所需信息", "升级条件"],
    output_fields: ["response", "diagnosis", "steps", "required_info", "escalate"],
    handoff_conditions: ["需要后台日志", "需要管理员权限", "排查步骤无法复现"],
    tool_scope: ["knowledge_search", "lookup_error_code", "build_diagnostic_plan"],
    temperature: 0.15,
    max_tokens: 1200,
  },
  billing: {
    role: "账单服务 Agent",
    mission: "通过只读工具查询当前用户的演示订单和账单，解释退款、发票和支付政策。",
    workflow: ["识别账单场景", "个人记录先调用业务查询工具，列表不要求编号", "核对金额/订单线索", "说明政策和人工操作边界"],
    input_contract: ["用户消息", "本次用户身份", "订单号和金额实体", "记忆上下文", "业务知识"],
    output_contract: ["政策说明", "核验字段", "处理时效", "人工审核条件"],
    output_fields: ["response", "steps", "required_info", "escalate"],
    handoff_conditions: ["真实资金操作", "退款状态需要后台核验", "账单信息冲突"],
    tool_scope: ["knowledge_search", "check_billing_fields", "compare_amounts", "list_orders", "get_order", "list_bills", "get_bill"],
    temperature: 0.1,
    max_tokens: 1100,
  },
};

export const ESCALATION_PROFILE: AgentProfile = {
  role: "人工升级与交接节点",
  mission: "整理升级原因和已知信息，停止继续模拟后台操作。",
  workflow: ["确认升级原因", "整理已知信息", "标记优先级", "生成交接摘要"],
  input_contract: ["用户消息", "意图", "紧急度", "结构化实体", "对话背景"],
  output_contract: ["升级原因", "已知信息摘要", "安全提醒", "人工后续说明"],
  output_fields: ["response", "handoff_reason", "known_information", "escalate"],
  handoff_conditions: ["用户明确要求人工", "紧急或高风险场景"],
  tool_scope: [],
  temperature: 0,
  max_tokens: 500,
};


/*
`profiles.ts` 大白话来说，就是：

> 给每个 Agent 写的一份“岗位说明书和工作规范”。

它规定不同 Agent 是干什么的、按什么流程处理问题、可以接收和输出什么内容、什么时候应该转人工。

例如 Technical Agent 的配置会告诉系统：

- 你是技术支持；
- 主要处理登录失败、崩溃、错误码和 API 问题；
- 要按照“识别故障 → 解释错误 → 分步排查 → 判断升级”的流程工作；
- 输出必须包含回答、诊断、处理步骤、所需信息和是否升级；
- 如果需要后台日志或管理员权限，就应该建议升级；
- 可以使用哪些工具；
- 模型回答时使用什么温度和最大输出长度。

Billing Agent 则是另一套规范，重点处理退款、发票、扣款和订阅问题。

可以把它理解成：

```text
profiles.ts
    = Agent 岗位说明书
    + 标准工作流程
    + 输入输出合同
    + 转人工条件
    + 模型运行参数
```

它本身不会执行 Agent，也不会查询数据库。真正运行 Agent 的是 `executor.ts`，但 `executor.ts` 会读取 `profiles.ts` 中的配置来构建提示词、约束输出并设置模型参数。

一句面试表达：

> `profiles.ts` 是所有 Agent 的统一配置中心，通过声明式配置定义每个 Agent 的角色、职责、工作流、输入输出契约、工具范围和升级条件，避免把这些规则散落在执行代码中。


*/
