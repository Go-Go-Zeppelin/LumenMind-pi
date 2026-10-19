import type { AgentToolResult } from "@earendil-works/pi-agent-core";
import type { Context, Message } from "@earendil-works/pi-ai";

export interface ModelBudgetOptions {
  /** Byte-to-token estimate multiplier, deliberately conservative without a tokenizer. */
  safetyMultiplier?: number;
  providerOverheadTokens?: number;
}

export interface ModelBudgetReport {
  serializedChars: number;
  serializedBytes: number;
  estimatedInputTokens: number;
  reservedOutputTokens: number;
  contextWindow: number;
  estimateMethod: "utf8_bytes_with_margin";
}

export class ModelBudgetError extends Error {
  readonly name = "ModelBudgetError";
  constructor(message: string, readonly report?: ModelBudgetReport) { super(message); }
}

export class ToolResultBudgetError extends Error {
  readonly name = "ToolResultBudgetError";
}

export class ToolPairingError extends Error {
  readonly name = "ToolPairingError";
}

export function unicodeLength(value: string): number { return Array.from(value).length; }

/** Run before a model call, after all previous tool calls have completed. */
export function assertToolPairs(messages: readonly Message[]): void {
  const pending = new Map<string, string>();
  const seen = new Set<string>();
  for (const message of messages) {
    if (message.role === "toolResult") {
      const name = pending.get(message.toolCallId);
      if (name === undefined || name !== message.toolName) {
        throw new ToolPairingError(`Orphan or mismatched tool result: ${message.toolCallId}`);
      }
      pending.delete(message.toolCallId);
      continue;
    }
    if (pending.size) throw new ToolPairingError("Tool results must complete before another user/assistant message");
    if (message.role === "assistant") {
      for (const part of message.content) {
        if (part.type !== "toolCall") continue;
        if (seen.has(part.id)) throw new ToolPairingError(`Duplicate tool call: ${part.id}`);
        seen.add(part.id);
        pending.set(part.id, part.name);
      }
    }
  }
  if (pending.size) throw new ToolPairingError("Model context has unfinished tool calls");
}

/**
 * Checks every actual model request, including tool loops and repair calls.
 * This estimate is intentionally stricter than character budgets. Actual API
 * usage must calibrate it; it is not a claim that characters equal tokens.
 */
export function assertModelBudget(
  model: { contextWindow: number; maxTokens: number },
  context: Context,
  outputTokens: number = model.maxTokens,
  options: ModelBudgetOptions = {},
): ModelBudgetReport {
  const safetyMultiplier = options.safetyMultiplier ?? 1.1;
  const providerOverheadTokens = options.providerOverheadTokens ?? 1_024;
  if (!Number.isFinite(model.contextWindow) || model.contextWindow <= 0 ||
      !Number.isFinite(model.maxTokens) || model.maxTokens <= 0 ||
      !Number.isInteger(outputTokens) || outputTokens <= 0 || outputTokens > model.maxTokens ||
      !Number.isFinite(safetyMultiplier) || safetyMultiplier < 1 ||
      !Number.isInteger(providerOverheadTokens) || providerOverheadTokens < 0) {
    throw new ModelBudgetError("Invalid model window, output reserve, or budget configuration");
  }
  assertToolPairs(context.messages);
  // Pi tool details, usage, timestamps and telemetry are not model-visible.
  // Tool arguments, signatures and result content all remain in the projection.
  const payload = {
    systemPrompt: context.systemPrompt ?? "",
    messages: context.messages.map((message) => message.role === "toolResult" ? {
      role: message.role, toolCallId: message.toolCallId, toolName: message.toolName,
      content: message.content, isError: message.isError,
    } : { role: message.role, content: message.content }),
    tools: (context.tools ?? []).map((tool) => ({
      name: tool.name, description: tool.description, parameters: tool.parameters,
      ...(tool.constrainedSampling === undefined ? {} : { constrainedSampling: tool.constrainedSampling }),
    })),
  };
  let serialized: string;
  try { serialized = JSON.stringify(payload); }
  catch { throw new ModelBudgetError("Model context is not JSON serializable"); }
  const serializedBytes = Buffer.byteLength(serialized, "utf8");
  const report: ModelBudgetReport = {
    serializedChars: unicodeLength(serialized), serializedBytes,
    estimatedInputTokens: Math.ceil(serializedBytes * safetyMultiplier) + providerOverheadTokens,
    reservedOutputTokens: outputTokens, contextWindow: model.contextWindow,
    estimateMethod: "utf8_bytes_with_margin",
  };
  if (report.estimatedInputTokens + outputTokens > model.contextWindow) {
    throw new ModelBudgetError("Model context and output reserve exceed the configured window", report);
  }
  return report;
}

