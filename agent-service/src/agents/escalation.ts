import type { AgentRunInput, AgentRunResult } from "./executor.js";
import type { ChatEventSink } from "../types.js";
import { AgentStats } from "./stats.js";
import { ESCALATION_PROFILE } from "./profiles.js";

/**
 * 独立升级节点：不调用 LLM、不调用知识库，只生成标准化交接摘要。
 * 生产环境可在这里接工单系统、人工队列或 Webhook。
 *
 * 作用：处理用户明确要求转人工或紧急度为 critical 的请求，跳过
 * general、technical、billing 等业务 Agent，避免继续模拟后台操作。
 * 当前实现只生成升级说明和安全提醒，不会真正创建工单或连接人工客服。
 *
 * 输入：AgentRunInput。当前主要使用：
 * - entities：订单号、金额、错误码等已提取信息，用于生成交接摘要
 * - intent：升级意图，缺省为 unknown
 * - urgency：紧急度，缺省为 high
 * message、convId、memoryContext、skillPrompt、allowKnowledge、workflowContext
 * 虽然属于统一 AgentRunInput 契约，但当前节点不会读取它们。
 *
 * 输出：AgentRunResult：
 * - agentType="escalation"、response=标准化升级摘要、escalated=true
 * - knowledgeUsed=false、toolsUsed=[]、toolTraces=[]、latencyMs=本次处理耗时
 *
 * 流式调用时依次发送 agent_start 和一条包含完整升级摘要的 text_delta，
 * 不产生 tool_start 或 tool_end 事件。
 */
export class EscalationExecutor {
  readonly agentType = "escalation" as const;
  readonly profile = ESCALATION_PROFILE;
  readonly stats = new AgentStats();

  async run(input: AgentRunInput, sink?: ChatEventSink): Promise<AgentRunResult> {
    const startedAt = performance.now();
    await sink?.({ type: "agent_start", agent_type: this.agentType });
    const intent = input.intent ?? "unknown";
    const urgency = input.urgency ?? "high";
    const entities = Object.keys(input.entities).length ? JSON.stringify(input.entities) : "无";
    const response = [
      "我已将这个问题标记为人工升级处理。",
      `升级原因：意图=${intent}，紧急度=${urgency}`,
      `已记录信息：${entities}`,
      "请不要发送密码、短信验证码或完整支付凭证；人工客服会根据会话记录继续核验。",
    ].join("\n");
    await sink?.({ type: "text_delta", agent_type: this.agentType, delta: response });
    this.stats.record(true, performance.now() - startedAt);
    return {
      agentType: this.agentType,
      response,
      knowledgeUsed: false,
      toolsUsed: [],
      toolTraces: [],
      escalated: true,
      latencyMs: performance.now() - startedAt,
    };
  }
}
