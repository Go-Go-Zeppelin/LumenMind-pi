import { describe, expect, it, vi } from "vitest";
import type { InfrastructureClient } from "../src/clients/infrastructure.js";
import { createKnowledgeSearchTool } from "../src/tools/knowledge-search.js";

describe("Pi knowledge_search 工具", () => {
  it("通过 Python HTTP 基础设施检索并返回模型可读内容", async () => {
    const searchKnowledge = vi.fn().mockResolvedValue({
      query: "退款多久",
      results: [{ title: "退款政策", content: "审核通过后原路退回", score: 0.9 }],
      reranked: false,
    });
    const tool = createKnowledgeSearchTool({ searchKnowledge } as unknown as InfrastructureClient);
    const result = await tool.execute("call-1", { query: "退款多久", top_k: 3 });

    expect(searchKnowledge).toHaveBeenCalledWith("退款多久", 3, undefined);
    expect(result.details.results[0]?.title).toBe("退款政策");
    expect(result.content[0]).toMatchObject({ type: "text" });
  });
});

