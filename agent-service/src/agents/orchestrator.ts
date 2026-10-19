import { randomUUID } from "node:crypto";
import type { AgentTool } from "@earendil-works/pi-agent-core";
import type { AppConfig } from "../config.js";
import type { InfrastructureClient } from "../clients/infrastructure.js";
import type { SkillManager } from "../skills/loader.js";
import type {
  AgentType,
  ChatEventSink,
  ChatRequest,
  ChatResponse,
  Intent,
  IntentDecision,
  MemoryMessage,
  MemoryContext,
  RequestTrace,
  RoutingAgentType,
  MemoryOperationReceipt,
} from "../types.js";
import { KeyedLock } from "../utils/keyed-lock.js";
import { EscalationExecutor } from "./escalation.js";
import { ResponseComposer } from "./composer.js";
import { AgentExecutionError, PiAgentExecutor, type AgentRunResult } from "./executor.js";
import { IntentRouter } from "./intent-router.js";
import { AGENT_PROFILES } from "./profiles.js";
import { runAgentWorkflow } from "./workflow.js";
import { roleTools } from "../tools/agent-tools.js";
import { MemoryCoordinator, isPureMemoryRequest, memoryReceiptText, finalizeMemoryReply } from "./memory-coordinator.js";
import { ModelBudgetError } from "./model-budget.js";
import { withMemoryTurn } from "./memory-turn.js";

// 编排器接收 IntentDecision 后，不直接用 intent 字符串查 Agent，
// 而是结合关键词和结构化实体计算三个领域的分数。
const TECHNICAL_INTENTS = new Set<Intent>(["technical", "technical_login", "technical_crash"]);
const BILLING_INTENTS = new Set<Intent>(["billing", "account", "account_security", "refund", "invoice", "payment_issue"]);
const GENERAL_INTENTS = new Set<Intent>([
  "query", "order_status", "logistics", "request", "complaint", "greeting", "feedback", "other",
]);
const HANDOFF_INTENTS = new Set<Intent>(["escalation", "human_handoff"]);

const TECHNICAL_WORDS = ["崩溃", "报错", "error", "crash", "无法登录", "登录失败", "登录报错", "500", "401", "403", "验证码", "超时"];
const BILLING_WORDS = ["退款", "退货", "扣款", "重复扣款", "多扣", "发票", "账单", "支付", "订阅", "续费", "refund", "invoice", "billing"];
const GENERAL_WORDS = ["订单", "物流", "快递", "配送", "会员", "积分", "咨询", "帮助"];

export interface RoutingDecision {
  // 计划中负责主要回答的领域；escalation 对应独立的非 LLM 升级节点。
  primaryAgent: RoutingAgentType;
  // 计划中需要协助回答的领域，当前最多包含 technical/billing。
  supportingAgents: AgentType[];
  // 给客户端和监控使用的可解释路由原因。
  reason: string;
  // 根据主领域分数计算的路由置信度，范围为 0 到 1。
  confidence: number;
}

/** 返回一条消息命中了某个领域关键词的次数。 */
function hits(message: string, words: string[]): number {
  const lower = message.toLowerCase();
  return words.filter((word) => lower.includes(word)).length;
}

