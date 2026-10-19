import type { AgentTool } from "@earendil-works/pi-agent-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { InfrastructureClient } from "../src/clients/infrastructure.js";
import { PiAgentExecutor, type AgentRunInput } from "../src/agents/executor.js";
import { LumenMindOrchestrator } from "../src/agents/orchestrator.js";
import { AGENT_PROFILES } from "../src/agents/profiles.js";
import { roleTools } from "../src/tools/agent-tools.js";
import { BUSINESS_TOOL_NAMES } from "../src/tools/business-tools.js";
import { config } from "../src/config.js";

const scenario = vi.hoisted(() => ({ failBilling: false, instances: [] as string[][] }));
vi.mock("@earendil-works/pi-agent-core", () => ({
  Agent: class {
    state = { messages: [] as unknown[] };
    tools: AgentTool[];
    private subscriber?: (event: unknown) => Promise<void>;
    constructor(options: { initialState: { tools: AgentTool[] } }) {
      this.tools = options.initialState.tools;
      scenario.instances.push(this.tools.map(t => t.name));
    }
    subscribe(fn: (event: unknown) => Promise<void>) { this.subscriber = fn; }
    async prompt(message: string) {
      const name = message.includes("账单") ? "list_bills" : "list_orders";
      const tool = this.tools.find(t => t.name === name);
      if (tool) {
        await this.subscriber?.({ type: "tool_execution_start", toolName: name, toolCallId: "query-1", args: {} });
        let result: unknown;
        let isError = false;
        try { result = await tool.execute("query-1", {}); }
        catch (error) {
          isError = true;
          result = { content: [{ type: "text", text: (error as Error).message }] };
        }
        await this.subscriber?.({ type: "tool_execution_end", toolName: name, toolCallId: "query-1", result, isError });
        if (scenario.failBilling && this.tools.some(t => t.name === "check_billing_fields")) throw new Error("billing interrupted");
      }
      this.state.messages.push({ role: "assistant", stopReason: "stop", content: [{ type: "text", text: JSON.stringify({
        response: "演示查询结果", steps: [], required_info: [], escalate: false,
      }) }] });
    }
  },
}));

const cfg = { ...config, apiKey: "test-key", useLlmRouting: false, useEmbeddingIntent: false };
const knowledge = { name: "knowledge_search" } as AgentTool;
const input: AgentRunInput = { message: "查一下我的订单", userId: "u1001", convId: "test", memoryContext: "", skillPrompt: "", entities: {}, allowKnowledge: false };

function infra() {
  const client = new InfrastructureClient("http://infra.test", 1000);
  vi.spyOn(client, "getMemoryContext").mockResolvedValue({ prompt_text: "", recent_messages: [], relevant_history: [], user_profile: {}, summary: "" });
  vi.spyOn(client, "commitTurn").mockResolvedValue();
  vi.spyOn(client, "beginTurn").mockImplementation(async (_u, c, r) => ({ status: "RECEIVED", conv_id: c || "generated", request_id: r, lease_token: "test-lease", turn_seq: 1, source_seq: 1 }));
  vi.spyOn(client, "finishTurn").mockImplementation(async (_u, c, r, _token, status, response) => ({ status, conv_id: c, request_id: r, response }));
  return client;
}

