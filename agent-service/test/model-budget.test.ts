import { describe, expect, it } from "vitest";
import type { AssistantMessage, Context, Message, ToolResultMessage } from "@earendil-works/pi-ai";
import { assertModelBudget, assertToolPairs, boundToolResult, ModelBudgetError, ToolPairingError, ToolResultBudgetError, unicodeLength } from "../src/agents/model-budget.js";

const model = { contextWindow: 200_000, maxTokens: 4_096 };
const user = (text: string): Message => ({ role: "user", content: text, timestamp: 0 });
const call = (id: string, name = "get_order"): AssistantMessage => ({
  role: "assistant", content: [{ type: "toolCall", id, name, arguments: { order_id: "o1" } }],
  api: "anthropic-messages", provider: "anthropic", model: "example", stopReason: "toolUse", timestamp: 0,
  usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
});
const tool = (id: string, name = "get_order"): ToolResultMessage => ({
  role: "toolResult", toolCallId: id, toolName: name, content: [{ type: "text", text: "{}" }], isError: false, timestamp: 0,
});

describe("conservative model request budgets", () => {
  it("counts rules, schemas, history, UTF8 bytes and output reserve", () => {
    const context: Context = { systemPrompt: "system workflow skills", messages: [user("😀登录")], tools: [{ name: "get_order", description: "query", parameters: { type: "object" } as never }] };
    const report = assertModelBudget(model, context, 2_048);
    expect(report.serializedBytes).toBeGreaterThan(report.serializedChars);
    expect(report.estimatedInputTokens).toBe(Math.ceil(report.serializedBytes * 1.1) + 1_024);
    expect(report.reservedOutputTokens).toBe(2_048);
    expect(report.serializedBytes).toBeGreaterThan(assertModelBudget(model, { messages: context.messages }, 2_048).serializedBytes);
  });
  it("checks final request boundary and carries overflow report", () => {
    const context = { messages: [user("current request")] };
    const report = assertModelBudget(model, context, 100);
    const required = report.estimatedInputTokens + 100;
    expect(() => assertModelBudget({ ...model, contextWindow: required }, context, 100)).not.toThrow();
    try { assertModelBudget({ ...model, contextWindow: required - 1 }, context, 100); throw new Error("expected overflow"); }
    catch (error) { expect(error).toBeInstanceOf(ModelBudgetError); expect((error as ModelBudgetError).report?.contextWindow).toBe(required - 1); }
  });
  it("rechecks accumulated tools and repair instructions", () => {
    const first = { messages: [user("hello")] };
    const report = assertModelBudget(model, first, 100);
    const small = { ...model, contextWindow: report.estimatedInputTokens + 200 };
    expect(() => assertModelBudget(small, first, 100)).not.toThrow();
    expect(() => assertModelBudget(small, { messages: [...first.messages, call("c"), { ...tool("c"), content: [{ type: "text", text: "x".repeat(2_000) }] }, user("repair output contract")] }, 100)).toThrow(ModelBudgetError);
  });
  it("excludes application-only tool details and timestamps", () => {
    const response = tool("c");
    const base = assertModelBudget(model, { messages: [call("c"), response] }, 100);
    const enriched = assertModelBudget(model, { messages: [call("c"), { ...response, timestamp: 99, details: { huge: "x".repeat(200_000) } }] }, 100);
    expect(enriched).toEqual(base);
  });
  it.each([0, -1, 4_097, Number.NaN, 1.5])("rejects invalid output reserve %s", (output) => {
    expect(() => assertModelBudget(model, { messages: [] }, output)).toThrow(ModelBudgetError);
  });
  it("rejects undefined window and unsafe estimate settings", () => {
    expect(() => assertModelBudget({ ...model, contextWindow: 0 }, { messages: [] })).toThrow(ModelBudgetError);
    expect(() => assertModelBudget(model, { messages: [] }, 100, { safetyMultiplier: 0.1 })).toThrow(ModelBudgetError);
  });
});