export function domainScores(message: string, decision: IntentDecision): Record<AgentType, number> {
  /**
   * 将 IntentDecision 转成领域分数。
   *
   * 输入：
   * - message：原始用户消息，用于统计技术、账单、通用关键词
   * - decision：IntentRouter 的结构化输出，用于读取 intent 和 entities
   *
   * 输出：{ general, technical, billing }，例如：
   * { general: 0.1, technical: 1.49, billing: 0.6 }
   * 后续 makeRoutingDecision() 按分数排序选择主 Agent 和辅助 Agent。
   */
  const scores: Record<AgentType, number> = { general: 0.1, technical: 0, billing: 0 };
  if (GENERAL_INTENTS.has(decision.intent)) scores.general += 0.55;
  if (TECHNICAL_INTENTS.has(decision.intent)) scores.technical += 0.75;
  if (BILLING_INTENTS.has(decision.intent)) scores.billing += 0.75;

  const technicalHits = hits(message, TECHNICAL_WORDS);
  const billingHits = hits(message, BILLING_WORDS);
  const generalHits = hits(message, GENERAL_WORDS);
  if (technicalHits) scores.technical += Math.max(0.45, Math.min(0.54, technicalHits * 0.18));
  if (billingHits) scores.billing += Math.max(0.45, Math.min(0.54, billingHits * 0.18));
  scores.general += Math.min(0.35, generalHits * 0.12);

  if (decision.entities.error_code?.length) scores.technical += 0.2;
  if (decision.entities.amount?.length) scores.billing += 0.15;
  if (decision.entities.order_id?.length) scores.general += 0.1;
  return Object.fromEntries(
    Object.entries(scores).map(([agent, score]) => [agent, Number(score.toFixed(3))]),
  ) as Record<AgentType, number>;
}

export function makeRoutingDecision(
  message: string,
  decision: IntentDecision,
  unavailableAgents: ReadonlySet<AgentType> = new Set<AgentType>(),
): RoutingDecision {
  /**
   * 输入：原始消息 + IntentDecision。
   * 输出：RoutingDecision，描述“计划让谁处理”，不代表这些 Agent 一定成功返回。
   *
   * 优先级：紧急/转人工 -> escalation；否则按领域分数决定主 Agent，
   * 再按两个阈值决定是否加入 supportingAgents。unavailableAgents 由监控
   * 降权计算得到，专业 Agent 被移除后由 General 接管。
   */
  if (decision.urgency === "critical") {
    return { primaryAgent: "escalation", supportingAgents: [], reason: "紧急度为 CRITICAL，触发升级路由", confidence: 1 };
  }
  if (HANDOFF_INTENTS.has(decision.intent)) {
    return {
      primaryAgent: "escalation",
      supportingAgents: [],
      reason: `意图为 ${decision.intent}，触发升级路由`,
      confidence: Math.max(decision.confidence, 0.8),
    };
  }

  const scores = domainScores(message, decision);
  // 监控降权达到阈值的专业 Agent 从本次候选中移除；General 始终保留作为安全回退。
  const ordered = (Object.entries(scores) as Array<[AgentType, number]>)
    .filter(([agentType]) => agentType === "general" || !unavailableAgents.has(agentType))
    .sort((left, right) => right[1] - left[1]);
  const [primaryAgent = "general", primaryScore = 0.1] = ordered[0] ?? [];
  const supportingAgents = ordered.slice(1)
    .filter(([agentType, score]) => agentType !== "general" && score >= 0.45 && score >= Math.min(primaryScore, 1) * 0.4)
    .map(([agentType]) => agentType);
  const scoreText = ordered.map(([agentType, score]) => `${agentType}=${score.toFixed(2)}`).join(", ");
  const degraded = [...unavailableAgents].sort().join(", ");
  const fallbackReason = degraded ? `, monitor_fallback=[${degraded}]` : "";
  return {
    primaryAgent,
    supportingAgents,
    reason: `intent=${decision.intent}, group=${decision.intent_group}, primary=${primaryAgent}, supporting=${supportingAgents.join(",") || "none"}, scores=[${scoreText}]${fallbackReason}`,
    confidence: Number(Math.min(primaryScore, 1).toFixed(3)),
  };
}

export function needsClarification(message: string, decision: IntentDecision): boolean {
  // 输入低置信度的 other 时先澄清，避免把不明确的问题误交给业务 Agent。
  // 输出 true 时，主链路不会创建业务 Pi Agent，但仍会写入记忆。
  return decision.intent === "other" && message.trim().length > 2 && decision.confidence < 0.5;
}

export function shouldUseKnowledge(_intent: Intent): boolean {
  // knowledge_search 是所有业务 Agent 的共享只读工具。是否实际检索由
  // Agent 根据当前问题和上下文决定，路由意图不再构成一次预检索或硬门控。
  return true;
}

