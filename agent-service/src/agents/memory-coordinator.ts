import type { InfrastructureClient } from "../clients/infrastructure.js";
import type { MemoryOperationReceipt } from "../types.js";

export type MemoryIntent =
  | { kind: "remember" }
  | { kind: "clear" }
  | { kind: "retract"; factId?: string }
  | { kind: "clarify" }
  | { kind: "none" };

/** 只识别当前用户直接、肯定的记忆操作；引述/翻译/否定不触发删除。 */
export function parseMemoryIntent(message: string): MemoryIntent {
  const text = message.trim();
  if (!text || /^(翻译|translate)\s*[:：]/i.test(text) || /不要忘掉|别忘掉|不要记住/.test(text)) return { kind: "none" };
  if (/^(请)?(清空|清除|删除)\s*(我的)?(长期)?记忆[。！!]?$/u.test(text)) return { kind: "clear" };
  if (/(忘掉|删除|撤回).*(偏好|事实|信息|记录)/u.test(text)) return { kind: "retract" };
  if (/(记住|记下来|以后都|今后都).{1,}/u.test(text)) return { kind: "remember" };
  if (/忘掉|删除|撤回/u.test(text)) return { kind: "clarify" };
  return { kind: "none" };
}

export class MemoryCoordinator {
  constructor(private readonly infrastructure: InfrastructureClient) {}

  async flush(userId: string, convId: string, requestId: string): Promise<MemoryOperationReceipt> {
    return this.infrastructure.flushMemory(userId, convId, requestId);
  }

  async coordinate(message: string, userId: string, convId: string, requestId: string): Promise<MemoryOperationReceipt | undefined> {
    const intent = parseMemoryIntent(message);
    if (intent.kind === "none" || intent.kind === "clarify") return intent.kind === "clarify"
      ? { request_id: requestId, status: "INCOMPLETE", applied: 0, mutated: false }
      : undefined;
    if (intent.kind === "remember") return this.flush(userId, convId, requestId);
    if (intent.kind === "clear" && typeof (this.infrastructure as unknown as { clearMemory?: unknown }).clearMemory === "function") {
      return this.infrastructure.clearMemory(userId, convId, requestId);
    }
    // 没有目标 fact_id 时不猜测用户想删除什么，返回真实待处理状态。
    return { request_id: requestId, status: "PENDING", applied: 0, mutated: false };
  }
}

