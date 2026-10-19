import { Agent, type AgentTool } from "@earendil-works/pi-agent-core";
import type { AssistantMessage, Model } from "@earendil-works/pi-ai";
import { streamSimple as streamAnthropicSimple } from "@earendil-works/pi-ai/api/anthropic-messages";
import type { AppConfig } from "../config.js";
import type { AgentType, ChatEventSink, KnowledgeSearchResponse, ToolCallTrace, MemoryContext, MemoryOperationReceipt } from "../types.js";
import { buildMemoryMessages } from "./memory-context.js";
import { assertModelBudget, boundToolResult, ModelBudgetError } from "./model-budget.js";
import { buildSystemPrompt } from "./prompts.js";
import { AGENT_PROFILES, type AgentProfile } from "./profiles.js";
import { AgentStats } from "./stats.js";
import { InfrastructureClient } from "../clients/infrastructure.js";
import { businessToolNames, createBusinessTools } from "../tools/business-tools.js";

/**
 * 编排器传给单个 Pi Agent 的输入对象。
 *
 * 数据来源：
 * - message：当前用户消息
 * - userId：编排器标准化后的身份，只由本次业务工具闭包使用
 * - convId：当前会话 ID，用作 Pi sessionId
 * - memoryContext：Python /memory/context 返回的 prompt_text
 * - skillPrompt：SkillManager 根据消息和 Agent 类型匹配出的提示词
 * - entities：IntentRouter 提取的订单号、金额、错误码等
 * - allowKnowledge：是否向本次 Agent 暴露 knowledge_search 工具
 */
export interface AgentRunInput {
  message: string;
  userId: string;
  convId: string;
  memoryContext: string;
  memorySnapshot?: MemoryContext;
  memoryOperation?: MemoryOperationReceipt;
  skillPrompt: string;
  entities: Record<string, string[]>;
  allowKnowledge: boolean;
  intent?: string;
  urgency?: string;
  workflowContext?: string;
  signal?: AbortSignal;
}

/**
 * 单个 Pi Agent 执行结束后的输出对象，交回 Orchestrator 合并。
 * knowledgeUsed 表示工具实际成功返回过非 fallback 知识，不只是工具已被暴露。
 */
export interface AgentRunResult {
  agentType: AgentType | "escalation";
  response: string;
  knowledgeUsed: boolean;
  toolsUsed: string[];
  toolTraces: ToolCallTrace[];
  skillsUsed?: string[];
  fallbackUsed?: boolean;
  escalated?: boolean;
  outputValid?: boolean;
  outputErrors?: string[];
  latencyMs: number;
}

/**
 * Agent 在工具循环中失败时携带已经发生的请求级状态。
 *
 * 轨迹属于一次 run，而不是 PiAgentExecutor 实例；这样同一个执行器并发
 * 处理多个会话时，失败请求也不会把工具调用信息丢给编排器。
 */
export class AgentExecutionError extends Error {
  readonly name = "AgentExecutionError";

  constructor(
    message: string,
    readonly toolsUsed: string[],
    readonly toolTraces: ToolCallTrace[],
    readonly knowledgeUsed: boolean,
  ) {
    super(message);
  }
}

/** 从 AssistantMessage 的 content 数组中只提取文本片段，拼成最终回复。 */
function finalText(message: AssistantMessage): string {
  return message.content
    .filter((part): part is Extract<(typeof message.content)[number], { type: "text" }> => part.type === "text")
    .map((part) => part.text)
    .join("")
    .trim();
}