function needsEscalation(intent: Intent, urgency: string, response: string): boolean {
  // 输入意图、紧急度和最终回复；输出是否在业务语义上标记 escalated。
  // 回复中出现人工相关措辞时，即使原始意图不是 handoff，也会标记升级。
  if (HANDOFF_INTENTS.has(intent) || urgency === "critical") return true;
  return ["转人工", "人工客服", "人工审核", "二线技术", "escalate", "specialist", "无法处理"]
    .some((word) => response.toLowerCase().includes(word.toLowerCase()));
}

export class LumenMindOrchestrator {
  private readonly intentRouter: IntentRouter;
  private readonly agents: Record<AgentType, PiAgentExecutor>;
  private readonly agentPools: Record<AgentType, PiAgentExecutor[]>;
  private readonly escalation: EscalationExecutor;
  private readonly composer: ResponseComposer;
  private readonly memoryCoordinator: MemoryCoordinator;
  private readonly lock = new KeyedLock();
  private readonly recentToolTraces: RequestTrace[] = [];

  constructor(
    private readonly appConfig: AppConfig,
    private readonly infrastructure: InfrastructureClient,
    private readonly skills: SkillManager,
    knowledgeTool: AgentTool,
  ) {
    const embeddingSearch = typeof (infrastructure as unknown as { searchIntentCandidates?: unknown }).searchIntentCandidates === "function"
      ? infrastructure.searchIntentCandidates.bind(infrastructure)
      : undefined;
    this.intentRouter = new IntentRouter(appConfig, embeddingSearch);
    const makePool = (agentType: AgentType, count: number) => Array.from(
      { length: Math.max(1, count) },
      () => new PiAgentExecutor(agentType, appConfig, knowledgeTool, AGENT_PROFILES[agentType], roleTools(agentType), infrastructure),
    );
    this.agentPools = {
      general: makePool("general", appConfig.generalReplicas),
      technical: makePool("technical", appConfig.technicalReplicas),
      billing: makePool("billing", appConfig.billingReplicas),
    };
    this.agents = {
      general: this.agentPools.general[0]!,
      technical: this.agentPools.technical[0]!,
      billing: this.agentPools.billing[0]!,
    };
    this.escalation = new EscalationExecutor();
    this.composer = new ResponseComposer(appConfig);
    this.memoryCoordinator = new MemoryCoordinator(infrastructure);
  }

  async chat(request: ChatRequest, sink?: ChatEventSink, signal?: AbortSignal): Promise<ChatResponse> {
    const userId = request.user_id?.trim() || "anonymous";
    const requestedConv = request.conv_id?.trim() || undefined;
    const lockKey = JSON.stringify([userId, requestedConv ?? request.idempotency_key ?? randomUUID()]);
    return this.lock.run(lockKey, () => withMemoryTurn(
      this.infrastructure, { ...request, message: request.message.trim(), user_id: userId, conv_id: requestedConv },
      this.appConfig.memoryRenewIntervalMs, signal,
      async (turn) => this.runChat(request.message.trim(), userId, turn.convId, turn.requestId, turn.available, turn.signal, sink),
    ));
  }

  /**
   * 只执行意图识别，不读取/写入 Python 记忆，也不运行业务 Agent。
   * 是否允许 IntentRouter 在低置信度时调用 LLM 由调用方传入的 appConfig 决定。
   */
  async recognizeIntent(message: string, history: MemoryMessage[] = []): Promise<IntentDecision> {
    return this.intentRouter.recognize(message.trim(), history);
  }

