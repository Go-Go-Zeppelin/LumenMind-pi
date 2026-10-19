import type { AgentTool } from "@earendil-works/pi-agent-core";
import { Type } from "@earendil-works/pi-ai";
import type { AgentType } from "../types.js";

const text = (value: unknown) => typeof value === "string" ? value : JSON.stringify(value);
const tool = (name: string, label: string, description: string, properties: Record<string, unknown>, handler: (params: Record<string, unknown>) => Record<string, unknown>): AgentTool => ({
  name,
  label,
  description,
  parameters: Type.Object(properties as never),
  async execute(_toolCallId, params) {
    const result = handler(params as Record<string, unknown>);
    return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }], details: result };
  },
});

export function roleTools(agentType: AgentType): AgentTool[] {
  if (agentType === "technical") {
    return [
      tool("lookup_error_code", "查询错误码", "解释常见 HTTP/API 错误码，只返回排障线索。", { error_code: Type.String({ minLength: 3 }) }, (params) => {
        const code = text(params.error_code);
        const meanings: Record<string, string> = { "401": "身份验证失败或凭证过期", "403": "权限不足", "404": "资源不存在", "500": "服务端内部错误" };
        return { success: true, error_code: code, meaning: meanings[code] || "需要结合服务日志进一步确认", safe_next_step: "检查请求上下文和相关日志" };
      }),
      tool("build_diagnostic_plan", "生成排障计划", "根据错误码和症状生成低风险、可验证的排障步骤。", { symptom: Type.String({ minLength: 1 }), error_code: Type.Optional(Type.String()) }, (params) => ({
        success: true,
        steps: ["记录复现时间和完整错误响应", "确认客户端版本与接口地址", "在不暴露凭证的前提下收集日志"],
        symptom: text(params.symptom),
        error_code: text(params.error_code || "未提供"),
      })),
    ];
  }
  if (agentType === "billing") {
    return [
      tool("check_billing_fields", "核验账单字段", "检查退款、重复扣款和发票问题是否提供了必要核验字段。", { order_id: Type.Optional(Type.String()), amount: Type.Optional(Type.String()), payment_time: Type.Optional(Type.String()) }, (params) => ({
        success: true,
        missing_fields: ["order_id", "amount", "payment_time"].filter((field) => !params[field]),
        reminder: "不要索取完整银行卡号、支付密码或验证码",
      })),
      tool("compare_amounts", "比较金额线索", "只比较用户提供的金额文本，不查询真实账单。", { first_amount: Type.String({ minLength: 1 }), second_amount: Type.String({ minLength: 1 }) }, (params) => ({
        success: true,
        same_text: text(params.first_amount) === text(params.second_amount),
        note: "金额是否真实扣款仍需后台或人工核验",
      })),
    ];
  }
  return [
    tool("inspect_request_context", "检查请求上下文", "整理当前消息中的问题类型和已提供线索。", { message: Type.String({ minLength: 1 }) }, (params) => ({ success: true, message: text(params.message), next: "确认用户想解决的问题和关键字段" })),
    tool("suggest_required_fields", "建议必要字段", "根据场景建议用户补充信息，但不索取敏感凭证。", { category: Type.String({ minLength: 1 }) }, (params) => ({ success: true, category: text(params.category), required_fields: ["问题发生时间", "可复现步骤", "相关订单或版本线索（如适用）"] })),
  ];
}
