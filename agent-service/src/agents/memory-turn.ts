import { randomUUID } from "node:crypto";
import { InfrastructureError, type InfrastructureClient, type MemoryTurnBeginReceipt } from "../clients/infrastructure.js";
import type { ChatRequest, ChatResponse } from "../types.js";

interface RunningTurn {
  convId: string;
  requestId: string;
  available: boolean;
  signal: AbortSignal;
}

function replay(begin: MemoryTurnBeginReceipt): ChatResponse {
  if (begin.status !== "COMPLETED" || typeof begin.response !== "string") {
    throw new InfrastructureError("该幂等请求尚未完成或已经中断，请使用新的 Idempotency-Key", 409);
  }
  // Original response is durable; routing/trace metadata is not a Pi checkpoint.
  return {
    conv_id: begin.conv_id, request_id: begin.request_id, response: begin.response,
    intent: "other", intent_group: "other", agent_type: "general", agent_types: [],
    primary_agent: "general", supporting_agents: [], routing_reason: "持久化回复重放；未重新执行路由或业务工具",
    routing_confidence: 0, escalated: false, latency_ms: 0, knowledge_used: false,
    tools_used: [], output_valid: true, output_errors: [], entities: {}, intent_confidence: 0,
    intent_source_scores: {}, memory_status: "stored", replayed: true,
  };
}

/** Owns one durable lease. No fallback write may run after a failed/ambiguous begin. */
export async function withMemoryTurn(
  infra: InfrastructureClient, request: ChatRequest, renewMs: number,
  external: AbortSignal | undefined, execute: (turn: RunningTurn) => Promise<ChatResponse>,
): Promise<ChatResponse> {
  const userId = request.user_id || "anonymous";
  let convId = request.conv_id || randomUUID();
  let requestId: string = randomUUID();
  const controller = new AbortController();
  const signal = external ? AbortSignal.any([controller.signal, external]) : controller.signal;
  let begin: MemoryTurnBeginReceipt | undefined;
  // Compatibility exists only for old adapters without begin, never on network failure.
  const protocol = typeof infra.beginTurn === "function";
  if (protocol) {
    try {
      begin = await infra.beginTurn(userId, request.conv_id || undefined, requestId, request.message, request.idempotency_key);
      if (!begin.request_id || !begin.conv_id) throw new Error("Invalid memory receipt");
      if (begin.status !== "RECEIVED") return replay(begin);
      if (!begin.lease_token) throw new Error("Invalid memory lease receipt");
      if (begin.request_id !== requestId) throw new InfrastructureError("同一幂等请求正在处理", 409);
      convId = begin.conv_id;
      requestId = begin.request_id;
    } catch (error) {
      if (error instanceof InfrastructureError && (error.status ?? 500) < 500) throw error;
      begin = undefined;
      console.warn("记忆主库暂不可用，跳过本轮记忆读写");
    }
  }
  let leaseLost = false;
  let renewing: Promise<void> | undefined;
  const timer = begin ? setInterval(() => {
    if (renewing || signal.aborted) return;
    renewing = infra.renewTurn(userId, convId, requestId, begin!.lease_token)
      .then(() => undefined, () => {
        leaseLost = true;
        controller.abort(new InfrastructureError("记忆会话租约续期失败", 409));
      }).finally(() => { renewing = undefined; });
  }, renewMs) : undefined;
  timer?.unref();
  const stop = async () => { if (timer) clearInterval(timer); await renewing; };
  try {
    signal.throwIfAborted();
    const result = await execute({ convId, requestId, available: protocol ? Boolean(begin) : true, signal });
    await stop();
    signal.throwIfAborted();
    if (begin) {
      try {
        const receipt = await infra.finishTurn(userId, convId, requestId, begin.lease_token, "COMPLETED", result.response);
        result.memory_status = receipt.status === "COMPLETED" ? "stored" : "failed";
      } catch { result.memory_status = "failed"; }
    } else if (!protocol) {
      try { await infra.commitTurn(userId, convId, request.message, result.response); result.memory_status = "stored"; }
      catch { result.memory_status = "failed"; }
    } else { result.memory_status = "unavailable"; }
    return result;
  } catch (error) {
    await stop();
    if (begin && !leaseLost) {
      try { await infra.finishTurn(userId, convId, requestId, begin.lease_token, external?.aborted ? "CANCELLED" : "FAILED"); }
      catch { /* Recovery will settle an expired RECEIVED turn; no invented assistant. */ }
    }
    throw error;
  } finally { await stop(); }
}
