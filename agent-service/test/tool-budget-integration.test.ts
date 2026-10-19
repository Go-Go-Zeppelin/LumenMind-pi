import type { AgentTool } from "@earendil-works/pi-agent-core";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PiAgentExecutor } from "../src/agents/executor.js";
import { config } from "../src/config.js";
import { unicodeLength } from "../src/agents/model-budget.js";

const scenario = vi.hoisted(() => ({ toolName: "knowledge_search", visibleContent: [] as unknown[] }));
vi.mock("@earendil-works/pi-agent-core", () => ({
  Agent: class {
    state = { messages: [] as unknown[] };
    tools: AgentTool[];
    subscriber?: (event: unknown) => Promise<void>;
    constructor(options: { initialState: { tools: AgentTool[] } }) { this.tools = options.initialState.tools; }
    subscribe(fn: (event: unknown) => Promise<void>) { this.subscriber = fn; }
    async prompt() {
      const tool = this.tools.find(item => item.name === scenario.toolName)!;
      await this.subscriber?.({ type: "tool_execution_start", toolName: tool.name, toolCallId: "call-budget", args: {} });
      let result: { content: unknown[]; details?: unknown };
      let isError = false;
      try { result = await tool.execute("call-budget", {}); }
      catch (error) { isError = true; result = { content: [{ type: "text", text: (error as Error).message }] }; }
      scenario.visibleContent = result.content;
      await this.subscriber?.({ type: "tool_execution_end", toolName: tool.name, toolCallId: "call-budget", result, isError });
      this.state.messages.push({ role: "assistant", stopReason: "stop", content: [{ type: "text", text: JSON.stringify({
        response: "根据查询结果继续排查", steps: ["检查凭证"], required_info: [], escalate: false,
      }) }] });
    }
  },
}));

const input = { message: "登录报错401", userId: "u", convId: "c", memoryContext: "", skillPrompt: "", entities: {}, allowKnowledge: true };
beforeEach(() => { scenario.toolName = "knowledge_search"; scenario.visibleContent = []; });

describe("bounded model tool output with complete application evidence", () => {
  it("limits model text while retaining original knowledge details and trace marker", async () => {
    const details = { query: "401", results: [{ title: "登录帮助", content: "凭证检查".repeat(3_000), score: 0.9, fallback: false }], reranked: false };
    const knowledge = { name: "knowledge_search", execute: vi.fn().mockResolvedValue({ content: [{ type: "text", text: JSON.stringify(details.results) }], details }) } as unknown as AgentTool;
    const executor = new PiAgentExecutor("technical", { ...config, apiKey: "fake-no-network", toolResultMaxChars: 1_500 }, knowledge);
    const result = await executor.run(input);
    expect(unicodeLength(JSON.stringify(scenario.visibleContent))).toBeLessThanOrEqual(1_500);
    const visible = JSON.parse((scenario.visibleContent[0] as { text: string }).text);
    expect(visible.context_truncated).toBe(true);
    expect(visible.data[0].title).toBe("登录帮助");
    expect(visible.data[0].content).toContain("[truncated]");
    expect(result.toolTraces[0]).toMatchObject({ model_output_truncated: true, success: true, is_error: false, tool_name: "knowledge_search" });
    expect(result.toolTraces[0]!.output).toBe(details);
    expect(details.results[0]!.content).toHaveLength(12_000);
    expect(result.knowledgeUsed).toBe(true);
  });

  it("reports a tool error when required business fields cannot fit", async () => {
    scenario.toolName = "read_record";
    const data = { order_id: "order".repeat(4_000), status: "paid", amount_cents: 1200 };
    const reader = { name: "read_record", execute: vi.fn().mockResolvedValue({ content: [{ type: "text", text: JSON.stringify(data) }], details: data }) } as unknown as AgentTool;
    const knowledge = { name: "knowledge_search" } as AgentTool;
    const executor = new PiAgentExecutor("technical", { ...config, apiKey: "fake-no-network", toolResultMaxChars: 1_000 }, knowledge, undefined, [reader]);
    const result = await executor.run({ ...input, allowKnowledge: false });
    expect(result.toolTraces[0]).toMatchObject({ success: false, is_error: true, tool_name: "read_record" });
    expect(result.toolTraces[0]?.error).toContain("preserving required fields");
    expect(result.toolsUsed).toEqual([]);
    expect(result.knowledgeUsed).toBe(false);
  });
});
