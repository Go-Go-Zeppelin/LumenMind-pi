import { describe, expect, it } from "vitest";
import { makeRoutingDecision, needsClarification, shouldUseKnowledge } from "../src/agents/orchestrator.js";
import { IntentRouter, extractEntities, recognizeByRules } from "../src/agents/intent-router.js";
import { config } from "../src/config.js";

describe("LumenMind 意图路由", () => {
  it.each([
    ["我的订单什么时候到？", "logistics"],
    ["帮我取消订单", "request"],
    ["你们服务太差了！", "complaint"],
    ["应用一直报500错误", "technical_crash"],
    ["为什么扣了两次款？", "payment_issue"],
    ["我要投诉，转人工！", "human_handoff"],
    ["你好", "greeting"],
    ["修改我的邮箱地址", "account"],
    ["帮我开发票", "invoice"],
    ["退款多久到账？", "refund"],
    ["登录一直报401", "technical_login"],
  ])("将 %s 识别为 %s", (message, expected) => {
    expect(recognizeByRules(message).intent).toBe(expected);
  });

  it("为复合问题返回主 Agent、辅助 Agent 和可解释路由", () => {
    const intent = recognizeByRules("登录报错 401，而且还被重复扣款 50 元");
    const decision = makeRoutingDecision("登录报错 401，而且还被重复扣款 50 元", intent);
    expect(decision.primaryAgent).toBe("technical");
    expect(decision.supportingAgents).toContain("billing");
    expect(decision.reason).toContain("scores=[");
  });

  it.each(["查一下我的订单", "查订单", "订单列表", "查询订单 ORD-9999-999", "订单 ORD-1001-001 发货了吗"])("明确订单查询 %s 进入 General，无需低置信度澄清", (message) => {
    const intent = recognizeByRules(message);
    expect(intent.intent).toBe("order_status");
    expect(needsClarification(message, intent)).toBe(false);
    expect(makeRoutingDecision(message, intent).primaryAgent).toBe("general");
  });

  it.each(["查看我的账单", "订单 ORD-1001-001 的账单", "查询账单 BILL-1001-002"])("明确账单查询 %s 进入 Billing", (message) => {
    const intent = recognizeByRules(message);
    expect(needsClarification(message, intent)).toBe(false);
    expect(makeRoutingDecision(message, intent).primaryAgent).toBe("billing");
  });

  it("提取订单号、金额、日期和错误码实体", () => {
    expect(extractEntities("订单号 #A12345 今天支付了 50 元，但登录报 401")).toMatchObject({
      order_id: ["A12345"],
      date: ["今天"],
      amount: ["50 元"],
      error_code: ["401"],
    });
  });

  it("细粒度意图保留宽泛 intent_group", () => {
    expect(recognizeByRules("退款多久到账？")).toMatchObject({ intent: "refund", intent_group: "billing" });
  });

  it("业务 Agent 始终获得共享知识工具，由模型决定是否调用", () => {
    expect(shouldUseKnowledge("refund")).toBe(true);
    expect(shouldUseKnowledge("technical_login")).toBe(true);
    // 工具是业务 Agent 的共享能力；是否实际调用由模型决定。
    expect(shouldUseKnowledge("greeting")).toBe(true);
    expect(shouldUseKnowledge("human_handoff")).toBe(true);
    expect(shouldUseKnowledge("other")).toBe(true);
  });

  it("低置信度 OTHER 会先澄清，短寒暄不会误触发", () => {
    const other = recognizeByRules("这个情况不太对");
    expect(needsClarification("这个情况不太对", other)).toBe(true);
    expect(needsClarification("嗯", { ...other, confidence: 0.2 })).toBe(false);
  });

  it("规则低置信度时使用 Embedding 候选兜底", async () => {
    const embeddingSearch = async () => [{ intent: "refund" as const, score: 0.88, example: "退款多久到账" }];
    const router = new IntentRouter({
      ...config,
      apiKey: "",
      useLlmRouting: false,
      useEmbeddingIntent: true,
      embeddingIntentThreshold: 0.55,
    }, embeddingSearch);
    const result = await router.recognize("这笔钱什么时候能回来", []);
    expect(result).toMatchObject({
      intent: "refund",
      intent_group: "billing",
      confidence: 0.88,
      source_scores: { embedding: 0.88 },
    });
  });

  it("明确规则命中时不调用 Embedding", async () => {
    const embeddingSearch = async () => {
      throw new Error("should not be called");
    };
    const router = new IntentRouter({
      ...config,
      apiKey: "",
      useLlmRouting: false,
      useEmbeddingIntent: true,
    }, embeddingSearch);
    await expect(router.recognize("退款多久到账？", [])).resolves.toMatchObject({ intent: "refund" });
  });
});
