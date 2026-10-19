import type { AgentTool } from "@earendil-works/pi-agent-core";
import { Type } from "@earendil-works/pi-ai";
import type { InfrastructureClient } from "../clients/infrastructure.js";
import type { KnowledgeSearchResponse } from "../types.js";

const parameters = Type.Object({
  query: Type.String({ minLength: 1, description: "要在 LumenMind 业务知识库中检索的问题" }),
  top_k: Type.Optional(Type.Integer({ minimum: 1, maximum: 10, default: 5 })),
});

export function createKnowledgeSearchTool(client: InfrastructureClient): AgentTool<typeof parameters, KnowledgeSearchResponse> {
  return {
    name: "knowledge_search",
    label: "搜索 LumenMind 知识库",
    description: "检索退款、配送、账户安全、技术故障、会员积分等经过维护的 LumenMind 业务知识。只读，不可查询真实订单或执行退款。",
    parameters,
    async execute(_toolCallId, params, signal) {
      const result = await client.searchKnowledge(params.query, params.top_k ?? 5, signal);
      return {
        content: [{ type: "text", text: JSON.stringify(result.results, null, 2) }],
        details: result,
      };
    },
  };
}
