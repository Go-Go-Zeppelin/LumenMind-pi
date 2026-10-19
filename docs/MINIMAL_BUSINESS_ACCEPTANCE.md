# 最小订单与账单查询验收记录

验收日期：2026-10-08（Asia/Shanghai）。仅改动 LumenMind-pi，未新增运行依赖、数据库服务、支付操作或前端页面。

## 自动化检查

- 修改前基线：TypeScript 类型检查通过、34 项测试通过；Python 31 项测试通过。
- 修改后：TypeScript 类型检查、56 项测试、构建全部通过；Python 51 项测试通过。
- Python 仅有既存 Starlette/AnyIO 弃用警告，没有失败。
- `docker compose config --quiet` 通过；`docker compose up -d --build python-infra pi-agent` 成功，两服务健康。
- Python 业务测试使用临时数据库和独立 app，不启动 Redis/Chroma。TypeScript 测试不调用模型。
- 交错执行两个用户、关闭知识检索、Billing 异常回退 General、监控降级均验证身份与工具结果保留；Technical 不提供业务查询工具，健康白名单与实际角色工具一致。

## Docker API 与持久化

- 15 项直接 HTTP 检查通过：四个查询接口、u1001 三条订单/账单、按订单过滤、u1002 单条记录、越权/不存在统一 404、未知用户空列表、缺失用户和非法 limit 422。
- API 和 trace 返回均保留整数分及 `source=demo_business_db`；ORD-1001-001 为 12900 分、shipped，BILL-1001-002 为 5900 分、unpaid。
- 重启 `python-infra` 后，数据库仍为 4 条订单和 4 条账单；所有字段及 SQLite 文件 SHA256 与重启前相同。再次从 HTTP 查询 u1001 仍为各 3 条，seed 未重复或覆盖数据。
- Redis/Chroma 数据卷、既有 `.env` 保留；业务库位于挂载的 `data/business/business.sqlite3`。

## 真实模型聊天与工具证据

使用 Docker 中实际配置的模型 `deepseek-v4-flash`，Pi SDK `0.82.1`，共 11 次真实模型聊天。不是 mock 验收。

下表每次响应均包含相应 `tools_used`，对应 trace 具有成功工具调用、实际数据库返回值以及正确 `user_id`；`knowledge_used=false`。完整响应及 trace 快照保存于 [MINIMAL_BUSINESS_ACCEPTANCE.json](MINIMAL_BUSINESS_ACCEPTANCE.json)。

| 入口 | 用户 | 提问 | 工具 | request_id |
|---|---|---|---|---|
| Python :8000 | u1001 | 查一下我的订单 | list_orders | 96d96de0-0c77-4595-b79a-06dd29d5abad |
| Python :8000 | u1001 | 订单 ORD-1001-001 发货了吗？ | get_order | 1a8dc783-cb3d-4bba-bdbb-9e19c8861fdb |
| Python :8000 | u1001 | 查看我的账单 | list_bills | 199001de-f11c-40db-9130-eae2f808304a |
| Python :8000 | u1001 | 订单 ORD-1001-001 的账单 | list_bills | 625f29ad-f5b7-40a7-90ac-740b0b37bb2f |
| Python :8000 | u1001 | 查询账单 BILL-1001-002 | get_bill | 04d24896-4049-43f2-aed6-785d13a4cb06 |
| Python :8000 | u1001 | 查询订单 ORD-9999-999 | get_order | c1dc6b75-dd44-4fad-b671-9dfb2bbf6487 |
| Python :8000 | u1001 | 查询订单 ORD-1002-001 | get_order | 4eee10e6-69ea-427f-9b20-fb5017540751 |
| Pi :3000 | u1002 | 查一下我的订单 | list_orders | de2e9520-dcca-4d8e-b5c4-390cce2de923 |
| Pi :3000 | unknown | 查看我的账单 | list_bills | 1490cfa2-a0e5-4314-aa63-90e94772d1d3 |
| Pi :3000 | u1001 | 查一下我的订单和账单 | list_orders, list_bills | 43cd505d-2ace-418b-8c13-842b5921689a |
| Pi SSE :3000 | u1001 | 订单 ORD-1001-001 发货了吗？ | get_order | c6bf6926-5526-4393-a9c2-c7ff1b5f73f8 |

实际结果：列表无需补充编号；已发货订单返回无线键盘、129.00 元和 shipped；三条账单区分 paid/unpaid；按订单过滤仅返回 BILL-1001-001；账单详情为鼠标订单的 59.00 元未支付记录。不存在与越权订单均得到 found=false，未暴露 u1002 商品或金额。u1002 原生聊天仅取得自己的 199.00 元机械键盘订单；未知用户查询为空。复合问题同时调用 list_orders 与 list_bills。SSE 出现 agent_start、tool_start、tool_end 和 done，并可用 done 的 request_id 获取 get_order 返回值。

示例：发货查询的工具返回（request_id: `1a8dc783-cb3d-4bba-bdbb-9e19c8861fdb`）：

```json
{
  "user_id": "u1001",
  "tool_name": "get_order",
  "input": {
    "order_id": "ORD-1001-001"
  },
  "success": true,
  "is_error": false,
  "output": {
    "item": {
      "order_id": "ORD-1001-001",
      "user_id": "u1001",
      "product_name": "无线键盘",
      "quantity": 1,
      "total_amount_cents": 12900,
      "currency": "CNY",
      "status": "shipped",
      "created_at": "2026-10-01T10:00:00+08:00"
    },
    "source": "demo_business_db"
  }
}
```

## 使用与边界

- 使用说明、启动命令、演示用户和接口见 [README](../README.md#最小订单与账单演示)。
- user_id 是演示身份选择，SQL 归属过滤不构成登录鉴权；模型 schema 不包含用户参数，代码绑定每次 run 的身份。
- 工具 404 是正常 found=false；503/超时保持工具失败并在 trace 标记 is_error=true。异常语义和故障降级由离线回归测试验证，本次没有破坏 Docker 数据库或向真实模型注入故障。
- trace 仍是 Pi 内存中的最近请求，重启 Pi 会清空；本 JSON 保存了本次验收快照。重启 Python 不清空 Pi trace。
- 本期无物流轨迹、预计送达时间、支付、退款、开票或下单。演示支付状态不代表真实渠道流水。
- 模型部分回复附带了额外建议；既有 escalated 字段按回复中的人工措辞判定，因此首个发货查询虽成功，escalated=true。这不代表系统创建了人工工单，本次保持该既有契约。
