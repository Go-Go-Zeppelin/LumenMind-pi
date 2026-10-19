import { afterEach, describe, expect, it, vi } from "vitest";
import { InfrastructureClient } from "../src/clients/infrastructure.js";
import { BUSINESS_TOOL_NAMES, createBusinessTools } from "../src/tools/business-tools.js";

const client = () => new InfrastructureClient("http://infra.test", 1000);
const reply = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });

describe("用户绑定业务工具与 HTTP 契约", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("四个工具 schema 不允许选择 user_id，HTTP 参数由闭包绑定并编码", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => reply({ items: [], count: 0, source: "demo_business_db" }));
    vi.stubGlobal("fetch", fetchMock);
    const tools = createBusinessTools(client(), "u+1001 &");
    expect(tools.map(t => t.name)).toEqual(BUSINESS_TOOL_NAMES);
    for (const tool of tools) {
      const schema = tool.parameters as unknown as { properties: object; additionalProperties: boolean };
      expect(schema.properties).not.toHaveProperty("user_id");
      expect(schema.additionalProperties).toBe(false);
    }
    const signal = new AbortController().signal;
    const args = [{ limit: 2, user_id: "u1002" }, { order_id: "ORD/1?" }, { order_id: "ORD-1", limit: 3 }, { bill_id: "BILL/1?" }];
    for (let i = 0; i < tools.length; i++) {
      const result = await tools[i]!.execute(`c${i}`, args[i]!, signal);
      expect(JSON.parse((result.content[0] as { text: string }).text)).toEqual(result.details);
    }
    const urls = fetchMock.mock.calls.map(([url]) => new URL(url));
    expect(urls.map(u => u.pathname)).toEqual(["/business/orders", "/business/orders/ORD%2F1%3F", "/business/bills", "/business/bills/BILL%2F1%3F"]);
    expect(urls.map(u => u.searchParams.get("user_id"))).toEqual(Array(4).fill("u+1001 &"));
    expect(urls[0]!.searchParams.get("limit")).toBe("2");
    expect(urls[2]!.searchParams.get("order_id")).toBe("ORD-1");
    expect(urls[2]!.searchParams.get("limit")).toBe("3");
    expect(fetchMock.mock.calls.every(([, init]) => init.signal instanceof AbortSignal)).toBe(true);
  });

  it("交错执行不串号，默认 limit 为 10，未知用户身份保持原样", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      const parsed = new URL(url);
      await new Promise(resolve => setTimeout(resolve, parsed.searchParams.get("user_id") === "u1001" ? 5 : 1));
      expect(parsed.searchParams.get("limit")).toBe("10");
      return reply({ items: [{ user_id: parsed.searchParams.get("user_id") }], count: 1, source: "demo_business_db" });
    }));
    const shared = client();
    const users = ["u1001", "u1002", "anonymous", "demo"];
    const results = await Promise.all(users.map(u => createBusinessTools(shared, u)[0]!.execute(u, {})));
    expect(results.map(r => (r.details as { items: { user_id: string }[] }).items[0]!.user_id)).toEqual(users);
    expect(() => createBusinessTools(shared, " ")).toThrow("userId");
  });

  it.each([1, 3])("详情 404 映射为正常 found=false：工具 %s", async (index) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(reply({ detail: "not_found" }, 404)));
    const result = await createBusinessTools(client(), "u1001")[index]!.execute("404", { order_id: "missing", bill_id: "missing" });
    expect(result.details).toEqual({ found: false, source: "demo_business_db" });
  });

  it.each([503, 500, 404])("列表 HTTP %s 保持执行失败语义", async (status) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(reply({ detail: "business_unavailable" }, status)));
    await expect(createBusinessTools(client(), "u1001")[0]!.execute("error", {})).rejects.toThrow("business_unavailable");
  });

  it("超时不会变成空列表", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new DOMException("timeout", "TimeoutError")));
    await expect(createBusinessTools(client(), "u1001")[0]!.execute("timeout", {})).rejects.toThrow("查询服务暂时不可用");
  });
});
