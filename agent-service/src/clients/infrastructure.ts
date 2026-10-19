/**
 * Pi 服务（TypeScript）访问 Python 基础设施（:8000）的唯一 HTTP 客户端。
 *
 * 职责边界：Pi 服务不直接读写 Redis/ChromaDB，所有记忆和知识库访问
 * 都通过这里的四个方法走 HTTP：
 *   - health()         GET  /health            探活，供 Pi /health 聚合状态
 *   - getMemoryContext POST /memory/context    Agent 执行前读取记忆
 *   - commitTurn()     POST /memory/turn       回复后写入完整一轮对话
 *   - searchKnowledge  POST /knowledge/search  Pi AgentTool 与 MCP 共用的检索契约
 */
import { randomUUID } from "node:crypto";
import type { IntentEmbeddingCandidate, KnowledgeSearchResponse, MemoryContext, MemoryOperationReceipt } from "../types.js";

export interface BusinessOrder {
  order_id: string;
  user_id: string;
  product_name: string;
  quantity: number;
  total_amount_cents: number;
  currency: "CNY";
  status: "pending_payment" | "processing" | "shipped" | "completed" | "cancelled";
  created_at: string;
}

export interface BusinessBill {
  bill_id: string;
  order_id: string;
  amount_cents: number;
  currency: "CNY";
  status: "unpaid" | "paid";
  payment_method: string | null;
  paid_at: string | null;
  created_at: string;
}

export interface BusinessList<T> {
  items: T[];
  count: number;
  source: "demo_business_db";
}

export interface BusinessDetail<T> {
  item: T;
  source: "demo_business_db";
}

/**
 * 记忆读取失败时的兜底空上下文。
 * 语义：记忆不可用时 Agent 仍可执行（无记忆降级），而不是让整个请求失败。
 * 注意每次返回浅拷贝（见 getMemoryContext），避免多个请求共享同一对象被污染。
 */
const EMPTY_MEMORY: MemoryContext = {
  prompt_text: "",
  recent_messages: [],
  relevant_history: [],
  user_profile: {},
  summary: "",
  schema_version: 2,
  status: "unavailable",
};

export interface MemoryTurnBeginReceipt {
  status: string;
  request_id: string;
  conv_id: string;
  turn_seq: number;
  source_seq: number;
  lease_token: string;
  lease_until?: string | null;
  context_revision?: number;
  replayed?: boolean;
  response?: string | null;
}

export interface MemoryTurnFinishReceipt {
  status: string;
  memory_status?: string;
  request_id: string;
  conv_id: string;
  turn_seq?: number;
  source_seq?: number;
  response?: string | null;
}

/** Python 基础设施返回非 2xx 时抛出的错误，携带 HTTP 状态码方便上层区分处理。 */
export class InfrastructureError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
    this.name = "InfrastructureError";
  }
}

export class InfrastructureClient {
  /**
   * @param baseUrl   Python 服务地址，来自 config.infrastructureUrl（如 http://python-infra:8000）
   * @param timeoutMs 每次请求的超时（config.infrastructureTimeoutMs，默认 10000ms）
   */
  constructor(
    readonly baseUrl: string,
    private readonly timeoutMs: number,
  ) {}

  /**
   * 组合超时信号与外部取消信号。
   * - 无外部 signal：仅按 timeoutMs 超时中断
   * - 有外部 signal（如 SSE 请求的 req.on("close")）：任一触发即中断
   * 典型场景：客户端断开 SSE 后，正在进行的知识检索也会被取消。
   */
  private signal(external?: AbortSignal): AbortSignal {
    const timeout = AbortSignal.timeout(this.timeoutMs);
    return external ? AbortSignal.any([external, timeout]) : timeout;
  }

