import type { AddressInfo } from "node:net";
import express from "express";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { InfrastructureClient } from "../src/clients/infrastructure.js";
import { handleMcpHttpRequest } from "../src/mcp/server.js";

describe("Streamable HTTP MCP", () => {
  const closeCallbacks: Array<() => Promise<void>> = [];
  afterEach(async () => {
    await Promise.all(closeCallbacks.splice(0).map((close) => close()));
  });

  it("公开 knowledge_search 并代理到 Python HTTP 接口", async () => {
    const searchKnowledge = vi.fn().mockResolvedValue({
      query: "配送",
      results: [{ title: "配送说明", content: "标准配送 3-5 个工作日", score: 0.92 }],
      reranked: false,
    });
    const infrastructure = { searchKnowledge } as unknown as InfrastructureClient;
    const app = express();
    app.use(express.json());
    app.all("/mcp", (req, res, next) => {
      void handleMcpHttpRequest(req, res, infrastructure).catch(next);
    });
    const httpServer = app.listen(0, "127.0.0.1");
    await new Promise<void>((resolve) => httpServer.once("listening", resolve));

    const address = httpServer.address() as AddressInfo;
    const transport = new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${address.port}/mcp`));
    const client = new Client({ name: "lumenmind-test", version: "1.0.0" });
    await client.connect(transport);
    closeCallbacks.push(async () => {
      await client.close();
      await new Promise<void>((resolve, reject) => httpServer.close((error) => error ? reject(error) : resolve()));
    });

    const tools = await client.listTools();
    expect(tools.tools.map((tool) => tool.name)).toContain("knowledge_search");
    const result = await client.callTool({ name: "knowledge_search", arguments: { query: "配送", top_k: 2 } });
    expect(result.isError).not.toBe(true);
    expect(searchKnowledge).toHaveBeenCalledWith("配送", 2);
  });
});