describe("tool pairing", () => {
  it("allows complete parallel results in either order", () => {
    const parallel = { ...call("a"), content: [...call("a").content, ...call("b", "get_bill").content] };
    expect(() => assertToolPairs([user("hi"), parallel, tool("b", "get_bill"), tool("a"), user("repair")])).not.toThrow();
  });
  it.each([
    { messages: [tool("orphan")] }, { messages: [call("missing")] },
    { messages: [call("a"), tool("a", "wrong")] },
    { messages: [call("a"), user("premature"), tool("a")] },
    { messages: [call("a"), tool("a"), call("a"), tool("a")] },
  ])("rejects invalid pairs", ({ messages }) => { expect(() => assertToolPairs(messages)).toThrow(ToolPairingError); });
});

describe("structured tool bounds", () => {
  it("preserves small results and original details", () => {
    const result = { content: [{ type: "text" as const, text: '{"status":"paid"}' }], details: { order_id: "o1" } };
    expect(boundToolResult(result)).toBe(result);
  });
  it("bounds Unicode free text and preserves scalar business fields", () => {
    const data = { order_id: "o1", amount_cents: 1_200, status: "paid", user_id: "u1", description: "😀".repeat(12_000) };
    const result = { content: [{ type: "text" as const, text: JSON.stringify(data) }], details: data };
    const bounded = boundToolResult(result, 1_500);
    expect(unicodeLength(JSON.stringify(bounded.content))).toBeLessThanOrEqual(1_500);
    const visible = JSON.parse((bounded.content[0] as { text: string }).text);
    expect(visible.context_truncated).toBe(true);
    expect(visible.data).toMatchObject({ order_id: "o1", amount_cents: 1_200, status: "paid", user_id: "u1" });
    expect(visible.data.description).toContain("[truncated]");
    expect(bounded.details).toBe(data);
    expect(data.description).toHaveLength(24_000);
  });
  it("omits whole list records with explicit counts", () => {
    const data = { source: "demo_business_db", orders: Array.from({ length: 20 }, (_, i) => ({ order_id: `o${i}`, amount_cents: 100, status: "paid" })) };
    const bounded = boundToolResult({ content: [{ type: "text" as const, text: JSON.stringify(data) }], details: data }, 600);
    const visible = JSON.parse((bounded.content[0] as { text: string }).text);
    expect(visible.data.source).toBe("demo_business_db");
    expect(visible.data.orders.length).toBeLessThan(20);
    expect(visible.omitted_items).toBe(20 - visible.data.orders.length);
    expect(visible.data.orders[0]).toEqual(data.orders[0]);
    expect(data.orders).toHaveLength(20);
    expect(unicodeLength(JSON.stringify(bounded.content))).toBeLessThanOrEqual(600);
  });
  it("compacts whitespace without discarding fields", () => {
    const data = { order_id: "o1", amount_cents: 1200 };
    const bounded = boundToolResult({ content: [{ type: "text" as const, text: JSON.stringify(data, null, 100) }], details: data }, 150);
    expect(JSON.parse((bounded.content[0] as { text: string }).text)).toEqual(data);
  });
  it("bounds raw text with an explicit marker", () => {
    const bounded = boundToolResult({ content: [{ type: "text" as const, text: "raw text ".repeat(3_000) }], details: null }, 500);
    const visible = JSON.parse((bounded.content[0] as { text: string }).text);
    expect(visible.context_truncated).toBe(true);
    expect(visible.data).toContain("[truncated]");
    expect(unicodeLength(JSON.stringify(bounded.content))).toBeLessThanOrEqual(500);
  });
  it("fails explicitly when required scalar fields cannot fit", () => {
    const data = { order_id: "id".repeat(5_000), amount_cents: 100, status: "paid" };
    expect(() => boundToolResult({ content: [{ type: "text", text: JSON.stringify(data) }], details: null }, 100)).toThrow(ToolResultBudgetError);
    expect(() => boundToolResult({ content: [], details: null }, 0)).toThrow(ToolResultBudgetError);
  });
});
