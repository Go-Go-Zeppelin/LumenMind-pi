import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createApp } from "../src/app.js";
import { config } from "../src/config.js";

describe("Pi /intent", () => {
  let server: ReturnType<ReturnType<typeof createApp>["listen"]> | undefined;

  afterEach(async () => {
    if (!server) return;
    await new Promise<void>((resolve, reject) => server!.close((error) => error ? reject(error) : resolve()));
    server = undefined;
  });

  it("默认只做规则识别，不访问基础设施或业务 Agent", async () => {
    const infrastructure = {
      health: vi.fn(),
    };
    const orchestrator = {
      getStats: () => ({}),
      recognizeIntent: vi.fn(),
      getToolTrace: vi.fn().mockReturnValue({ request_id: "req-1", tool_calls: [] }),
      getRecentToolTraces: vi.fn().mockReturnValue([]),
    };
    const skills = {
      summary: () => ({ count: 0 }),
      reload: vi.fn(),
    };
    const app = createApp(
      { ...config, apiKey: "" },
      orchestrator as never,
      skills as never,
      infrastructure as never,
    );
    server = app.listen(0, "127.0.0.1");
    await new Promise<void>((resolve) => server!.once("listening", resolve));
    const address = server.address() as AddressInfo;

    const response = await fetch(`http://127.0.0.1:${address.port}/intent`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ message: "退款多久到账？" }),
    });
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ intent: "refund", intent_group: "billing" });
    expect(orchestrator.recognizeIntent).not.toHaveBeenCalled();
    expect(infrastructure.health).not.toHaveBeenCalled();

    const traceResponse = await fetch(`http://127.0.0.1:${address.port}/trace/tool/req-1`);
    expect(await traceResponse.json()).toMatchObject({ request_id: "req-1", found: true });
    const recentResponse = await fetch(`http://127.0.0.1:${address.port}/trace/tools?limit=5`);
    expect(await recentResponse.json()).toEqual({ items: [] });
  });
});