describe("执行器与编排器用户上下文及业务 trace", () => {
  beforeEach(() => {
    scenario.failBilling = false;
    scenario.instances = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      const user = new URL(url).searchParams.get("user_id");
      return new Response(JSON.stringify({ items: [{ user_id: user }], count: 1, source: "demo_business_db" }));
    }));
  });
  afterEach(() => vi.unstubAllGlobals());

  it("共享执行器每次 run 绑定独立用户，关闭知识检索仍能查询并留下返回 trace", async () => {
    const executor = new PiAgentExecutor("general", cfg, knowledge, AGENT_PROFILES.general, roleTools("general"), infra());
    const results = await Promise.all([executor.run(input), executor.run({ ...input, userId: "u1002", convId: "other" })]);
    expect(results.map(r => (r.toolTraces[0]!.output as { items: { user_id: string }[] }).items[0]!.user_id)).toEqual(["u1001", "u1002"]);
    for (const result of results) {
      expect(result.knowledgeUsed).toBe(false);
      expect(result.toolsUsed).toEqual(["list_orders"]);
      expect(result.toolTraces[0]).toMatchObject({ success: true, is_error: false });
    }
    expect(scenario.instances.every(names => !names.includes("knowledge_search") && names.includes("get_bill"))).toBe(true);
  });

  it("健康工具列表与角色白名单一致，Technical 不提供业务工具", async () => {
    for (const type of ["general", "billing", "technical"] as const) {
      const executor = new PiAgentExecutor(type, cfg, knowledge, AGENT_PROFILES[type], roleTools(type), infra());
      expect(executor.availableToolNames()).toEqual(AGENT_PROFILES[type].tool_scope);
      if (type === "technical") {
        await executor.run(input);
        expect(scenario.instances.at(-1)!.filter(n => BUSINESS_TOOL_NAMES.includes(n as never))).toEqual([]);
        expect(executor.availableToolNames().filter(n => BUSINESS_TOOL_NAMES.includes(n as never))).toEqual([]);
      }
    }
  });

  it("业务工具故障在 trace 中标记 is_error=true，不设置 knowledgeUsed", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "business_unavailable" }), { status: 503 })));
    const result = await new PiAgentExecutor("general", cfg, knowledge, AGENT_PROFILES.general, roleTools("general"), infra()).run(input);
    expect(result.toolTraces[0]).toMatchObject({ tool_name: "list_orders", is_error: true, success: false });
    expect(JSON.stringify(result.toolTraces[0]!.output)).toContain("business_unavailable");
    expect(result.knowledgeUsed).toBe(false);
  });

  it.each([false, true])("正常及 Billing 失败降级后均传递标准化 userId 并保留查询 trace：fallback=%s", async (fallback) => {
    scenario.failBilling = fallback;
    const client = infra();
    const skills = { promptFor: vi.fn().mockReturnValue(""), matchedNamesFor: vi.fn().mockReturnValue([]) };
    const orchestrator = new LumenMindOrchestrator(cfg, client, skills as never, knowledge);
    const result = await orchestrator.chat({ message: "查看我的账单", user_id: " u1002 ", conv_id: "billing-context" });
    expect(result.agent_type).toBe(fallback ? "general" : "billing");
    expect(result.tools_used).toEqual(["list_bills"]);
    expect(result.knowledge_used).toBe(false);
    const trace = orchestrator.getToolTrace(result.request_id)!;
    expect(trace.user_id).toBe("u1002");
    expect(trace.tool_calls).toHaveLength(fallback ? 2 : 1);
    expect(trace.fallback_used).toBe(fallback);
    for (const call of trace.tool_calls) {
      expect(call.output).toEqual({ items: [{ user_id: "u1002" }], count: 1, source: "demo_business_db" });
      expect(call.is_error).toBe(false);
      if (fallback) expect(call.fallback_used).toBe(true);
    }
    expect(client.getMemoryContext).toHaveBeenCalledWith("u1002", "billing-context", "查看我的账单", result.request_id);
    expect(client.finishTurn).toHaveBeenCalledWith("u1002", "billing-context", result.request_id, "test-lease", "COMPLETED", "演示查询结果");
    expect(client.commitTurn).not.toHaveBeenCalled();
  });

  it("监控将 Billing 降级到 General 时仍使用原用户身份", async () => {
    const client = infra();
    const orchestrator = new LumenMindOrchestrator(cfg, client, { promptFor: () => "" } as never, knowledge);
    orchestrator.updateRoutingPenalties({ billing_0: 1 });
    const result = await orchestrator.chat({ message: "查看我的账单", user_id: "u1002" });
    expect(result.primary_agent).toBe("general");
    expect(orchestrator.getToolTrace(result.request_id)!.tool_calls[0]!.output).toMatchObject({ items: [{ user_id: "u1002" }] });
  });
});
