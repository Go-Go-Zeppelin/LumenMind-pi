import type { InfrastructureClient } from "../clients/infrastructure.js";
import type { MemoryOperationReceipt } from "../types.js";

export type MemoryIntent = { kind: "remember" | "clear" | "retract" | "clarify" | "none" };

/** Only anchored direct requests qualify. Ambiguous quoted/conditional text never deletes. */
export function parseMemoryIntent(message: string): MemoryIntent {
  const text = message.trim();
  if (/^(翻译|translate|假如|假设|如果|他说|她说|示例|不要|别|不能|不必)/iu.test(text)
      || /[“”「」『』"`]|不要忘|别忘|不要记|不要清空|不要删除/u.test(text)) return { kind: "none" };
  if (/^(?:请)?(?:清空|清除|删除)\s*(?:我的)?(?:全部|所有)?(?:长期)?记忆[。！!]?$/u.test(text)) return { kind: "clear" };
  if (/^(?:请)?(?:帮我)?(?:记住|记下来)[，,:：\s]*\S/u.test(text)) return { kind: "remember" };
  if (/^(?:请)?(?:忘掉|撤回|删除).*(?:偏好|事实|信息|记录)/u.test(text)) return { kind: "retract" };
  if (/^(?:请)?(?:忘掉|撤回|清空)/u.test(text)) return { kind: "clarify" };
  return { kind: "none" };
}

export function memoryReceiptText(receipt: MemoryOperationReceipt): string {
  switch (receipt.status) {
    case "WRITTEN": return "记忆操作已保存。";
    case "NOOP": return "本次记忆整理已完成，没有新增或修改长期事实。";
    case "DISABLED": return "当前身份的长期记忆未开启，本次没有写入长期事实。";
    case "INCOMPLETE": return "请明确要删除哪条长期事实，或明确要求清空全部长期记忆。";
    case "PENDING": case "BUSY": return "记忆整理尚未完成，现在不能确认已经记住。";
    case "BLOCKED": return "记忆整理任务被阻塞，尚未保存本次偏好。";
    case "CAPACITY_EXCEEDED": return "长期记忆已超出容量，本次修改没有保存。";
    default: return "记忆操作未完成，请稍后重试。";
  }
}

export function isPureMemoryRequest(message: string): boolean {
  const kind = parseMemoryIntent(message).kind;
  return kind !== "none" && !/订单|账单|报错|故障|退款|发票|查一下|帮我查询|排查/u.test(message);
}

export class MemoryCoordinator {
  constructor(private readonly infrastructure: InfrastructureClient) {}

  async unavailable(message: string, requestId: string): Promise<MemoryOperationReceipt | undefined> {
    return parseMemoryIntent(message).kind === "none" ? undefined
      : { request_id: requestId, status: "FAILED", applied: 0, mutated: false };
  }

  async coordinate(message: string, userId: string, convId: string, requestId: string): Promise<MemoryOperationReceipt | undefined> {
    const intent = parseMemoryIntent(message);
    if (intent.kind === "none") return undefined;
    if (intent.kind === "clarify" || intent.kind === "retract") {
      return { request_id: requestId, status: "INCOMPLETE", applied: 0, mutated: false };
    }
    if (intent.kind === "remember") return this.infrastructure.flushMemory(userId, convId, requestId);
    const result = await this.infrastructure.clearMemory(userId, convId, requestId);
    return { ...result, request_id: requestId };
  }
}
