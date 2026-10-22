import { assertModelBudget } from "./model-budget.js";
import { escapeMemoryData } from "./memory-context.js";
import { Agent } from "@earendil-works/pi-agent-core";
import type { AssistantMessage, Model } from "@earendil-works/pi-ai";
import { streamSimple as streamAnthropicSimple } from "@earendil-works/pi-ai/api/anthropic-messages";
import type { AppConfig } from "../config.js";
import type { Intent, IntentDecision, IntentEmbeddingCandidate, MemoryMessage, Urgency } from "../types.js";

// 所有允许输出的意图。Pi LLM 返回的 intent 必须属于这个集合，
// 否则会被视为无效结果并回退到规则结果。
const INTENTS = new Set<Intent>([
  "query", "complaint", "request", "greeting", "escalation",
  "technical", "billing", "account", "feedback",
  "order_status", "logistics", "refund", "invoice", "payment_issue",
  "account_security", "technical_login", "technical_crash", "human_handoff", "other",
]);

// 细粒度意图到兼容性大类的映射。
// 例如 refund 是细粒度意图，但它所属的 intent_group 是 billing。
const INTENT_GROUPS: Partial<Record<Intent, Intent>> = {
  order_status: "query",
  logistics: "query",
  refund: "billing",
  invoice: "billing",
  payment_issue: "billing",
  account_security: "account",
  technical_login: "technical",
  technical_crash: "technical",
  human_handoff: "escalation",
};

// 这些意图比 query、billing、technical 等通用意图更具体。
// 当 LLM 只给出宽泛意图时，后面会用规则命中的细粒度意图修正它。
const SPECIFIC_INTENTS = new Set<Intent>([
  "order_status", "logistics", "refund", "invoice", "payment_issue",
  "account_security", "technical_login", "technical_crash", "human_handoff",
]);

type IntentRule = { intent: Intent; confidence: number; words: string[] };

// 规则优先级较高，命中后通常可以直接返回。
// 同一条规则命中多个关键词时，matchRules() 会小幅提高置信度。
const SPECIFIC_RULES: IntentRule[] = [
  { intent: "technical_login", confidence: 0.96, words: ["无法登录", "登录失败", "登录报错", "401", "验证码"] },
  { intent: "technical_crash", confidence: 0.96, words: ["崩溃", "闪退", "500", "页面报错", "crash"] },
  { intent: "payment_issue", confidence: 0.96, words: ["重复扣款", "重复扣", "扣了两次", "多扣", "支付失败", "付款失败", "交易失败", "payment failed"] },
  { intent: "invoice", confidence: 0.96, words: ["发票", "抬头", "税号", "invoice"] },
  { intent: "refund", confidence: 0.96, words: ["退款", "退货", "refund", "return"] },
  { intent: "account_security", confidence: 0.95, words: ["账户被盗", "账号被盗", "异常登录", "重置密码", "两步验证", "账号安全"] },
  { intent: "order_status", confidence: 0.94, words: ["订单状态", "发货了吗", "处理到哪", "order status"] },
  { intent: "logistics", confidence: 0.94, words: ["物流", "快递", "包裹", "运单", "什么时候到", "还没到", "delivery", "shipping"] },
];

const GENERIC_RULES: IntentRule[] = [
  { intent: "escalation", confidence: 0.94, words: ["找经理", "supervisor", "escalate"] },
  { intent: "technical", confidence: 0.91, words: ["报错", "错误码", "error", "超时", "sdk", "api"] },
  { intent: "billing", confidence: 0.91, words: ["扣款", "扣费", "支付", "账单", "订阅", "续费", "billing", "payment"] },
  { intent: "order_status", confidence: 0.94, words: ["我的订单", "查订单", "订单列表", "查询订单", "查看订单", "查一下订单", "订单 ord-"] },
  { intent: "account", confidence: 0.92, words: ["修改邮箱", "邮箱", "注销账户", "注销账号", "关闭当前账号", "账户资料", "个人资料", "账户昵称", "绑定信息", "password"] },
  { intent: "complaint", confidence: 0.92, words: ["太差", "糟糕", "投诉", "等了很久", "不满意", "horrible"] },
  { intent: "feedback", confidence: 0.88, words: ["很棒", "满意", "好评", "谢谢你们"] },
  { intent: "request", confidence: 0.78, words: ["取消订单", "修改地址"] },
  { intent: "query", confidence: 0.72, words: ["怎么", "什么", "何时", "多久", "哪里", "status", "?", "？"] },
];