function parseContract(raw: string, fields: string[]): { response: string; valid: boolean; errors: string[]; escalate: boolean } {
  const start = raw.indexOf("{");
  const end = raw.lastIndexOf("}");
  if (start < 0 || end <= start) return { response: raw.trim(), valid: false, errors: ["模型未返回 JSON 对象"], escalate: false };
  try {
    const value = JSON.parse(raw.slice(start, end + 1)) as Record<string, unknown>;
    const errors: string[] = [];
    if (typeof value.response !== "string" || !value.response.trim()) errors.push("response 必须是非空字符串");
    if (fields.includes("steps") && (!Array.isArray(value.steps) || value.steps.some((item) => typeof item !== "string"))) errors.push("steps 必须是字符串数组");
    if (fields.includes("required_info") && (!Array.isArray(value.required_info) || value.required_info.some((item) => typeof item !== "string"))) errors.push("required_info 必须是字符串数组");
    if (typeof value.escalate !== "boolean") errors.push("escalate 必须是布尔值");
    return {
      response: typeof value.response === "string" ? value.response.trim() : raw.trim(),
      valid: errors.length === 0,
      errors,
      escalate: value.escalate === true,
    };
  } catch {
    return { response: raw.trim(), valid: false, errors: ["模型返回的 JSON 无法解析"], escalate: false };
  }
}

export class PiAgentExecutor {
  // 记录此类 Agent 的调用次数、成功率和耗时，供 /health 展示。
  readonly stats = new AgentStats();

  constructor(
    // 当前执行器固定对应一种角色：general、technical 或 billing。
    readonly agentType: AgentType,
    private readonly appConfig: AppConfig,
    private readonly knowledgeTool: AgentTool,
    readonly profile: AgentProfile = AGENT_PROFILES[agentType],
    private readonly roleTools: AgentTool[] = [],
    private readonly infrastructure: InfrastructureClient = new InfrastructureClient(appConfig.infrastructureUrl, appConfig.infrastructureTimeoutMs),
  ) { }

