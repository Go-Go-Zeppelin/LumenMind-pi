import type { AgentTool } from "@earendil-works/pi-agent-core";
import { describe, expect, it, vi } from "vitest";
import { MemoryCoordinator, isPureMemoryRequest, memoryReceiptText, parseMemoryIntent } from "../src/agents/memory-coordinator.js";
import { LumenMindOrchestrator } from "../src/agents/orchestrator.js";
import { config } from "../src/config.js";
import type { AgentType, MemoryOperationReceipt } from "../src/types.js";

function coordinator() {
  const api = { flushMemory: vi.fn().mockResolvedValue({ request_id: "r", status: "PENDING" }), clearMemory: vi.fn().mockResolvedValue({ status: "WRITTEN", revision: 2, mutated: true }) };
  return { api, instance: new MemoryCoordinator(api as never) };
}

describe("explicit memory request parsing", () => {
  it.each([
    "翻译：清空我的长期记忆", "假设我要清空我的长期记忆", "如果我说清空我的长期记忆会怎样？",
    "他说：清空我的长期记忆", "她说清空我的长期记忆", "示例：清空我的长期记忆",
    "不要清空我的长期记忆", "请不要清空我的长期记忆", "别忘记我的偏好", "不要记住这些信息",
    '“清空我的长期记忆”', '"清空我的长期记忆"', "`清空我的长期记忆`", "'清空我的长期记忆'",
    "请解释清空记忆是什么意思", "请把文字「清空我的记忆」翻译成英文", "我在文档中看到清空记忆这个功能",
  ])("does not interpret quoted, negated or conditional text as authorization: %s", async (message) => {
    const { api, instance } = coordinator();
    expect(parseMemoryIntent(message).kind).toBe("none");
    expect(await instance.coordinate(message, "u", "c", "r")).toBeUndefined();
    expect(api.clearMemory).not.toHaveBeenCalled();
    expect(api.flushMemory).not.toHaveBeenCalled();
  });

  it.each(["清空我的长期记忆", "请清空我的所有长期记忆。", "删除全部记忆", "清除记忆！"])("accepts anchored clear request: %s", async (message) => {
    const { api, instance } = coordinator();
    expect(parseMemoryIntent(message).kind).toBe("clear");
    expect(await instance.coordinate(message, "u", "c", "r")).toMatchObject({ status: "WRITTEN", request_id: "r", mutated: true });
    expect(api.clearMemory).toHaveBeenCalledWith("u", "c", "r");
    expect(api.flushMemory).not.toHaveBeenCalled();
  });

  it("returns incomplete for unresolved retraction rather than clearing scope", async () => {
    const { api, instance } = coordinator();
    expect(await instance.coordinate("请忘掉这个联系偏好", "u", "c", "r")).toEqual({ request_id: "r", status: "INCOMPLETE", applied: 0, mutated: false });
    expect(api.clearMemory).not.toHaveBeenCalled();
    expect(api.flushMemory).not.toHaveBeenCalled();
  });

  it("binds explicit remember to the persisted request without passing arbitrary source text", async () => {
    const { api, instance } = coordinator();
    await expect(instance.coordinate("请记住，以后邮件联系", "u", "c", "r")).resolves.toMatchObject({ status: "PENDING" });
    expect(api.flushMemory).toHaveBeenCalledExactlyOnceWith("u", "c", "r");
    expect(isPureMemoryRequest("请记住，以后邮件联系")).toBe(true);
    expect(isPureMemoryRequest("请记住邮件联系，并帮我查询订单")).toBe(false);
  });
});

it.each(["PENDING", "BUSY", "BLOCKED", "FAILED", "CAPACITY_EXCEEDED", "DISABLED", "INCOMPLETE"] as const)("receipt %s never claims successful persistence", (status) => {
  const text = memoryReceiptText({ request_id: "r", status });
  expect(text).not.toMatch(/操作已保存|^已经记住|成功保存/);
});

function orchestratorHarness(status: MemoryOperationReceipt["status"]) {
  const infrastructure = {
    beginTurn: vi.fn(async (_u, c, r) => ({ status: "RECEIVED", request_id: r, conv_id: c, lease_token: "token" })),
    renewTurn: vi.fn().mockResolvedValue({}), finishTurn: vi.fn().mockResolvedValue({ status: "COMPLETED" }),
    flushMemory: vi.fn().mockResolvedValue({ request_id: "r", status, applied: 0, mutated: false }),
    clearMemory: vi.fn(), getMemoryContext: vi.fn(),
  };
  const orchestrator = new LumenMindOrchestrator({ ...config, apiKey: "", useLlmRouting: false }, infrastructure as never,
    { promptFor: vi.fn().mockReturnValue(""), matchedNamesFor: vi.fn().mockReturnValue([]) } as never,
    { name: "knowledge_search" } as AgentTool);
  const agents = (orchestrator as unknown as { agents: Record<AgentType, { run: ReturnType<typeof vi.fn> }> }).agents;
  for (const type of ["general", "technical", "billing"] as AgentType[]) agents[type].run = vi.fn();
  return { orchestrator, infrastructure, agents };
}

describe("deterministic pure memory response", () => {
  it.each(["PENDING", "BUSY", "CAPACITY_EXCEEDED", "NOOP", "DISABLED"] as const)("uses exact %s receipt and never calls a model", async (status) => {
    const { orchestrator, infrastructure, agents } = orchestratorHarness(status);
    const result = await orchestrator.chat({ message: "请记住，以后优先邮件联系", user_id: "u", conv_id: "c" });
    expect(result.response).toBe(memoryReceiptText({ request_id: "r", status }));
    expect(result.memory_operation?.status).toBe(status);
    expect(infrastructure.getMemoryContext).not.toHaveBeenCalled();
    for (const agent of Object.values(agents)) expect(agent.run).not.toHaveBeenCalled();
    expect(infrastructure.finishTurn).toHaveBeenCalledOnce();
    expect(infrastructure.finishTurn.mock.calls[0]?.[5]).toBe(result.response);
  });

  it("retains operation success when turn finishing fails", async () => {
    const { orchestrator, infrastructure } = orchestratorHarness("WRITTEN");
    infrastructure.finishTurn.mockRejectedValue(new Error("disk unavailable after fact commit"));
    const result = await orchestrator.chat({ message: "请记住，以后优先邮件联系", user_id: "u", conv_id: "c" });
    expect(result.memory_status).toBe("failed");
    expect(result.memory_operation?.status).toBe("WRITTEN");
    expect(result.response).toBe("记忆操作已保存。");
  });
});
