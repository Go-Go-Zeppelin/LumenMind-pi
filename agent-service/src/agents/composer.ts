import { assertModelBudget } from "./model-budget.js";
import type { MemoryOperationReceipt } from "../types.js";
import { Agent } from "@earendil-works/pi-agent-core";
import type { AssistantMessage, Model } from "@earendil-works/pi-ai";
import { streamSimple as streamAnthropicSimple } from "@earendil-works/pi-ai/api/anthropic-messages";
import type { AppConfig } from "../config.js";
import type { AgentRunResult } from "./executor.js";

function textOf(message: AssistantMessage): string {
  return message.content
    .filter((part): part is Extract<(typeof message.content)[number], { type: "text" }> => part.type === "text")
    .map((part) => part.text)
    .join("")
    .trim();
}

/**
 * 多 Agent 回复合并器：以主 Agent 的结论为主，去重并处理不同 Agent 之间的冲突。
 * 它不负责意图识别、路由、知识检索或记忆写入，只负责生成最终用户回复。
 */
export class ResponseComposer {
  constructor(private readonly appConfig: AppConfig) {}

  /**
   * 输入：
   * - message：用户原始问题，用于让 Composer 理解各候选答案所回答的请求
   * - primaryAgent：路由选出的主 Agent 类型，用于确定合并时优先采用谁的结论
   * - results：主、辅助 Agent 的 AgentRunResult 数组；当前实际只读取每项的
   *   agentType 和 response，不使用 knowledgeUsed、toolsUsed、toolTraces、latencyMs 等字段
   *
   * 处理：先过滤 response 为空的结果。只有一个有效结果时直接返回其 response；
   * 有多个有效结果时，将用户问题、主 Agent 和各 Agent 回复交给一个无工具 Pi Agent
   * 合并。未配置 API Key、Composer 调用失败或没有生成文本时，使用确定性方式拼接。
   *
   * 输出：Promise<string>，内容是一条最终面向用户的自然语言回复。该字符串返回给
   * Orchestrator，随后作为 ChatResponse.response 返回客户端并写入 Python 对话记忆。
   */
  async compose(message: string, primaryAgent: string, results: AgentRunResult[], operation?: MemoryOperationReceipt, signal?: AbortSignal): Promise<string> {
    const successful = results.filter((result) => result.response.trim());
    if (successful.length <= 1) return successful[0]?.response ?? "抱歉，暂时没有可用的处理结果。";

    const evidence = successful.map((result) => `[${result.agentType} Agent 输出]\n${result.response}`).join("\n\n");
    if (!this.appConfig.apiKey) return this.deterministicMerge(successful);

    const agent = new Agent({
      initialState: {
        systemPrompt: [
          "你是 LumenMind 的 Response Composer，负责把多个专业 Agent 的结果合并成一条最终客服回复。",
          "以主 Agent 结论为主，去掉重复和冲突表述；不得编造订单、退款、后台查询结果。",
          "如果结论冲突，明确说明需要核验；保留必要的排查步骤、核验字段和升级边界。",
          "记忆操作只能按输入回执描述，PENDING/FAILED/NOOP不能写成已经记住。",
          "只输出给用户看的回复，不要提及 Agent、编排器或内部工具。",
        ].join("\n"),
        model: this.appConfig.model as Model<any>,
        thinkingLevel: this.appConfig.thinkingLevel,
        tools: [],
        messages: [],
      },
      streamFn: (model, context, options) => {
        assertModelBudget(model, context, this.appConfig.maxTokens);
        return streamAnthropicSimple(model as Model<"anthropic-messages">, context, {
          ...options, signal: signal && options?.signal ? AbortSignal.any([signal, options.signal]) : signal ?? options?.signal,
          apiKey: this.appConfig.apiKey, maxTokens: this.appConfig.maxTokens, temperature: 0.1,
        });
      },
    });

    try {
      signal?.throwIfAborted();
      await agent.prompt(`记忆操作回执：${JSON.stringify(operation ?? null)}\n主 Agent：${primaryAgent}\n用户问题：${message}\n候选结果：\n${evidence}`);
      const assistants = agent.state.messages.filter((item): item is AssistantMessage => item.role === "assistant");
      const response = assistants.at(-1);
      if (response) {
        const text = textOf(response);
        if (text) return text;
      }
    } catch {
      signal?.throwIfAborted();
      // Composer 失败时使用确定性合并，不能丢失专业 Agent 的结果。
    }
    return this.deterministicMerge(successful);
  }

  private deterministicMerge(results: AgentRunResult[], operation?: MemoryOperationReceipt, signal?: AbortSignal): string {
    return results.map((result, index) => index === 0 ? result.response : `补充说明：\n${result.response}`).join("\n\n");
  }
}
