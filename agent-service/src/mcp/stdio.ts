/**
 * MCP stdio 入口。
 *
 * 作用：
 * - 以标准输入/输出（stdio）方式启动 MCP Server，供本地 CLI、编辑器等 MCP 客户端调用。
 * - 与 HTTP 版 /mcp 不同，这里不走 Express，而是通过 stdin/stdout 传输 MCP 协议。
 *
 * 调用方：
 * - 通常由外部 MCP 客户端通过子进程方式启动，例如：
 *   npm run mcp:stdio
 *   或
 *   npx tsx src/mcp/stdio.ts
 *
 * 数据流：
 * 外部客户端消息
 *   -> StdioServerTransport
 *   -> createKnowledgeMcpServer() 创建的 MCP Server
 *   -> InfrastructureClient.searchKnowledge()
 *   -> Python /knowledge/search
 */
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { config } from "../config.js";
import { InfrastructureClient } from "../clients/infrastructure.js";
import { createKnowledgeMcpServer } from "./server.js";

// 创建连接 Python 基础设施的客户端。
const client = new InfrastructureClient(config.infrastructureUrl, config.infrastructureTimeoutMs);

// 创建同一个 knowledge_search MCP Server。
const server = createKnowledgeMcpServer(client);

// 连接到 stdio 传输层；启动后等待外部客户端通过 stdin/stdout 通信。
await server.connect(new StdioServerTransport());