  /**
   * 通用请求封装：fetch + JSON 解析 + 非 2xx 转 InfrastructureError。
   * 响应体尽量按 JSON 解析；解析失败时保留原始文本（比如 Python 返回纯文本错误页）。
   */
  private async request<T>(path: string, init: RequestInit = {}, external?: AbortSignal): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...init.headers },
      signal: this.signal(external),
    });
    const text = await response.text();
    let payload: unknown = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = text;
      }
    }
    if (!response.ok) {
      throw new InfrastructureError(`Python 基础设施 ${path} 返回 ${response.status}: ${text.slice(0, 300)}`, response.status);
    }
    return payload as T;
  }

  // Only retry durable idempotent protocol operations; the serialized payload stays unchanged.
  private async retryMemory<T>(path: string, init: RequestInit): Promise<T> {
    for (let attempt = 0; ; attempt++) {
      try { return await this.request<T>(path, init); }
      catch (error) {
        if (attempt >= 1 || (error instanceof InfrastructureError && (error.status ?? 500) < 500)) throw error;
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
    }
  }

  /** 探测 Python 基础设施健康状态，供 Pi /health 聚合展示依赖状态。 */
  async health(): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("/health");
  }

  async listOrders(userId: string, limit = 10, signal?: AbortSignal): Promise<BusinessList<BusinessOrder>> {
    const query = new URLSearchParams({ user_id: userId, limit: String(limit) });
    return this.request(`/business/orders?${query}`, {}, signal);
  }

  async getOrder(userId: string, orderId: string, signal?: AbortSignal): Promise<BusinessDetail<BusinessOrder>> {
    const query = new URLSearchParams({ user_id: userId });
    return this.request(`/business/orders/${encodeURIComponent(orderId)}?${query}`, {}, signal);
  }

  async listBills(userId: string, orderId?: string, limit = 10, signal?: AbortSignal): Promise<BusinessList<BusinessBill>> {
    const query = new URLSearchParams({ user_id: userId, limit: String(limit) });
    if (orderId !== undefined) query.set("order_id", orderId);
    return this.request(`/business/bills?${query}`, {}, signal);
  }

  async getBill(userId: string, billId: string, signal?: AbortSignal): Promise<BusinessDetail<BusinessBill>> {
    const query = new URLSearchParams({ user_id: userId });
    return this.request(`/business/bills/${encodeURIComponent(billId)}?${query}`, {}, signal);
  }

  /**
   * 读取记忆上下文（HTTP 契约 1/4）。
   *
   * 失败语义（与写入不同，这里吞掉异常）：
   *   读取失败 -> console.warn + 返回空上下文，Agent 照常执行。
   * 这样 Python 记忆服务短暂不可用时对话不会中断，只是"失忆"一轮。
   *
   * 对应 Python 侧：POST /memory/context -> MemoryManager.get_context()
   * 返回 {prompt_text, recent_messages, relevant_history, user_profile, summary}。
   */
  async getMemoryContext(userId: string, convId: string, query: string, requestId?: string): Promise<MemoryContext> {
    try {
      const context = await this.request<MemoryContext>("/memory/context", {
        method: "POST",
        body: JSON.stringify({ user_id: userId, conv_id: convId, query, request_id: requestId }),
      });
      return { ...EMPTY_MEMORY, ...context, status: context.status ?? "ready" };
    } catch (error) {
      if (error instanceof InfrastructureError && error.status === 413) throw error;
      console.warn("记忆服务不可用，本轮使用空上下文:", error instanceof Error ? error.message : error);
      return { ...EMPTY_MEMORY, recent_messages: [], relevant_history: [], user_profile: {} };
    }
  }

  async beginTurn(userId: string, convId: string | undefined, requestId: string, message: string, idempotencyKey?: string): Promise<MemoryTurnBeginReceipt> {
    return this.retryMemory<MemoryTurnBeginReceipt>("/memory/turn/begin", {
      method: "POST",
      headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined,
      body: JSON.stringify({ user_id: userId, conv_id: convId, request_id: requestId, message, idempotency_key: idempotencyKey }),
    });
  }

  async renewTurn(userId: string, convId: string, requestId: string, leaseToken: string): Promise<{ status: string; lease_until: string }> {
    return this.request("/memory/turn/renew", { method: "POST", body: JSON.stringify({ user_id: userId, conv_id: convId, request_id: requestId, lease_token: leaseToken }) });
  }

  async finishTurn(userId: string, convId: string, requestId: string, leaseToken: string, status: "COMPLETED" | "FAILED" | "CANCELLED" | "INTERRUPTED", response?: string): Promise<MemoryTurnFinishReceipt> {
    return this.retryMemory<MemoryTurnFinishReceipt>("/memory/turn/finish", { method: "POST", body: JSON.stringify({ user_id: userId, conv_id: convId, request_id: requestId, lease_token: leaseToken, status, response }) });
  }

  async flushMemory(userId: string, convId: string, requestId: string): Promise<MemoryOperationReceipt> {
    return this.request<MemoryOperationReceipt>("/memory/flush", { method: "POST", body: JSON.stringify({ user_id: userId, conv_id: convId, request_id: requestId }) });
  }

  async listMemoryFacts(userId: string): Promise<{ facts: Array<Record<string, unknown>>; revision: number; enabled: boolean }> {
    return this.request("/memory/facts?user_id=" + encodeURIComponent(userId));
  }

  async retractMemoryFact(userId: string, factId: string): Promise<MemoryOperationReceipt> {
    return this.request<MemoryOperationReceipt>(`/memory/facts/${encodeURIComponent(factId)}?user_id=${encodeURIComponent(userId)}`, { method: "DELETE" });
  }

  async clearMemory(userId: string, convId = "", requestId: string = randomUUID()): Promise<MemoryOperationReceipt> {
    return this.request<MemoryOperationReceipt>("/memory/clear", { method: "POST", body: JSON.stringify({ user_id: userId, conv_id: convId, request_id: requestId }) });
  }

  async setMemoryEnabled(userId: string, enabled: boolean): Promise<{ status: string; enabled: boolean }> {
    return this.request("/memory/settings", { method: "POST", body: JSON.stringify({ user_id: userId, enabled }) });
  }

  async deleteMemoryConversation(userId: string, convId: string): Promise<Record<string, unknown>> {
    return this.request(`/memory/conversations/${encodeURIComponent(convId)}?user_id=${encodeURIComponent(userId)}`, { method: "DELETE" });
  }

  /**
   * 写入一轮完整对话（HTTP 契约 2/4）。
   *
   * 失败语义（与读取不同，这里不吞异常）：
   *   写入失败 -> 异常向上抛，由 Orchestrator.commitTurn() 捕获并仅记录警告，
   *   已生成的 Agent 回复不会被丢弃。
   *
   * 注意：写入的是"用户消息 + 最终合并后的回复"，
   * 多 Agent 场景下辅助 Agent 的独立响应不会单独写入记忆。
   *
   * 对应 Python 侧：POST /memory/turn
   *   -> Redis 写入 user/assistant 两条消息
   *   -> BackgroundTasks 异步更新 user_profile
   */
  async commitTurn(userId: string, convId: string, message: string, response: string): Promise<void> {
    await this.request("/memory/turn", {
      method: "POST",
      body: JSON.stringify({ user_id: userId, conv_id: convId, message, response }),
    });
  }

  /**
   * 知识库检索（HTTP 契约 3/4，health 是 4/4）。
   *
   * 两个调用方共用此方法，避免两套知识逻辑漂移：
   *   1. Pi AgentTool（tools/knowledge-search.ts）—— Pi Agent 工具循环内部调用
   *   2. 标准 MCP（mcp/server.ts / mcp/stdio.ts）—— 外部 MCP 客户端调用
   *
   * @param signal 外部取消信号：SSE 客户端断开时透传给 Python 请求
   * 对应 Python 侧：POST /knowledge/search -> KnowledgeSearchResponse
   */
  async searchKnowledge(query: string, topK: number, signal?: AbortSignal): Promise<KnowledgeSearchResponse> {
    return this.request<KnowledgeSearchResponse>("/knowledge/search", {
      method: "POST",
      body: JSON.stringify({ query, top_k: topK }),
    }, signal);
  }

  /** 意图 Embedding 候选召回；向量生成和索引实现留在 Python/ChromaDB。 */
  async searchIntentCandidates(query: string, topK = 3): Promise<IntentEmbeddingCandidate[]> {
    const result = await this.request<{ candidates?: IntentEmbeddingCandidate[] }>("/intent/embedding", {
      method: "POST",
      body: JSON.stringify({ query, top_k: topK }),
    });
    return Array.isArray(result.candidates) ? result.candidates : [];
  }
}