  /**
   * 执行一次独立的 Pi Agent 运行。
   *
   * 输入：AgentRunInput，以及 /chat/stream 使用的可选事件接收器 sink。
   * 输出：Promise<AgentRunResult>。
   *
   * 完整数据流：
      AgentRun NzInput
    → 构建 system prompt
    → 创建 Pi Agent
    → 挂载知识库工具和角色工具
    → 订阅文本/工具事件
    → 提交用户消息
    → 获取最后一条 AssistantMessage
    → 校验 JSON 输出契约
    → 必要时让模型修复一次
    → 返回 AgentRunResult
   *
   * 每次 run() 都创建新 Agent 且 messages=[]。sessionId=convId 只是会话标识，
   * 不表示 Pi 自动保留跨请求历史；跨请求上下文来自 input.memoryContext。
   */
  async run(input: AgentRunInput, sink?: ChatEventSink): Promise<AgentRunResult> {
    const startedAt = performance.now();
    let knowledgeUsed = false;
    const toolsUsed = new Set<string>();
    const toolTraces: ToolCallTrace[] = [];
    const truncatedTools = new Set<string>();
    const startedTools = new Map<string, { startedAt: number; trace: ToolCallTrace }>();
    let ok = false;
    let budgetFailure: ModelBudgetError | undefined;

    try {
      if (!this.appConfig.apiKey) throw new Error("未配置 PI_API_KEY 或 ANTHROPIC_API_KEY");

      // 将 AgentRunInput 转换为 Pi Agent 初始状态。
      const agent = new Agent({
        initialState: {
          // 可信角色规则进入 system；记忆和工作流作为 user 数据消息，
          // 防止历史原文获得 system 级指令权限。
          systemPrompt: buildSystemPrompt(this.agentType, "", input.skillPrompt, input.entities, this.profile, input.workflowContext ?? ""),
          model: this.appConfig.model as Model<any>,
          thinkingLevel: this.appConfig.thinkingLevel,
          // allowKnowledge=true 仅表示暴露工具；是否调用由模型在运行时决定。
          tools: [
            ...(input.allowKnowledge ? [this.knowledgeTool] : []),
            ...this.roleTools,
            ...(this.agentType === "technical" ? [] : createBusinessTools(this.infrastructure, input.userId)),
          ].map((tool): AgentTool => ({ ...tool, execute: async (id, params, signal, onUpdate) => {
            const original = await tool.execute(id, params, signal, onUpdate);
            const bounded = boundToolResult(original, this.appConfig.toolResultMaxChars);
            if (bounded !== original) truncatedTools.add(id);
            return bounded;
          } })),
          // 每次执行从空消息列表开始，当前用户消息稍后由 agent.prompt() 加入。
          messages: buildMemoryMessages(input.memorySnapshot ?? {
            prompt_text: input.memoryContext, recent_messages: [], relevant_history: [], user_profile: {}, summary: "",
          }, this.appConfig.model, input.memoryOperation),
        },
        sessionId: input.convId,
        // 若模型在一次决策中产生多个工具调用，允许它们并行执行。
        toolExecution: "parallel",
        // Pi 最终通过 Anthropic Messages API 流式调用所配置的模型。
        streamFn: (model, context, options) => {
          const maxTokens = this.profile.max_tokens || this.appConfig.maxTokens;
          try { assertModelBudget(model, context, maxTokens); }
          catch (error) { if (error instanceof ModelBudgetError) budgetFailure = error; throw error; }
          return streamAnthropicSimple(model as Model<"anthropic-messages">, context, {
            ...options,
            signal: input.signal && options?.signal ? AbortSignal.any([input.signal, options.signal]) : input.signal ?? options?.signal,
            apiKey: this.appConfig.apiKey, maxTokens, temperature: this.profile.temperature,
          });
        },
      });

      // 将 Pi 内部事件转换为 LumenMind 的 ChatStreamEvent，再交给 SSE 接口发送。
      // 普通 /chat 没有传 sink，因此仍会执行订阅逻辑，但不会向客户端发送事件。
      agent.subscribe(async (event) => {
        if (event.type === "agent_start") {
          await sink?.({ type: "agent_start", agent_type: this.agentType });
        } else if (event.type === "message_update" && event.assistantMessageEvent.type === "text_delta") {
          await sink?.({ type: "text_delta", agent_type: this.agentType, delta: event.assistantMessageEvent.delta });
        } else if (event.type === "tool_execution_start") {
          const trace: ToolCallTrace = {
            agent_type: this.agentType,
            tool_name: event.toolName,
            tool_call_id: event.toolCallId,
            tool_use_id: event.toolCallId,
            input: event.args && typeof event.args === "object" ? event.args as Record<string, unknown> : {},
            started_at: new Date().toISOString(),
            success: false,
            is_error: false,
            latency_ms: 0,
            cached: false,
            reranked: false,
          };
          startedTools.set(event.toolCallId, { startedAt: performance.now(), trace });
          await sink?.({ type: "tool_start", agent_type: this.agentType, tool: event.toolName });
        } else if (event.type === "tool_execution_end") {
          const pending = startedTools.get(event.toolCallId);
          const details = event.result?.details as KnowledgeSearchResponse | undefined;
          const trace: ToolCallTrace = pending?.trace ?? {
            agent_type: this.agentType,
            tool_name: event.toolName,
            tool_call_id: event.toolCallId,
            tool_use_id: event.toolCallId,
            input: {},
            success: false,
            is_error: event.isError,
            latency_ms: 0,
            cached: false,
            reranked: false,
          };
          trace.success = !event.isError;
          trace.is_error = event.isError;
          trace.finished_at = new Date().toISOString();
          trace.model_output_truncated = truncatedTools.has(event.toolCallId);
          trace.output = event.result?.details ?? event.result?.content ?? null;
          trace.latency_ms = Number((performance.now() - (pending?.startedAt ?? performance.now())).toFixed(1));
          trace.cached = Boolean(details?.cached);
          trace.reranked = Boolean(details?.reranked);
          if (event.isError) {
            const errorResult = event.result as { errorMessage?: unknown; error?: unknown } | undefined;
            trace.error = typeof errorResult?.errorMessage === "string"
              ? errorResult.errorMessage
              : typeof errorResult?.error === "string" ? errorResult.error
                : event.result?.content?.filter((part: { type: string }) => part.type === "text")
                  .map((part: { text?: string }) => part.text ?? "").join("\n") || "工具执行失败";
          }
          toolTraces.push(trace);
          startedTools.delete(event.toolCallId);
          if (!event.isError) toolsUsed.add(event.toolName);
          if (event.toolName === "knowledge_search" && !event.isError) {
            // 工具执行成功还不够：至少一条结果不能是 Python 的 fallback 降级文档，
            // knowledgeUsed 才会变成 true。
            knowledgeUsed ||= Boolean(details?.results?.some((item) => !item.fallback));
          }
          await sink?.({ type: "tool_end", agent_type: this.agentType, tool: event.toolName, is_error: event.isError });
        }
      });

      // 当前用户消息是 Agent 的业务输入；此调用会驱动模型生成和可选工具循环，
      // 直到产生最终 AssistantMessage 或异常结束。
      input.signal?.throwIfAborted();
      await agent.prompt(input.message);
      if (budgetFailure) throw budgetFailure;
      input.signal?.throwIfAborted();

      // state.messages 可能包含 user、assistant、toolResult 等消息；这里只保留 assistant。
      const assistantMessages = agent.state.messages.filter(
        (message): message is AssistantMessage => message.role === "assistant",
      );
      const last = assistantMessages.at(-1);
      // 没有 assistant 消息、异常停止或最终文本为空，都算本次 Agent 执行失败，
      // 异常会交给 Orchestrator.runWithFallback() 处理。
      if (!last) throw new Error(agent.state.errorMessage ?? "Pi Agent 未返回 assistant 消息");
      if (last.stopReason === "error" || last.stopReason === "aborted") {
        throw new Error(last.errorMessage ?? `Pi Agent 异常结束: ${last.stopReason}`);
      }
      let response = finalText(last);
      if (!response) throw new Error("Pi Agent 返回了空文本");
      let contract = parseContract(response, this.profile.output_fields);
      // 一次修复回合：把输出契约从“提示建议”变成运行时校验，
      // 不符合 JSON schema 时要求同一个 Agent 重新结构化输出。
      if (!contract.valid) {
        await agent.prompt(`上一条输出不符合 JSON 输出契约，请只输出修正后的 JSON。错误：${contract.errors.join("；")}。原输出：${response}`);
        if (budgetFailure) throw budgetFailure;
        const repaired = agent.state.messages
          .filter((item): item is AssistantMessage => item.role === "assistant")
          .at(-1);
        if (repaired) {
          response = finalText(repaired);
          contract = parseContract(response, this.profile.output_fields);
        }
      }
      ok = true;
      // 输出给 Orchestrator：实际 Agent 类型、最终文本、知识使用状态和本次耗时。
      return {
        agentType: this.agentType,
        response: contract.response,
        knowledgeUsed,
        toolsUsed: [...toolsUsed],
        toolTraces,
        latencyMs: performance.now() - startedAt,
        outputValid: contract.valid,
        outputErrors: contract.errors,
        escalated: contract.escalate,
      };
    } catch (error) {
      // 如果流式调用在工具结束事件之前中断，也把已开始的调用保留下来，
      // 便于请求 trace 说明失败发生在哪个工具和参数上。
      const message = error instanceof Error ? error.message : String(error);
      for (const pending of startedTools.values()) {
        pending.trace.success = false;
        pending.trace.is_error = true;
        pending.trace.error = message;
        pending.trace.finished_at = new Date().toISOString();
        pending.trace.latency_ms = Number((performance.now() - pending.startedAt).toFixed(1));
        toolTraces.push(pending.trace);
      }
      startedTools.clear();
      if (budgetFailure) throw budgetFailure;
      if (error instanceof ModelBudgetError) throw error;
      if (error instanceof AgentExecutionError) throw error;
      throw new AgentExecutionError(message, [...toolsUsed], [...toolTraces], knowledgeUsed);
    } finally {
      // 成功或抛出异常都会记录统计；finally 不改变原返回值或异常。
      this.stats.record(ok, performance.now() - startedAt);
    }
  }

  availableToolNames(): string[] {
    return [this.knowledgeTool.name, ...this.roleTools.map((tool) => tool.name), ...businessToolNames(this.agentType)];
  }
}