  /**
   * 单次对话主链路（已在调用方 chat() 中通过 KeyedLock 获取会话锁）。
   *
   * 输入数据：
   * @param message 用户消息，已由 chat() trim 过；直接来自 ChatRequest.message。
   * @param userId  标准化后的用户 ID（默认 "anonymous"）。
   * @param convId  标准化后的会话 ID；客户端未传时由 chat() 生成 UUID。
   * @param sink    可选 SSE 事件回调；由 /chat/stream 注入，/chat 不传（undefined）。
   *
   * 输出数据：
   * @returns ChatResponse，包含：
   *   - response: 最终合并后的 Agent 回复文本
   *   - intent / intent_group / intent_confidence / intent_source_scores: 意图路由结果
   *   - primary_agent / supporting_agents: 计划路由（可能为 escalation）
   *   - agent_type / agent_types: 实际成功返回的 Agent
   *   - knowledge_used: 任一 Agent 实际调用 knowledge_search 且拿到非 fallback 结果
   *   - entities: 规则实体
   *   - conv_id / request_id / escalated / latency_ms / routing_reason / routing_confidence
   *
   * 内部数据流：
   *   message + userId + convId
   *     -> InfrastructureClient.getMemoryContext(userId, convId, message)
   *     -> MemoryContext（失败时为空上下文）
   *     -> IntentRouter.recognize(message, memory.recent_messages)
   *     -> IntentDecision
   *     -> needsClarification? 澄清响应（不执行 Pi Agent，仍写记忆）
   *     -> makeRoutingDecision(message, intent, monitorDegradedAgents) -> RoutingDecision
   *     -> Promise.all(runWithFallback(...)) -> AgentRunResult[]
   *     -> 合并主/辅回复 -> commitTurn 写回 Python 记忆 -> ChatResponse
   */
  private async runChat(message: string, userId: string, convId: string, requestId: string, memoryAvailable: boolean, signal: AbortSignal, sink?: ChatEventSink): Promise<ChatResponse> {
    const startedAt = performance.now();
    let memoryStatus: ChatResponse["memory_status"] = memoryAvailable ? "stored" : "unavailable";
    let memoryOperation: MemoryOperationReceipt | undefined;
    signal.throwIfAborted();
    const canCoordinateMemory = typeof (this.infrastructure as unknown as { flushMemory?: unknown }).flushMemory === "function";
    if (canCoordinateMemory && memoryAvailable) {
      try {
        memoryOperation = await this.memoryCoordinator.coordinate(message, userId, convId, requestId);
      } catch (error) {
        memoryOperation = { request_id: requestId, status: "FAILED", applied: 0, mutated: false };
        console.warn("显式记忆操作失败:", error instanceof Error ? error.message : error);
      }
    }
    if (!memoryAvailable && canCoordinateMemory) {
      memoryOperation = await this.memoryCoordinator.unavailable(message, requestId);
    }
    signal.throwIfAborted();
    if (memoryOperation && isPureMemoryRequest(message)) {
      return {
        conv_id: convId, request_id: requestId, response: memoryReceiptText(memoryOperation),
        intent: "request", intent_group: "request", agent_type: "general", agent_types: [],
        primary_agent: "general", supporting_agents: [], routing_reason: "显式记忆操作回执",
        routing_confidence: 1, escalated: false, latency_ms: performance.now() - startedAt,
        knowledge_used: false, tools_used: [], output_valid: true, output_errors: [],
        entities: {}, intent_confidence: 1, intent_source_scores: {}, memory_status: memoryStatus,
        memory_operation: memoryOperation,
      };
    }
    // 2) 读记忆：{user_id, conv_id, query} -> MemoryContext；Python 不可用时返回可区分的降级状态。
    const memory: MemoryContext = memoryAvailable
      ? await this.infrastructure.getMemoryContext(userId, convId, message, requestId)
      : { prompt_text: "", recent_messages: [], relevant_history: [], user_profile: {}, summary: "", status: "unavailable" as const };
    if (memory.status === "unavailable" || memory.status === "degraded") memoryStatus = memoryStatus === "stored" ? memory.status : memoryStatus;
    // 3) 意图识别：message + recent_messages -> IntentDecision。
    const intent = await this.intentRouter.recognize(message, memory.recent_messages, memory.routing_context, signal);
    signal.throwIfAborted();

    if (needsClarification(message, intent)) {
      const response = finalizeMemoryReply("我还不能确定您要处理的是哪类问题。请补充一下是订单物流、退款账单、账户资料，还是技术故障？", memoryOperation);
      const result: ChatResponse = {
        conv_id: convId,
        response,
        intent: intent.intent,
        intent_group: intent.intent_group,
        agent_type: "general",
        agent_types: ["general"],
        primary_agent: "general",
        supporting_agents: [],
        routing_reason: "低置信度 OTHER 意图，先澄清用户需求",
        routing_confidence: intent.confidence,
        escalated: false,
        latency_ms: Number((performance.now() - startedAt).toFixed(1)),
        knowledge_used: false,
        tools_used: [],
        output_valid: true,
        output_errors: [],
        entities: intent.entities,
        intent_confidence: Number(intent.confidence.toFixed(4)),
        intent_source_scores: intent.source_scores,
        request_id: requestId,
        memory_status: memoryStatus,
        memory_operation: memoryOperation,
      };
      this.recordToolTrace({
        request_id: requestId,
        timestamp: new Date().toISOString(),
        user_id: userId,
        conv_id: convId,
        message,
        response,
        intent: intent.intent,
        intent_group: intent.intent_group,
        primary_agent: "general",
        supporting_agents: [],
        agent_types: ["general"],
        tools_used: [],
        tool_calls: [],
        skills_used: [],
        escalated: false,
        latency_ms: result.latency_ms,
      });
      return result;
    }

    // 3) 结构化路由：message + IntentDecision -> RoutingDecision。
    const routing = makeRoutingDecision(message, intent, this.monitorDegradedAgents(message, intent));
    // 4) escalation 由独立节点处理；普通路由再组装主 Agent 和辅助 Agent。
    if (routing.primaryAgent === "escalation") {
      const result = await this.escalation.run({
        message,
        userId,
        convId,
        memoryContext: memory.prompt_text,
        skillPrompt: "",
        entities: intent.entities,
        allowKnowledge: false,
        intent: intent.intent,
        urgency: intent.urgency,
        workflowContext: "升级节点不执行业务 Agent 工作流",
      }, sink);
      result.response = finalizeMemoryReply(result.response, memoryOperation);
      const escalatedResult: ChatResponse = {
        conv_id: convId,
        response: result.response,
        intent: intent.intent,
        intent_group: intent.intent_group,
        agent_type: "escalation",
        agent_types: ["escalation"],
        primary_agent: "escalation",
        supporting_agents: [],
        routing_reason: routing.reason,
        routing_confidence: routing.confidence,
        escalated: true,
        latency_ms: Number((performance.now() - startedAt).toFixed(1)),
        knowledge_used: false,
        tools_used: [],
        output_valid: true,
        output_errors: [],
        entities: intent.entities,
        intent_confidence: Number(intent.confidence.toFixed(4)),
        intent_source_scores: intent.source_scores,
        request_id: requestId,
        memory_status: memoryStatus,
        memory_operation: memoryOperation,
      };
      this.recordToolTrace({
        request_id: requestId,
        timestamp: new Date().toISOString(),
        user_id: userId,
        conv_id: convId,
        message,
        response: result.response,
        intent: intent.intent,
        intent_group: intent.intent_group,
        primary_agent: "escalation",
        supporting_agents: [],
        agent_types: ["escalation"],
        tools_used: [],
        tool_calls: [],
        skills_used: [],
        escalated: true,
        latency_ms: escalatedResult.latency_ms,
      });
      return escalatedResult;
    }
    const primaryExecution: AgentType = routing.primaryAgent;
    const targets = [primaryExecution, ...routing.supportingAgents.filter((agent) => agent !== primaryExecution)];
    // 5) 并行执行：每个 target 组装 AgentRunInput{message, convId, memory.prompt_text, skillPrompt, entities, allowKnowledge} -> AgentRunResult。
    const executionController = new AbortController();
    const executionSignal = AbortSignal.any([signal, executionController.signal]);
    const pending = targets.map((agentType) => this.runWithFallback(
      agentType,
      message,
      userId,
      convId,
      memory,
      intent.entities,
      // 共享知识工具始终暴露给业务 Agent；模型自行决定是否调用。
      shouldUseKnowledge(intent.intent),
      intent.intent,
      intent.urgency,
      sink,
      executionSignal,
      memoryOperation,
    ).catch(error => { executionController.abort(error); throw error; }));
    const settled = await Promise.allSettled(pending);
    const failure = settled.find((item): item is PromiseRejectedResult => item.status === "rejected");
    if (failure) throw executionSignal.reason ?? failure.reason;
    const results = settled.map(item => (item as PromiseFulfilledResult<AgentRunResult>).value);

    // 6) 合并结果：单 Agent 直接返回；多 Agent 按“主处理/辅助处理”标签拼接最终回复。
    signal.throwIfAborted();
    const composed = await this.composer.compose(message, primaryExecution, results, memoryOperation, signal);
    const response = finalizeMemoryReply(composed, memoryOperation);
    // 7) 将多个 AgentRunResult 汇总为本次请求的统一状态：
    //    AgentRunResult[]
    //      -> 汇总知识库使用状态（任一 Agent 使用有效知识即为 true）
    //      -> 合并并去重成功使用的工具名称
    //      -> 合并全部工具调用轨迹（保留同一工具的多次调用）
    //      -> 合并全部输出契约校验错误
    //      -> 判断所有 Agent 输出是否有效（任一 outputValid=false 即为 false）
    //    escalated 则在汇总后根据意图、紧急度和最终回复文本单独判断。
    const knowledgeUsed = results.some((result) => result.knowledgeUsed);
    const toolsUsed = [...new Set(results.flatMap((result) => result.toolsUsed ?? []))];
    const skillsUsed = [...new Set(results.flatMap((result) => result.skillsUsed ?? []))];
    const toolCalls = results.flatMap((result) => result.toolTraces ?? []);
    const outputErrors = results.flatMap((result) => result.outputErrors ?? []);
    const outputValid = results.every((result) => result.outputValid !== false);
    const escalated = needsEscalation(intent.intent, intent.urgency, response);
    // 8) 写回记忆：只写入最终合并回复（user_id + conv_id + message + response），不单独写辅助 Agent 原始响应。

    const actualAgentTypes = [...new Set(results.map((result) => result.agentType))];
    // 9) 组装 ChatResponse：primary_agent/supporting_agents 表示计划路由；agent_type/agent_types 表示实际成功返回的 Agent。
      const result: ChatResponse = {
      conv_id: convId,
      response,
      intent: intent.intent,
      intent_group: intent.intent_group,
      agent_type: results[0]?.agentType ?? "general",
      agent_types: actualAgentTypes.length ? actualAgentTypes : targets,
      primary_agent: routing.primaryAgent,
      supporting_agents: routing.supportingAgents,
      routing_reason: routing.reason,
      routing_confidence: routing.confidence,
      escalated,
      latency_ms: Number((performance.now() - startedAt).toFixed(1)),
      knowledge_used: knowledgeUsed,
      tools_used: toolsUsed,
      output_valid: outputValid,
      output_errors: outputErrors,
      entities: intent.entities,
      intent_confidence: Number(intent.confidence.toFixed(4)),
      intent_source_scores: intent.source_scores,
      request_id: requestId,
      memory_status: memoryStatus,
      memory_operation: memoryOperation,
    };
    this.recordToolTrace({
      request_id: requestId,
      timestamp: new Date().toISOString(),
      user_id: userId,
      conv_id: convId,
      message,
      response,
      intent: intent.intent,
      intent_group: intent.intent_group,
      primary_agent: routing.primaryAgent,
      supporting_agents: routing.supportingAgents,
      agent_types: actualAgentTypes.length ? actualAgentTypes : targets,
      tools_used: toolsUsed,
      skills_used: skillsUsed,
      fallback_used: results.some((item) => item.fallbackUsed),
      tool_calls: toolCalls,
      escalated,
      latency_ms: result.latency_ms,
      output_valid: outputValid,
      output_errors: outputErrors,
    });
    return result;
  }