type RuleMatch = { intent: Intent; confidence: number; matched: string[] };

function activeRoutingText(message: string): string {
  return message.replace(/(?:不要|不用|不需要|不想|不必|没说要)[^，,。;；！？!?]{0,8}(?:人工客服|人工|真人|经理|主管)/g, "");
}

/** Match the requested action, not a polite prefix or a domain noun alone. */
function matchBoundaryRules(message: string): RuleMatch | null {
  // Remove only negated handoff/manager clauses. "不要机器人" remains a
  // positive human request; other clauses and business entities stay intact.
  const text = message;
  const tests: [Intent, number, RegExp][] = [
    ["escalation", 0.97, /(?:找|联系|让|交给|上报(?:到)?|升级(?:给|到)?|向|请由|希望由|转(?:给)?).{0,10}(?:经理|主管|负责人|管理层|管理人员|上级|更高级别客服)|升级处理|\b(?:supervisor|escalate)\b/i],
    ["human_handoff", 0.97, /(?:转|接|找|安排|需要|联系|换|让|要|和|与).{0,6}(?:人工|真人|客服人员)|(?:人工|真人).{0,6}(?:处理|接手|联系|沟通)|(?:不要|不想|别).{0,8}机器人|换人来答|^(?:人工客服|人工)$/],
    ["payment_issue", 0.96, /(?:付款|支付|交易).{0,8}(?:失败|未成功|没有成功|不成功)|(?:重复|多|两[次笔]|三[次笔]).{0,5}(?:扣款|扣费|扣钱)|扣.{0,6}(?:两[次笔遍]|三[次笔遍])|(?:钱扣了|扣款成功|已扣款|钱已经出去了).{0,20}(?:未支付|待支付|没付|未更新|没更新|没有更新)|(?:扣款|实扣|支付).{0,8}金额.{0,12}(?:不一致|对不上|多了)|(?:扣款|实扣).{0,8}(?:比|和).{0,10}(?:多|不一致)/],
    // Generic delivery policy is a query; shipment tracking remains logistics.
    ["query", 0.92, /(?:标准|普通)配送.{0,8}(?:需要|要|一般|几|多长)|(?:配送|服务|营业)时间.{0,8}(?:什么|何时|哪里|几点)|(?:了解|介绍|说明|查看|查询|哪些|什么|哪里).{0,10}(?:会员权益|服务范围|帮助中心)|(?:会员权益|服务范围|帮助中心).{0,8}(?:什么|哪些|哪里|入口)/],
  ];
  for (const [intent, confidence, pattern] of tests) {
    const match = text.match(pattern);
    if (match) return { intent, confidence, matched: [match[0]] };
  }
  return null;
}

function matchActionRule(message: string): RuleMatch | null {
  // Domain-specific requests (refund/invoice/account) have already matched.
  // "帮我看看营业时间" contains no operation and must remain a query.
  const action = message.match(/(?:取消|撤销|修改|更改|变更|更新|调整|提交|处理).{0,10}(?:订单|地址|收件|收货|联系信息|电话|资料|信息|变更|需求)|(?:帮我|请协助|请帮忙|麻烦|替我|需要你们)(?:一下|协助|帮忙)?(?:取消|撤销|修改|更改|变更|更新|调整|提交|处理)/);
  return action ? { intent: "request", confidence: 0.92, matched: [action[0]] } : null;
}

