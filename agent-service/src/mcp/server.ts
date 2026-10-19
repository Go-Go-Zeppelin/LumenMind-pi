import type { Request, Response } from "express";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";
import type { InfrastructureClient } from "../clients/infrastructure.js";

export function createKnowledgeMcpServer(client: InfrastructureClient): McpServer {
  const server = new McpServer({ name: "lumenmind-knowledge", version: "3.0.0" });
  server.registerTool(
    "knowledge_search",
    {
      title: "搜索 LumenMind 知识库",
      description: "检索 LumenMind 客服业务知识。该工具只读，不能查询真实用户订单或执行操作。",
      inputSchema: {
        query: z.string().min(1).describe("检索问题"),
        top_k: z.number().int().min(1).max(10).default(5).describe("返回条数"),
      },
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    },
    async ({ query, top_k }) => {
      const result = await client.searchKnowledge(query, top_k);
      return {
        content: [{ type: "text" as const, text: JSON.stringify(result.results, null, 2) }],
        structuredContent: { ...result } as Record<string, unknown>,
      };
    },
  );
  return server;
}

export async function handleMcpHttpRequest(
  req: Request,
  res: Response,
  client: InfrastructureClient,
): Promise<void> {
  const server = createKnowledgeMcpServer(client);
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: undefined,
    enableJsonResponse: true,
  });
  res.on("close", () => {
    void transport.close();
    void server.close();
  });
  await server.connect(transport);
  await transport.handleRequest(req, res, req.body);
}