  private recordToolTrace(trace: RequestTrace): void {
    const max = this.appConfig.toolTraceMax ?? 200;
    this.recentToolTraces.push({ ...trace, model_id: this.appConfig.model.id, runtime_version: "pi-agent-core@0.82.1" });
    if (this.recentToolTraces.length > max) this.recentToolTraces.splice(0, this.recentToolTraces.length - max);
  }

  private monitorDegradedAgents(message: string, intent: IntentDecision): ReadonlySet<AgentType> {
    const scores = domainScores(message, intent);
    const degraded = new Set<AgentType>();
    for (const agentType of ["technical", "billing"] as const) {
      if (scores[agentType] > 0 && this.isMonitorDegraded(agentType)) degraded.add(agentType);
    }
    return degraded;
  }

  private isMonitorDegraded(agentType: AgentType): boolean {
    if (agentType === "general") return false;
    const pool = this.agentPools[agentType];
    if (!pool?.length) return false;
    // 只有同类实例全部达到阈值才回退；仍有一个健康实例时继续路由到该类型。
    const threshold = this.appConfig.monitorFallbackPenalty ?? 0.5;
    return Math.min(...pool.map((agent) => agent.stats.snapshot().monitor_penalty)) >= threshold;
  }

  getToolTrace(requestId: string): RequestTrace | undefined {
    return [...this.recentToolTraces].reverse().find((trace) => trace.request_id === requestId);
  }