/** 将细粒度意图转换为对外兼容的大类。 */
function intentGroup(intent: Intent): Intent {
  return INTENT_GROUPS[intent] ?? intent;
}

function urgencyFor(message: string, intent: Intent): Urgency {
  const lower = message.toLowerCase();
  if (["紧急", "emergency", "urgent", "asap", "立刻"].some((word) => lower.includes(word))) return "critical";
  if (["今天", "马上", "尽快", "hurry", "now"].some((word) => lower.includes(word))) return "high";
  if (["这周", "soon", "快点"].some((word) => lower.includes(word))) return "medium";
  if (intentGroup(intent) === "escalation") return "high";
  if (intent === "complaint") return "medium";
  return "low";
}

function unique(values: string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))];
}

/**
 * 从用户原始消息中提取结构化实体。
 *
 * 输入：一条用户消息，例如“订单号 A-1234 重复扣款 50 元，返回 401”。
 * 输出：供路由和 Agent 使用的实体字典，例如：
 * {
 *   order_id: ["A-1234"],
 *   amount: ["50 元"],
 *   error_code: ["401"],
 *   date: [],
 *   product: []
 * }
 *
 * 这是确定性规则提取，不需要额外调用 LLM。
 */
export function extractEntities(message: string): Record<string, string[]> {
  const matches = (pattern: RegExp) => unique([...message.matchAll(pattern)].map((match) => match[1] ?? match[0] ?? ""));
  const orderIds = matches(/(?:订单号?|order(?:_id)?|#)\s*[:：#]?\s*([A-Za-z0-9_-]{4,32})/gi);
  const errorCodes = matches(/\b([45]\d{2}|[A-Z][A-Z0-9_-]{2,16})\b/g)
    .filter((value) => !orderIds.includes(value));
  return {
    order_id: orderIds,
    product: [],
    date: matches(/(今天|明天|昨天|本周|这周|下周|\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)/g),
    amount: matches(/((?:¥|￥)\s*\d+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?\s*(?:元|块|rmb|cny|usd|美元))/gi),
    error_code: errorCodes,
  };
}

function matchRules(message: string, rules: IntentRule[]): { intent: Intent; confidence: number; matched: string[] } | null {
  const lower = message.toLowerCase();
  for (const rule of rules) {
    const matched = rule.words.filter((word) => lower.includes(word));
    if (matched.length) {
      return {
        intent: rule.intent,
        confidence: Math.min(0.99, rule.confidence + (matched.length - 1) * 0.02),
        matched,
      };
    }
  }
  return null;
}

/**
 * 只使用规则完成一次意图识别。
 *
 * 输入：用户当前消息 message。
 * 输出：完整的 IntentDecision，而不仅仅是一个 intent 字符串。
 * 其中：
 * - intent：最终细粒度意图
 * - intent_group：兼容性大类
 * - confidence：规则置信度
 * - entities：从原消息提取的订单号、金额、错误码等
 * - source_scores：记录各识别来源的分数；Pi 版没有 Embedding 路径
 *
 * 规则顺序是：问候语 -> 细粒度规则 -> 通用规则 -> other。
 */
export function recognizeByRules(message: string): IntentDecision {
  const text = message.trim();
  const lower = text.toLowerCase();
  let matched: { intent: Intent; confidence: number; matched: string[] } | null = null;
  if (["你好", "您好", "嗨", "hi", "hello", "hey", "早上好", "晚上好"].includes(lower)) {
    matched = { intent: "greeting", confidence: 0.99, matched: [lower] };
  } else {
    const active = activeRoutingText(text);
    const boundary = matchBoundaryRules(active);
    const specific = matchRules(active, SPECIFIC_RULES);
    // Preserve established primary-agent precedence for compound technical +
    // payment problems. Handoff requests take priority; policy queries only
    // refine the broad logistics noun rule.
    const preferBoundary = boundary && (!specific
      || ["escalation", "human_handoff"].includes(boundary.intent)
      || (boundary.intent === "payment_issue" && ["payment_issue", "order_status", "logistics"].includes(specific.intent))
      || (boundary.intent === "query" && specific.intent === "logistics"));
    matched = (preferBoundary ? boundary : specific)
      ?? matchRules(active, GENERIC_RULES.filter((rule) => rule.intent === "account"))
      ?? matchActionRule(active) ?? matchRules(active, GENERIC_RULES);
  }
  const intent = matched?.intent ?? "other";
  const confidence = matched?.confidence ?? 0.3;
  return {
    intent,
    intent_group: intentGroup(intent),
    confidence,
    urgency: urgencyFor(text, intent),
    reasoning: matched ? `规则命中: ${matched.matched.join(", ")}` : "无明确规则命中",
    entities: extractEntities(text),
    source_scores: { llm: 0, embedding: 0, pattern: confidence },
  };
}

function assistantText(messages: unknown[]): string {
  // Pi Agent 的消息列表包含多种消息；这里只取最后一条 assistant 消息中的文本部分，
  // 用作意图 LLM 的 JSON 原始输出。
  const assistants = messages.filter(
    (message): message is AssistantMessage => typeof message === "object" && message !== null && (message as AssistantMessage).role === "assistant",
  );
  return assistants.at(-1)?.content
    .filter((part) => part.type === "text")
    .map((part) => part.type === "text" ? part.text : "")
    .join("") ?? "";
}

export class IntentRouter {
  constructor(
    private readonly appConfig: AppConfig,
    private readonly embeddingSearch?: (query: string, topK: number) => Promise<IntentEmbeddingCandidate[]>,
  ) {}

  /**
   * 对外的完整意图识别入口。
   *
   * 输入：
   * - message：当前用户消息
   * - history：Python MemoryContext.recent_messages，即最近对话消息
   *
   * 输出：Promise<IntentDecision>。
   * 数据流分为三段：
   * 1. message -> recognizeByRules(message) -> 规则 IntentDecision。
   * 2. 关闭 LLM 路由、未配置 API Key，或规则置信度 >= 0.9 时，直接返回规则结果。
   * 3. 否则将最近 4 条 history + message 交给无工具 Pi Router Agent，解析模型返回的
   *    intent、confidence、reasoning，再与规则结果融合并补齐 intent_group、urgency、
   *    entities、source_scores，最终返回 IntentDecision。
   *
   * 本流程不使用意图缓存，每次调用都会先重新执行规则识别。LLM 调用失败、JSON 无效
   * 或模型返回非法意图时，直接回退并返回本次生成的规则结果。
   */
  async recognize(message: string, history: MemoryMessage[], routingContext?: string, signal?: AbortSignal): Promise<IntentDecision> {
    const rule = recognizeByRules(message);
    if (rule.confidence >= 0.9) {
      // 规则置信度 >= 0.9 时跳过 Embedding 和 Pi LLM，避免为明确问题增加调用。
      return rule;
    }

    // 规则不明确时先做 Embedding 召回；Embedding 只提供候选，不直接覆盖规则结果。
    let embeddingCandidates: IntentEmbeddingCandidate[] = [];
    if (this.appConfig.useEmbeddingIntent && this.embeddingSearch) {
      try {
        embeddingCandidates = await this.embeddingSearch(message, this.appConfig.embeddingIntentTopK);
      } catch (error) {
        console.warn("Embedding 意图召回失败，继续使用规则/LLM:", error instanceof Error ? error.message : error);
      }
    }
    const embeddingTop = embeddingCandidates[0];
    const embeddingScore = embeddingTop?.score ?? 0;

    // 未配置 LLM 时，允许高于阈值的 Embedding 候选作为低置信度问题的兜底。
    if (!this.appConfig.useLlmRouting || !this.appConfig.apiKey) {
      if (embeddingTop && embeddingScore >= this.appConfig.embeddingIntentThreshold) {
        return {
          intent: embeddingTop.intent,
          intent_group: intentGroup(embeddingTop.intent),
          confidence: embeddingScore,
          urgency: urgencyFor(message, embeddingTop.intent),
          reasoning: `Embedding 候选: ${embeddingTop.example}`,
          entities: rule.entities,
          source_scores: { llm: 0, embedding: embeddingScore, pattern: rule.confidence },
        };
      }
      return rule;
    }

    // 这是一个只负责分类的临时 Agent：无工具、空消息列表，
    // 不参与业务回答，也不会访问知识库。
    const agent = new Agent({
      initialState: {
        model: this.appConfig.model as Model<any>,
        thinkingLevel: "off",
        systemPrompt: [
          "你是 LumenMind 的只读意图路由器。只输出一个 JSON 对象，不要解释。",
          `格式: {"intent":"${[...INTENTS].join("|")}","confidence":0到1,"reasoning":"短句"}`,
          "优先返回细粒度意图：退款 refund、发票 invoice、支付异常 payment_issue、登录故障 technical_login、崩溃 technical_crash、物流 logistics、转人工 human_handoff。",
          "分类边界：escalation 是向经理、主管、负责人、上级升级或申诉；human_handoff 是普通人工/真人接手。明确要求主管时选 escalation，即使同时提到人工。",
          "payment_issue 是付款失败、重复扣款、实扣金额与订单不一致、已扣款但订单未支付；billing 是账单、订阅价格、收费组成等一般问题。",
          "先判断具体业务领域，再考虑通用 query/request。order_status 是订单当前状态、处理进度、是否发货/完成；logistics 是具体包裹、运单、运输与收货进度。account 是账户资料、昵称、绑定信息、邮箱的查询或变更；account_security 是被盗、异常登录、密码泄露和安全设置。",
          "technical 是接口、网络、SDK、性能和未知技术错误；technical_login 是登录/认证失败；technical_crash 是崩溃、闪退、500。refund 是退货/退钱/退款时效；invoice 是发票/开票/税号。",
          "complaint 是对服务不满、负面评价、投诉；feedback 是表扬、感谢、正面评价；greeting 是问候寒暄；other 是没有说明具体目的、无法判断的情况，不要强行猜成 query。",
          "仅在没有更具体领域时，query 是询问信息、规则、位置或通用时效；request 是要求实际办理取消、修改、提交等操作。‘请问/麻烦帮我看看/该怎么处理/能帮忙确认吗’仅是附加表达，不能覆盖主要诉求或据此判 request/query。",
          "标准配送时效属于 query，具体包裹运输、运单跟踪属于 logistics；否定的转人工或升级请求不能当成用户要求交接。只按用户实际目的判断，不按情绪或单个关键词。",
          embeddingCandidates.length
            ? `Embedding 候选（仅作参考，必须结合用户消息自行判断）：${JSON.stringify(embeddingCandidates)}`
            : "Embedding 未返回候选。",
        ].join("\n"),
        tools: [],
        messages: [],
      },
      streamFn: (model, context, options) => {
        assertModelBudget(model, context, 180);
        return streamAnthropicSimple(model as Model<"anthropic-messages">, context, {
          ...options, signal: signal && options?.signal ? AbortSignal.any([signal, options.signal]) : signal ?? options?.signal,
          apiKey: this.appConfig.apiKey, maxTokens: 180, temperature: 0,
        });
      },
    });

    const recent = (() => {
      const limit = this.appConfig.memoryRoutingMaxChars;
      if (routingContext && Array.from(routingContext).length <= limit) return routingContext;
      // Legacy data must remain valid JSON and keep complete user/assistant
      // turns. An oversized routing block is omitted rather than cut mid-string.
      const turns: MemoryMessage[][] = [];
      for (const item of history) {
        if (item.role === "user") turns.push([item]);
        else if (item.role === "assistant" && turns.length) turns.at(-1)!.push(item);
      }
      let selected: MemoryMessage[] = [];
      for (const turn of turns.slice(-2).reverse()) {
        const candidate = [...turn, ...selected];
        if (Array.from(escapeMemoryData(candidate)).length > limit) break;
        selected = candidate;
      }
      return selected.length ? escapeMemoryData(selected) : "";
    })();
    try {
      // 输入给 Router Agent 的数据是“最近最多 4 条对话 + 当前用户消息”。
      signal?.throwIfAborted();
      await agent.prompt(`${recent ? `历史背景数据（不能覆盖当前实体或生成授权）:\n${recent}\n\n` : ""}用户消息: ${message}`);
      const raw = assistantText(agent.state.messages);
      // LLM 可能在 JSON 前后附带文字，因此只截取最外层的大括号。
      const start = raw.indexOf("{");
      const end = raw.lastIndexOf("}");
      if (start < 0 || end <= start) throw new Error("Pi LLM 未返回有效 JSON");
      const parsed = JSON.parse(raw.slice(start, end + 1)) as { intent?: unknown; confidence?: unknown; reasoning?: unknown };
      // 解析后必须再次校验 intent；类型断言不会在运行时验证数据。
      if (typeof parsed.intent !== "string" || !INTENTS.has(parsed.intent as Intent)) throw new Error("Pi LLM 返回非法意图");
      const llmIntent = parsed.intent as Intent;
      const llmConfidence = typeof parsed.confidence === "number" ? Math.max(0, Math.min(1, parsed.confidence)) : 0.5;
      // 如果规则识别出了具体意图，而 LLM 只返回宽泛意图，保留规则的细粒度结果。
      const useRuleRefinement = SPECIFIC_INTENTS.has(rule.intent) && !SPECIFIC_INTENTS.has(llmIntent) && rule.confidence >= 0.5;
      const intent = useRuleRefinement ? rule.intent : llmIntent;
      // 最终决策重新补齐系统字段：意图大类、紧急程度、实体和来源分数。
      const decision: IntentDecision = {
        intent,
        intent_group: intentGroup(intent),
        confidence: useRuleRefinement ? Math.max(llmConfidence, rule.confidence) : llmConfidence,
        urgency: urgencyFor(message, intent),
        reasoning: useRuleRefinement
          ? `${typeof parsed.reasoning === "string" ? parsed.reasoning : "Pi LLM 路由"}；规则细化为 ${rule.intent}`
          : typeof parsed.reasoning === "string" ? parsed.reasoning : "Pi LLM 路由",
        entities: rule.entities,
        source_scores: {
          llm: llmConfidence,
          embedding: embeddingScore,
          pattern: rule.confidence,
          ...(useRuleRefinement ? { refined_by_pattern: rule.confidence } : {}),
        },
      };
      return decision;
    } catch (error) {
      signal?.throwIfAborted();
      // LLM 不可用、返回非法 JSON 或网络失败时，整个路由回退到规则决策。
      console.warn("Pi 意图路由失败，使用规则结果:", error instanceof Error ? error.message : error);
      // LLM 不可用时，只有超过阈值的 Embedding 候选才允许作为兜底，避免相似度过低导致误路由。
      if (embeddingTop && embeddingScore >= this.appConfig.embeddingIntentThreshold) {
        return {
          intent: embeddingTop.intent,
          intent_group: intentGroup(embeddingTop.intent),
          confidence: embeddingScore,
          urgency: urgencyFor(message, embeddingTop.intent),
          reasoning: `Embedding 候选回退: ${embeddingTop.example}`,
          entities: rule.entities,
          source_scores: { llm: 0, embedding: embeddingScore, pattern: rule.confidence },
        };
      }
      return rule;
    }
  }
}
