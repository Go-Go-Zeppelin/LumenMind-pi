import { describe, expect, it } from "vitest";
import { AGENT_PROFILES } from "../src/agents/profiles.js";
import { runAgentWorkflow } from "../src/agents/workflow.js";
import { roleTools } from "../src/tools/agent-tools.js";

describe("Pi Agent 契约与工作流", () => {
  it("每个业务 Agent 使用不同的实际工具白名单", () => {
    expect(roleTools("general").map((tool) => tool.name)).toEqual([
      "inspect_request_context",
      "suggest_required_fields",
    ]);
    expect(roleTools("technical").map((tool) => tool.name)).toEqual([
      "lookup_error_code",
      "build_diagnostic_plan",
    ]);
    expect(roleTools("billing").map((tool) => tool.name)).toEqual([
      "check_billing_fields",
      "compare_amounts",
    ]);
  });

  it("不同领域执行不同的确定性前置工作流", () => {
    const technical = runAgentWorkflow("technical", "登录报错 401", { error_code: ["401"] });
    const billing = runAgentWorkflow("billing", "重复扣款 50 元", { amount: ["50 元"] });
    expect(technical.stages).toContain("提取错误码");
    expect(technical.context).toContain("401");
    expect(billing.stages).toContain("核对订单和金额");
    expect(billing.context).toContain("50 元");
    expect(AGENT_PROFILES.technical.output_fields).toContain("diagnosis");
  });
});