  getRecentToolTraces(limit = 20): RequestTrace[] {
    const safeLimit = Math.max(1, Math.min(Number(limit) || 20, this.recentToolTraces.length || 1));
    return [...this.recentToolTraces].reverse().slice(0, safeLimit);
  }

  private async runWithFallback(
    agentType: AgentType,
    message: string,
    userId: string,
    convId: string,
    memorySnapshot: MemoryContext,
    entities: Record<string, string[]>,
    allowKnowledge: boolean,
    intent?: string,
    urgency?: string,
    sink?: ChatEventSink,
    signal?: AbortSignal,
    memoryOperation?: MemoryOperationReceipt,
  ): Promise<AgentRunResult> {
    /**
     * 输入：一个计划执行的 Agent 类型，以及本轮 AgentRunInput 所需的上下文。
     *
     * 这里将多个上游对象合并为 executor 的输入：
     * message + convId + memoryContext + entities + allowKnowledge + skillPrompt
     *
     * 输出：AgentRunResult，至少包含 agentType、response、knowledgeUsed、latencyMs。
     * 专业 Agent 失败时使用同一输入降级到 general；两者都失败时返回固定文案。
     */
    const input = {
      message,
      userId,
      convId,
      memoryContext: memorySnapshot.prompt_text,
      memorySnapshot,
      memoryOperation,
      entities,
      allowKnowledge,
      intent,
      urgency,
      signal,
      workflowContext: runAgentWorkflow(agentType, message, entities).context,
      skillPrompt: this.skills.promptFor(message, agentType),
    };
    const skillsUsed = this.skills.matchedNamesFor?.(message, agentType) ?? [];
    try {
      return { ...await this.bestAgent(agentType).run(input, sink), skillsUsed };
    } catch (error) {
      signal?.throwIfAborted();
      if (error instanceof ModelBudgetError) throw error;
      const failure = error instanceof AgentExecutionError ? error : undefined;
      const failureMessage = error instanceof Error ? error.message : String(error);
      const failedTools = failure?.toolsUsed ?? [];
      const failedTraces = (failure?.toolTraces ?? []).map((trace) => ({
        ...trace,
        ...(agentType !== "general" ? { fallback_used: true } : {}),
      }));
      console.error(`${agentType} Agent 失败:`, failureMessage);
      if (agentType !== "general") {
        const fallbackSkills = this.skills.matchedNamesFor?.(message, "general") ?? [];
        try {
          const fallback = await this.bestAgent("general").run({
            ...input,
            skillPrompt: this.skills.promptFor(message, "general"),
          }, sink);
          return {
            ...fallback,
            fallbackUsed: true,
            skillsUsed: [...new Set([...skillsUsed, ...fallbackSkills])],
            knowledgeUsed: Boolean(failure?.knowledgeUsed || fallback.knowledgeUsed),
            toolsUsed: [...new Set([...failedTools, ...fallback.toolsUsed])],
            toolTraces: [
              ...failedTraces,
              ...fallback.toolTraces.map((trace) => ({ ...trace, fallback_used: true })),
            ],
          };
        } catch (fallbackError) {
          signal?.throwIfAborted();
          if (fallbackError instanceof ModelBudgetError) throw fallbackError;
          const fallbackFailure = fallbackError instanceof AgentExecutionError ? fallbackError : undefined;
          const fallbackMessage = fallbackError instanceof Error ? fallbackError.message : String(fallbackError);
          console.error("General Agent 降级也失败:", fallbackMessage);
          return {
            agentType: "general",
            fallbackUsed: true,
            skillsUsed: [...new Set([...skillsUsed, ...fallbackSkills])],
            response: "抱歉，智能客服暂时无法完成这次请求，已建议转人工客服，请稍后重试。",
            knowledgeUsed: Boolean(failure?.knowledgeUsed || fallbackFailure?.knowledgeUsed),
            toolsUsed: [...new Set([...failedTools, ...(fallbackFailure?.toolsUsed ?? [])])],
            toolTraces: [
              ...failedTraces,
              ...(fallbackFailure?.toolTraces ?? []).map((trace) => ({ ...trace, fallback_used: true })),
            ],
            outputValid: false,
            outputErrors: [failureMessage, fallbackMessage],
            latencyMs: 0,
          };
        }
      }
      return {
        agentType: "general",
        fallbackUsed: true,
        skillsUsed,
        response: "抱歉，智能客服暂时无法完成这次请求，已建议转人工客服，请稍后重试。",
        knowledgeUsed: Boolean(failure?.knowledgeUsed),
        toolsUsed: failedTools,
        toolTraces: failedTraces,
        outputValid: false,
        outputErrors: [failureMessage],
        latencyMs: 0,
      };
    }
  }

