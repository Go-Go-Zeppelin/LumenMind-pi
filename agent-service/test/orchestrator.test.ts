import type { AgentTool } from "@earendil-works/pi-agent-core";
import { describe, expect, it, vi } from "vitest";
import { LumenMindOrchestrator } from "../src/agents/orchestrator.js";
import { config } from "../src/config.js";
import type { AgentType, MemoryContext } from "../src/types.js";
import { AgentExecutionError } from "../src/agents/executor.js";

const emptyMemory: MemoryContext = {
  prompt_text: "",
  recent_messages: [],
  relevant_history: [],
  user_profile: {},
  summary: "",
};

function createHarness(overrides: Partial<typeof config> = {}) {
  const infrastructure = {
    getMemoryContext: vi.fn().mockResolvedValue(emptyMemory),
    commitTurn: vi.fn().mockResolvedValue(undefined),
  };
  const skills = { promptFor: vi.fn().mockReturnValue(""), matchedNamesFor: vi.fn().mockImplementation((_message, type) => [`skill-${type}`]) };
  const tool = { name: "knowledge_search" } as AgentTool;
  const orchestrator = new LumenMindOrchestrator(
    { ...config, apiKey: "", useLlmRouting: false, ...overrides },
    infrastructure as never,
    skills as never,
    tool,
  );
  const agents = (orchestrator as unknown as { agents: Record<AgentType, { run: ReturnType<typeof vi.fn> }> }).agents;
  for (const type of ["general", "technical", "billing"] as AgentType[]) {
    agents[type].run = vi.fn().mockResolvedValue({
      agentType: type,
      response: `${type}-ok`,
      knowledgeUsed: type === "technical",
      toolsUsed: type === "technical" ? ["knowledge_search"] : [],
      toolTraces: type === "technical" ? [{
        agent_type: type,
        tool_name: "knowledge_search",
        tool_call_id: `${type}-call-1`,
        input: { query: "test" },
        success: true,
        is_error: false,
        latency_ms: 1,
        cached: false,
        reranked: false,
      }] : [],
      latencyMs: 1,
    });
  }
  return { infrastructure, orchestrator, agents };
}

