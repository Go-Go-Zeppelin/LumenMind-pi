import type { AgentType } from "../types.js";

export interface WorkflowResult {
  agent_type: AgentType;
  stages: string[];
  context: string;
}

/**
 * Agent 的确定性前置工作流。它不是 Prompt 文本，而是在模型调用前真实执行，
 * 为不同领域生成不同的结构化处理上下文。
 *
 * 作用：在调用 LLM 前，根据 Agent 类型预先整理该领域的处理阶段和已知线索，
 * 让模型按照相对稳定的业务流程回答。它本身不调用 LLM、知识库或外部接口，
 * 也不会修改输入数据。
 *
 * 输入：
 * - agentType：general、technical 或 billing，决定使用哪套确定性工作流
 * - message：当前用户消息；General 用它生成截断后的消息摘要
 * - entities：意图识别阶段提取的订单号、金额、错误码等结构化实体
 *
 * 输出：WorkflowResult：
 * - agent_type：实际生成工作流的 Agent 类型
 * - stages：该领域建议遵循的处理阶段列表
 * - context：由消息和实体整理出的结构化文本
 *
 * 示例（技术问题）：
 * ```ts
 * runAgentWorkflow("technical", "登录报错 401", { error_code: ["401"] })
 * // {
 * //   agent_type: "technical",
 * //   stages: ["识别故障类型", "提取错误码", "生成低风险排障计划", "判断升级条件"],
 * //   context: "错误码=401；建议步骤=[...]; 结构化实体={\"error_code\":[\"401\"]}"
 * // }
 * ```
 * 也就是说，函数不会直接回答“如何解决登录问题”，而是先把错误码和建议处理步骤整理好，
 * 供后续 Technical Agent 参考。
 *
 * Orchestrator 将输出的 context 写入 AgentRunInput.workflowContext，Executor 随后
 * 把它作为“执行工作流”内容合并进 system prompt；stages 当前主要用于描述和测试。
 */
export function runAgentWorkflow(
  agentType: AgentType,
  message: string,
  entities: Record<string, string[]>,
): WorkflowResult {
  const safe = (value: string) => value.replace(/[\u0000-\u001f]/g, " ").trim();
  const entityText = JSON.stringify(entities);
  if (agentType === "technical") {
    const errorCode = entities.error_code?.[0] || "未识别";
    const steps = ["确认错误码和复现步骤", "检查客户端/接口配置", "必要时收集日志并升级"];
    return {
      agent_type: agentType,
      stages: ["识别故障类型", "提取错误码", "生成低风险排障计划", "判断升级条件"],
      context: `错误码=${safe(errorCode)}；建议步骤=${JSON.stringify(steps)}；结构化实体=${entityText}`,
    };
  }
  if (agentType === "billing") {
    const amount = entities.amount?.[0] || "未提供";
    const orderId = entities.order_id?.[0] || "未提供";
    const required = ["订单号", "扣款时间", "金额和支付渠道"];
    return {
      agent_type: agentType,
      stages: ["识别账单场景", "核对订单和金额", "说明政策及时效", "判断人工审核条件"],
      context: `个人订单/账单查询先调用业务工具，列表无需编号；查询成功按演示记录回复。仅资金争议或操作需要补充核验字段。订单号=${safe(orderId)}；金额=${safe(amount)}；操作需核验=${JSON.stringify(required)}；结构化实体=${entityText}`,
    };
  }
  return {
    agent_type: agentType,
    stages: ["理解用户问题", "确认关键字段", "给出可验证下一步", "判断是否需要升级"],
    context: `个人订单/账单查询先调用业务工具，列表无需编号；查询成功按演示记录回复。消息摘要=${safe(message.slice(0, 240))}；结构化实体=${entityText}`,
  };
}