interface Payload {
  data: unknown;
  changed: boolean;
  removedItems: number;
  raw: boolean;
}
interface TextCandidate { payload: Payload; parent: Record<string, unknown> | unknown[] | Payload; key: string | number; value: string }
interface ListCandidate { payload: Payload; value: unknown[] }
const FREE_TEXT_FIELDS = new Set(["content", "text", "description", "summary", "message", "error", "body", "notes"]);
const TRUNCATION_MARKER = "… [truncated]";

function candidates(payload: Payload): { texts: TextCandidate[]; lists: ListCandidate[] } {
  const texts: TextCandidate[] = [];
  const lists: ListCandidate[] = [];
  const visit = (value: unknown, parent: TextCandidate["parent"], key: string | number, freeText = false): void => {
    if (typeof value === "string" && freeText) texts.push({ payload, parent, key, value });
    else if (Array.isArray(value)) {
      if (value.length) lists.push({ payload, value });
      value.forEach((item, index) => visit(item, value, index, false));
    } else if (value !== null && typeof value === "object") {
      for (const [field, child] of Object.entries(value)) {
        visit(child, value as Record<string, unknown>, field, FREE_TEXT_FIELDS.has(field));
      }
    }
  };
  visit(payload.data, payload, "data", typeof payload.data === "string");
  return { texts, lists };
}

function shorten(candidate: TextCandidate, chars: number): void {
  const points = Array.from(candidate.value);
  const value = points.slice(0, Math.max(0, chars - unicodeLength(TRUNCATION_MARKER))).join("") + TRUNCATION_MARKER;
  (candidate.parent as Record<string | number, unknown>)[candidate.key] = value;
  candidate.payload.changed = true;
}

/**
 * Bound model-visible text, preserving details for application traces. JSON is
 * parsed/re-serialized, never cut as raw bytes. Scalar business identifiers,
 * amounts, statuses and ownership fields are retained; whole list records can
 * be omitted with an explicit count. Required fields that cannot fit fail.
 */
export function boundToolResult<T>(result: AgentToolResult<T>, maxChars = 8_000): AgentToolResult<T> {
  if (!Number.isInteger(maxChars) || maxChars <= 0) throw new ToolResultBudgetError("Invalid tool result character budget");
  if (unicodeLength(JSON.stringify(result.content)) <= maxChars) return result;
  const payloads = result.content.map((part): Payload | null => {
    if (part.type !== "text") return null;
    try { return { data: JSON.parse(part.text) as unknown, changed: false, removedItems: 0, raw: false }; }
    catch { return { data: part.text, changed: false, removedItems: 0, raw: true }; }
  });
  const render = (): AgentToolResult<T>["content"] => result.content.map((part, index) => {
    const payload = payloads[index];
    if (part.type !== "text" || !payload) return part;
    if (!payload.changed) return payload.raw ? part : { ...part, text: JSON.stringify(payload.data) };
    return { ...part, text: JSON.stringify({
      data: payload.data, context_truncated: true, omitted_items: payload.removedItems,
    }) };
  });
  let content = render();
  while (unicodeLength(JSON.stringify(content)) > maxChars) {
    const all = payloads.flatMap((payload) => payload ? [candidates(payload)] : []);
    const texts = all.flatMap((item) => item.texts).sort((a, b) => unicodeLength(b.value) - unicodeLength(a.value));
    const longest = texts[0];
    if (longest && unicodeLength(longest.value) > 1_000) {
      shorten(longest, 1_000);
    } else {
      const lists = all.flatMap((item) => item.lists).sort((a, b) => unicodeLength(JSON.stringify(b.value)) - unicodeLength(JSON.stringify(a.value)));
      const largest = lists[0];
      if (largest) {
        largest.value.pop();
        largest.payload.changed = true;
        largest.payload.removedItems += 1;
      } else if (longest && unicodeLength(longest.value) > 64) {
        shorten(longest, Math.max(64, Math.floor(unicodeLength(longest.value) / 2)));
      } else {
        throw new ToolResultBudgetError("Tool result cannot fit while preserving required fields and structure");
      }
    }
    content = render();
  }
  return { ...result, content };
}