describe("LumenMind 结构化编排", () => {
  it("紧急请求使用独立升级节点，不调用业务 Agent", async () => {
    const { infrastructure, orchestrator, agents } = createHarness();
    const result = await orchestrator.chat({
      message: "紧急，请立刻转人工处理",
      user_id: "user-escalation",
      conv_id: "conv-escalation",
    });

    expect(result).toMatchObject({
      agent_type: "escalation",
      agent_types: ["escalation"],
      primary_agent: "escalation",
      escalated: true,
    });
    expect(agents.general.run).not.toHaveBeenCalled();
    expect(infrastructure.commitTurn).toHaveBeenCalledOnce();
  });

  it("按 routing_score 从同类 Agent 池选择实例", async () => {
    const infrastructure = {
      getMemoryContext: vi.fn().mockResolvedValue(emptyMemory),
      commitTurn: vi.fn().mockResolvedValue(undefined),
    };
    const skills = { promptFor: vi.fn().mockReturnValue("") };
    const orchestrator = new LumenMindOrchestrator(
      { ...config, apiKey: "", useLlmRouting: false, technicalReplicas: 2 },
      infrastructure as never,
      skills as never,
      { name: "knowledge_search" } as AgentTool,
    );
    const internals = orchestrator as unknown as {
      agents: Record<AgentType, { run: ReturnType<typeof vi.fn> }>;
      agentPools: Record<AgentType, Array<{ run: ReturnType<typeof vi.fn> }>>;
    };
    const selected = internals.agentPools.technical[1]!;
    selected.run = vi.fn().mockResolvedValue({
      agentType: "technical",
      response: "technical-pool-1",
      knowledgeUsed: false,
      toolsUsed: [],
      toolTraces: [],
      latencyMs: 1,
    });
    orchestrator.updateRoutingPenalties({ technical_0: 0.9, technical_1: 0 });
    const result = await orchestrator.chat({ message: "接口报错 500", user_id: "user-pool", conv_id: "conv-pool" });
    expect(result.response).toBe("technical-pool-1");
    expect(selected.run).toHaveBeenCalledOnce();
  });

  it("返回细粒度意图、实体和主辅助 Agent 元数据", async () => {
    const { infrastructure, orchestrator } = createHarness();
    const result = await orchestrator.chat({
      message: "登录报错 401，而且还被重复扣款 50 元",
      user_id: "user-1",
      conv_id: "conv-1",
    });

    expect(result).toMatchObject({
      intent: "technical_login",
      intent_group: "technical",
      agent_type: "technical",
      agent_types: ["technical", "billing"],
      primary_agent: "technical",
      supporting_agents: ["billing"],
      knowledge_used: true,
      entities: { amount: ["50 元"], error_code: ["401"] },
    });
    expect(result.routing_reason).toContain("primary=technical");
    expect(result.tools_used).toEqual(["knowledge_search"]);
    const trace = orchestrator.getToolTrace(result.request_id);
    expect(trace?.tool_calls[0]?.tool_name).toBe("knowledge_search");
    expect(trace?.message).toContain("登录报错");
    expect(infrastructure.commitTurn).toHaveBeenCalledOnce();
  });

  it("低置信度 OTHER 直接澄清并仍写入会话记忆", async () => {
    const { infrastructure, orchestrator, agents } = createHarness();
    const result = await orchestrator.chat({ message: "这个情况不太对", user_id: "user-2", conv_id: "conv-2" });

    expect(result).toMatchObject({
      intent: "other",
      agent_type: "general",
      primary_agent: "general",
      routing_reason: "低置信度 OTHER 意图，先澄清用户需求",
      knowledge_used: false,
    });
    expect(agents.general.run).not.toHaveBeenCalled();
    expect(infrastructure.commitTurn).toHaveBeenCalledOnce();
  });

  it("专业 Agent 失败时保留失败前的工具轨迹并标记 General 降级", async () => {
    const { orchestrator, agents } = createHarness();
    const trace = {
      agent_type: "technical" as const,
      tool_name: "lookup_error_code",
      tool_call_id: "technical-call-1",
      tool_use_id: "technical-call-1",
      input: { error_code: "401" },
      success: true,
      is_error: false,
      latency_ms: 2,
      cached: false,
      reranked: false,
    };
    agents.technical.run = vi.fn().mockRejectedValue(new AgentExecutionError(
      "模型服务中断",
      ["lookup_error_code"],
      [trace],
      false,
    ));
    agents.general.run = vi.fn().mockResolvedValue({
      agentType: "general",
      response: "general-fallback",
      knowledgeUsed: false,
      toolsUsed: [],
      toolTraces: [],
      latencyMs: 1,
    });

    const result = await orchestrator.chat({ message: "登录报错 401", user_id: "fallback", conv_id: "fallback-1" });

    expect(result.agent_type).toBe("general");
    expect(result.tools_used).toEqual(["lookup_error_code"]);
    expect(orchestrator.getToolTrace(result.request_id)?.fallback_used).toBe(true);
    expect(orchestrator.getToolTrace(result.request_id)?.skills_used).toEqual(["skill-technical", "skill-general"]);
    expect(orchestrator.getToolTrace(result.request_id)?.tool_calls[0]).toMatchObject({
      tool_name: "lookup_error_code",
      fallback_used: true,
    });
    expect(agents.general.run).toHaveBeenCalledOnce();
  });

  it("专业 Agent 全部达到监控降权阈值时回退到 General", async () => {
    const { orchestrator, agents } = createHarness({ monitorFallbackPenalty: 0.5 });
    orchestrator.updateRoutingPenalties({ technical_0: 0.5 });

    const result = await orchestrator.chat({ message: "登录报错 401", user_id: "monitor", conv_id: "monitor-1" });

    expect(result.primary_agent).toBe("general");
    expect(result.agent_type).toBe("general");
    expect(result.routing_reason).toContain("monitor_fallback=[technical]");
    expect(agents.technical.run).not.toHaveBeenCalled();
    expect(agents.general.run).toHaveBeenCalledOnce();
  });

  it("按 PI_TOOL_TRACE_MAX 保留最近请求轨迹", async () => {
    const { orchestrator } = createHarness({ toolTraceMax: 1 });
    await orchestrator.chat({ message: "退款多久到账？", user_id: "trace", conv_id: "trace-1" });
    await orchestrator.chat({ message: "订单什么时候到？", user_id: "trace", conv_id: "trace-2" });

    const traces = orchestrator.getRecentToolTraces(20);
    expect(traces).toHaveLength(1);
    expect(traces[0]?.conv_id).toBe("trace-2");
    expect(traces[0]?.skills_used).toEqual(["skill-general"]);
    expect(traces[0]?.model_id).toBe(config.model.id);
    expect(traces[0]?.runtime_version).toBe("pi-agent-core@0.82.1");
  });
});
