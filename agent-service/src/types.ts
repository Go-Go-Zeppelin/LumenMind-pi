export type AgentType = "general" | "technical" | "billing";
export type RoutingAgentType = AgentType | "escalation";
export type ExecutionAgentType = RoutingAgentType;

export type Intent =
  | "query"
  | "complaint"
  | "request"
  | "greeting"
  | "escalation"
  | "technical"
  | "billing"
  | "account"
  | "feedback"
  | "order_status"
  | "logistics"
  | "refund"
  | "invoice"
  | "payment_issue"
  | "account_security"
  | "technical_login"
  | "technical_crash"
  | "human_handoff"
  | "other";

export type Urgency = "low" | "medium" | "high" | "critical";

export interface IntentDecision {
  intent: Intent;
  intent_group: Intent;
  confidence: number;
  urgency: Urgency;
  reasoning: string;
  entities: Record<string, string[]>;
  source_scores: Record<string, number>;
}

/** Embedding 意图索引返回的候选结果（相似度已由基础设施层归一化到 0~1）。 */
export interface IntentEmbeddingCandidate {
  intent: Intent;
  score: number;
  example: string;
}

export interface ChatRequest {
  message: string;
  user_id?: string;
  conv_id?: string | null;
}

export interface ChatResponse {
  conv_id: string;
  response: string;
  intent: Intent;
  intent_group: Intent;
  agent_type: RoutingAgentType;
  agent_types: RoutingAgentType[];
  primary_agent: RoutingAgentType;
  supporting_agents: AgentType[];
  routing_reason: string;
  routing_confidence: number;
  escalated: boolean;
  latency_ms: number;
  knowledge_used: boolean;
  tools_used: string[];
  output_valid: boolean;
  output_errors: string[];
  entities: Record<string, string[]>;
  intent_confidence: number;
  intent_source_scores: Record<string, number>;
  request_id: string;
  memory_status?: "stored" | "degraded" | "unavailable" | "failed";
  memory_operation?: MemoryOperationReceipt;
}

export interface MemoryOperationReceipt {
  request_id: string;
  status: "WRITTEN" | "NOOP" | "PENDING" | "BUSY" | "INCOMPLETE" | "BLOCKED" | "FAILED" | "CAPACITY_EXCEEDED" | "DISABLED";
  covered_source_seq?: number;
  revision?: number;
  applied?: number;
  mutated?: boolean;
}

export interface ToolCallTrace {
  agent_type: RoutingAgentType;
  tool_name: string;
  tool_call_id: string;
  /** Anthropic/Python 侧使用的同义字段，保留以便跨运行时串联 trace。 */
  tool_use_id?: string;
  input: Record<string, unknown>;
  started_at?: string;
  finished_at?: string;
  output?: unknown;
  success: boolean;
  is_error: boolean;
  latency_ms: number;
  cached: boolean;
  reranked: boolean;
  error?: string;
  /** 专业 Agent 失败后由 General Agent 接管时，保留原调用轨迹并标记降级。 */
  fallback_used?: boolean;
}

export interface RequestTrace {
  model_id?: string;
  runtime_version?: string;
  fallback_used?: boolean;
  request_id: string;
  timestamp: string;
  user_id: string;
  conv_id: string;
  message: string;
  response: string;
  intent: Intent;
  intent_group: Intent;
  primary_agent: RoutingAgentType;
  supporting_agents: AgentType[];
  agent_types: RoutingAgentType[];
  tools_used: string[];
  skills_used?: string[];
  tool_calls: ToolCallTrace[];
  escalated: boolean;
  latency_ms: number;
  output_valid?: boolean;
  output_errors?: string[];
}

export interface MemoryMessage {
  role: "user" | "assistant" | "system";
  content: string;
  timestamp?: string;
  metadata?: Record<string, unknown>;
}

export interface MemoryContext {
  prompt_text: string;
  recent_messages: MemoryMessage[];
  relevant_history: string[];
  user_profile: Record<string, unknown>;
  summary: string;
  schema_version?: number;
  status?: "ready" | "degraded" | "unavailable";
  snapshot?: { fact_revision: number; context_revision: number; erase_epoch: number };
  blocks?: { facts: Array<Record<string, unknown>>; episodes: Array<Record<string, unknown>>; summary: unknown };
  routing_context?: string;
  budget?: { rendered_chars: number; context_truncated: boolean };
}

export interface KnowledgeResult {
  title: string;
  content: string;
  score: number;
  chunk?: number;
  fallback?: boolean;
}

export interface KnowledgeSearchResponse {
  query: string;
  results: KnowledgeResult[];
  reranked: boolean;
  cached?: boolean;
}

export type ChatStreamEvent =
  | { type: "agent_start"; agent_type: RoutingAgentType }
  | { type: "text_delta"; agent_type: RoutingAgentType; delta: string }
  | { type: "tool_start"; agent_type: RoutingAgentType; tool: string }
  | { type: "tool_end"; agent_type: RoutingAgentType; tool: string; is_error: boolean };

export type ChatEventSink = (event: ChatStreamEvent) => void | Promise<void>;