  private bestAgent(agentType: AgentType): PiAgentExecutor {
    const pool = this.agentPools[agentType];
    return pool.reduce((best, candidate) => candidate.stats.snapshot().routing_score > best.stats.snapshot().routing_score ? candidate : best);
  }

  getStats() {
    const stats = Object.fromEntries(
      (Object.entries(this.agentPools) as Array<[AgentType, PiAgentExecutor[]]>).flatMap(([type, agents]) =>
        agents.map((agent, index) => [`${type}_${index}`, {
          ...agent.stats.snapshot(),
          role: agent.profile.role,
          workflow: agent.profile.workflow,
          input_contract: agent.profile.input_contract,
          output_contract: agent.profile.output_contract,
          output_fields: agent.profile.output_fields,
          handoff_conditions: agent.profile.handoff_conditions,
          tool_scope: agent.profile.tool_scope,
          available_tools: agent.availableToolNames(),
        }])),
    );
    return {
      ...stats,
      escalation_0: {
        ...this.escalation.stats.snapshot(),
        role: this.escalation.profile.role,
        workflow: this.escalation.profile.workflow,
        input_contract: this.escalation.profile.input_contract,
        output_contract: this.escalation.profile.output_contract,
        output_fields: this.escalation.profile.output_fields,
        handoff_conditions: this.escalation.profile.handoff_conditions,
        tool_scope: this.escalation.profile.tool_scope,
      },
    };
  }

  updateRoutingPenalties(penalties: Record<string, number>): void {
    for (const [agentType, agents] of Object.entries(this.agentPools) as Array<[AgentType, PiAgentExecutor[]]>) {
      agents.forEach((agent, index) => agent.stats.setMonitorPenalty(penalties[`${agentType}_${index}`] ?? 0));
    }
    this.escalation.stats.setMonitorPenalty(penalties.escalation_0 ?? 0);
  }

}
