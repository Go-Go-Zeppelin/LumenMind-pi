import type { AgentTool } from "@earendil-works/pi-agent-core";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PiAgentExecutor, AgentExecutionError } from "../src/agents/executor.js";
import { config } from "../src/config.js";

const scenario = vi.hoisted(() => ({ interrupt: false }));
vi.mock("@earendil-works/pi-agent-core", () => ({
  Agent: class {
    state = { messages: [] as unknown[] };
    private subscriber?: (event: unknown) => Promise<void>;
    subscribe(fn: (event: unknown) => Promise<void>) { this.subscriber = fn; }
    async prompt() {
      await this.subscriber?.({ type: "tool_execution_start", toolName: "lookup_error_code", toolCallId: "call-1", args: { error_code: "401" } });
      if (scenario.interrupt) throw new Error("stream interrupted");
      await this.subscriber?.({ type: "tool_execution_end", toolName: "lookup_error_code", toolCallId: "call-1", isError: false,
        result: { details: { success: true, meaning: "身份验证失败" }, content: [{ type: "text", text: "解释" }] } });
      this.state.messages.push({ role: "assistant", stopReason: "stop", content: [{ type: "text", text: JSON.stringify({
        response: "请检查凭证是否过期", steps: ["检查凭证"], required_info: [], escalate: false,
      }) }] });
    }
  },
}));

describe("工具执行证据", () => {
  beforeEach(() => { scenario.interrupt = false; });
  const input = { message: "登录报错401", userId: "u1001", convId: "test", memoryContext: "", skillPrompt: "", entities: {}, allowKnowledge: false };
  const executor = () => new PiAgentExecutor("technical", { ...config, apiKey: "test-key" }, { name: "knowledge_search" } as AgentTool);

  it("保留调用入参、真实返回和开始结束时间", async () => {
    const result = await executor().run(input);
    const trace = result.toolTraces[0]!;
    expect(trace.input).toEqual({ error_code: "401" });
    expect(trace.output).toEqual({ success: true, meaning: "身份验证失败" });
    expect(Date.parse(trace.started_at!)).toBeLessThanOrEqual(Date.parse(trace.finished_at!));
    expect(trace.success).toBe(true);
  });

  it("流中断仍保留未完成调用的证据", async () => {
    scenario.interrupt = true;
    try {
      await executor().run(input);
      throw new Error("expected interruption");
    } catch (error) {
      expect(error).toBeInstanceOf(AgentExecutionError);
      const trace = (error as AgentExecutionError).toolTraces[0]!;
      expect(trace.error).toBe("stream interrupted");
      expect(trace.is_error).toBe(true);
      expect(trace.started_at).toBeTruthy();
      expect(trace.finished_at).toBeTruthy();
    }
  });
});
