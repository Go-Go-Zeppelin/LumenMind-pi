import { existsSync } from "node:fs";
import path from "node:path";
import { config as loadEnv } from "dotenv";
import type { Model } from "@earendil-works/pi-ai";
import { anthropicProvider } from "@earendil-works/pi-ai/providers/anthropic";

loadEnv({ path: path.resolve(process.cwd(), ".env"), quiet: true });
loadEnv({ path: path.resolve(process.cwd(), "../.env"), quiet: true });

function numberFromEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value)) {
    throw new Error(`${name} 必须是数字，当前值为 ${raw}`);
  }
  return value;
}

function booleanFromEnv(name: string, fallback: boolean): boolean {
  const raw = process.env[name];
  if (raw === undefined) return fallback;
  return !["0", "false", "no", "off"].includes(raw.toLowerCase());
}

const anthropicModels = anthropicProvider().getModels();
const templateModel = anthropicModels.find((model) => model.id === "claude-sonnet-4-6") ?? anthropicModels[0];
if (!templateModel) throw new Error("Pi Anthropic provider 没有可用模型模板");
const modelId = process.env.PI_MODEL?.trim() || process.env.ANTHROPIC_MODEL?.trim() || templateModel.id;
const baseUrl = (process.env.PI_BASE_URL?.trim() || process.env.ANTHROPIC_BASE_URL?.trim() || templateModel.baseUrl).replace(/\/$/, "");
const projectRoot = [process.cwd(), path.resolve(process.cwd(), "..")]
  .find((candidate) => existsSync(path.join(candidate, "skills"))) ?? process.cwd();
const configuredSkillsDir = process.env.LUMENMIND_SKILLS_DIR?.trim() || "skills";

export const config = {
  host: process.env.PI_HOST ?? "0.0.0.0",
  port: numberFromEnv("PI_PORT", 3000),
  apiKey: process.env.PI_API_KEY?.trim() || process.env.ANTHROPIC_API_KEY?.trim() || "",
  model: {
    ...templateModel,
    id: modelId,
    name: modelId,
    baseUrl,
  } satisfies Model<"anthropic-messages">,
  maxTokens: numberFromEnv("PI_MAX_TOKENS", 1024),
  thinkingLevel: (process.env.PI_THINKING_LEVEL ?? "off") as "off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max",
  useLlmRouting: booleanFromEnv("PI_LLM_ROUTING", true),
  // 规则未明确时先用 Python/ChromaDB 的 Embedding 索引召回候选意图。
  useEmbeddingIntent: booleanFromEnv("PI_EMBEDDING_INTENT", true),
  embeddingIntentTopK: Math.max(1, Math.floor(numberFromEnv("PI_EMBEDDING_INTENT_TOP_K", 3))),
  embeddingIntentThreshold: Math.max(0, Math.min(1, numberFromEnv("PI_EMBEDDING_INTENT_THRESHOLD", 0.55))),
  infrastructureUrl: (process.env.PYTHON_INFRA_URL ?? "http://localhost:8000").replace(/\/$/, ""),
  memoryRenewIntervalMs: 20_000,
  infrastructureTimeoutMs: numberFromEnv("PYTHON_INFRA_TIMEOUT_MS", 10_000),
  skillsDir: path.isAbsolute(configuredSkillsDir) ? configuredSkillsDir : path.resolve(projectRoot, configuredSkillsDir),
  skillsMaxPromptChars: numberFromEnv("LUMENMIND_SKILLS_MAX_PROMPT_CHARS", 5000),
  generalReplicas: Math.max(1, Math.floor(numberFromEnv("PI_GENERAL_REPLICAS", 1))),
  technicalReplicas: Math.max(1, Math.floor(numberFromEnv("PI_TECHNICAL_REPLICAS", 1))),
  billingReplicas: Math.max(1, Math.floor(numberFromEnv("PI_BILLING_REPLICAS", 1))),
  toolTraceMax: Math.max(1, Math.floor(numberFromEnv("PI_TOOL_TRACE_MAX", 200))),
  // 专业 Agent 的监控降权达到该阈值时，从本次路由候选中移除并回退到 General。
  monitorFallbackPenalty: Math.max(0, Math.min(0.9, numberFromEnv(
    "PI_MONITOR_FALLBACK_PENALTY",
    numberFromEnv("LUMENMIND_MONITOR_FALLBACK_PENALTY", 0.5),
  ))),
};

export type AppConfig = typeof config;
