import type { AgentTool } from "@earendil-works/pi-agent-core";
import { Type } from "@earendil-works/pi-ai";
import { InfrastructureError, type InfrastructureClient } from "../clients/infrastructure.js";
import type { AgentType } from "../types.js";

export const BUSINESS_TOOL_NAMES = ["list_orders", "get_order", "list_bills", "get_bill"] as const;

export function businessToolNames(agentType: AgentType): readonly string[] {
  return agentType === "technical" ? [] : BUSINESS_TOOL_NAMES;
}

/** Bound to one run's immutable identity, never stored on a pooled executor. */
export function createBusinessTools(client: InfrastructureClient, userId: string): AgentTool[] {
  if (!userId.trim()) throw new Error("业务查询需要本次 run 的 userId");
  const limit = Type.Optional(Type.Integer({ minimum: 1, maximum: 20, default: 10 }));
  const make = (
    name: string, description: string, properties: Record<string, unknown>,
    query: (params: Record<string, unknown>, signal?: AbortSignal) => Promise<unknown>,
    detail = false,
  ): AgentTool => ({
    name,
    label: description,
    description: `${description}。只读查询当前用户的演示业务数据，金额为整数分，不能执行支付、退款或开票。`,
    parameters: Type.Object(properties as never, { additionalProperties: false }),
    async execute(_id, params, signal) {
      let result: unknown;
      try {
        result = await query(params as Record<string, unknown>, signal);
      } catch (error) {
        if (detail && error instanceof InfrastructureError && error.status === 404) {
          result = { found: false, source: "demo_business_db" };
        } else {
          // Throw so Pi reports tool_execution_end.isError=true, rather than an empty result.
          throw new Error("business_unavailable: 查询服务暂时不可用", { cause: error });
        }
      }
      return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }], details: result };
    },
  });
  return [
    make("list_orders", "查询我的最近订单列表", { limit },
      (params, signal) => client.listOrders(userId, params.limit as number | undefined, signal)),
    make("get_order", "查询指定订单详情", { order_id: Type.String({ minLength: 1 }) },
      (params, signal) => client.getOrder(userId, params.order_id as string, signal), true),
    make("list_bills", "查询我的最近账单或指定订单的账单", { order_id: Type.Optional(Type.String({ minLength: 1 })), limit },
      (params, signal) => client.listBills(userId, params.order_id as string | undefined, params.limit as number | undefined, signal)),
    make("get_bill", "查询指定账单详情", { bill_id: Type.String({ minLength: 1 }) },
      (params, signal) => client.getBill(userId, params.bill_id as string, signal), true),
  ];
}
