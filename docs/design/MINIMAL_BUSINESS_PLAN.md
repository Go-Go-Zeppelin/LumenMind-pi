# LumenMind-pi 最小订单与账单查询工程计划

本计划可直接交给另一个 agent 实施。目标是在现有聊天界面中查询 SQLite 中实际持久化的演示订单和账单，形成“用户提问 → Agent 调用工具 → Python 查询数据库 → 返回结果”的完整链路。

## 1. 范围与技术选择

- 仅改动 `LumenMind-pi`，复用现有 TypeScript/Pi 和 Python/FastAPI 服务。
- 使用 Python 标准库 `sqlite3`，独立业务库路径为 `/app/data/business/business.sqlite3`。
- 复用 Compose 已有的 `./data:/app/data` 挂载，不增加 MySQL/PostgreSQL 容器，不增加 ORM。
- 首期只有订单列表、订单详情、账单列表、账单详情四项只读能力。
- 订单和账单通过幂等初始化写入固定演示数据；本期不实现用户下单、付款、退款、开票、后台录入页面或登录系统。
- Redis、ChromaDB 继续承担现有记忆和知识检索职责，业务记录不写入向量库。
- 当前前端默认 `user_id=u1001`，演示数据必须覆盖这个用户；前端无需修改。
- 文档和业务查询回复应说明这是演示业务数据，不能把演示记录描述成真实支付渠道记录。

完成标准：以 `u1001` 身份发送“查一下我的订单”“订单 ORD-1001-001 发货了吗”“查看我的账单”“订单 ORD-1001-001 的账单”，均能从业务库取得结果，并能在请求 trace 中看到对应工具及其返回值。

## 2. 当前代码中必须注意的接入点

- `python-service/api/main.py`：现有 FastAPI 应用和启动生命周期；挂载业务路由、初始化数据库。
- `agent-service/src/clients/infrastructure.ts`：Pi 访问 Python 的 HTTP 客户端；添加四个业务查询方法，复用超时与错误处理。
- `agent-service/src/agents/orchestrator.ts`：已经获得标准化后的 `userId`，但目前没有把它传入普通 Agent 的执行输入。
- `agent-service/src/agents/executor.ts`：`AgentRunInput` 目前没有 `userId`；需要增加，并在每次 `run()` 时创建绑定本次用户的业务工具。
- `agent-service/src/tools/agent-tools.ts`：当前账单工具只检查字段和比较文本，应保留；新增查询工具单独放在 `business-tools.ts`。
- `agent-service/src/agents/profiles.ts`、`prompts.ts`、`workflow.ts` 和相关 Skills：现有描述默认真实记录需要后台核验，应更新为允许通过新只读工具核验演示记录。
- 当前 Docker 里的 Agent 构建产物落后于本地源码。实施后必须重新构建 Python 和 Pi 服务；仅重启旧容器不会加载源码变更。

## 3. 数据模型：只建两张表

所有时间保存为带时区的 ISO 8601 字符串；所有金额保存为整数分，展示时转换为元。首期货币固定为 `CNY`。

### orders

| 字段 | 类型/约束 | 含义 |
|---|---|---|
| order_id | TEXT PRIMARY KEY | 订单编号，例如 ORD-1001-001 |
| user_id | TEXT NOT NULL | 订单所属用户 |
| product_name | TEXT NOT NULL | 商品名称；一个订单只含一种商品 |
| quantity | INTEGER NOT NULL，> 0 | 数量 |
| total_amount_cents | INTEGER NOT NULL，>= 0 | 订单总额，单位分 |
| currency | TEXT NOT NULL，默认 CNY | 货币 |
| status | TEXT NOT NULL | pending_payment / processing / shipped / completed / cancelled |
| created_at | TEXT NOT NULL | 创建时间 |

### bills

| 字段 | 类型/约束 | 含义 |
|---|---|---|
| bill_id | TEXT PRIMARY KEY | 账单编号，例如 BILL-1001-001 |
| order_id | TEXT NOT NULL，关联 orders.order_id | 对应订单 |
| amount_cents | INTEGER NOT NULL，>= 0 | 账单金额，单位分 |
| currency | TEXT NOT NULL，默认 CNY | 货币 |
| status | TEXT NOT NULL | unpaid / paid |
| payment_method | TEXT，可空 | 演示支付方式，例如 alipay |
| paid_at | TEXT，可空 | unpaid 时为空 |
| created_at | TEXT NOT NULL | 创建时间 |

账单的用户归属通过 JOIN orders 获取，不在两张表重复维护 user_id。本期每个演示订单对应一张账单；不实现拆单、分期或退款流水。

必要约束：每个连接启用外键，使用参数化 SQL；订单查询始终包含 user_id 条件，账单查询始终 JOIN 订单并过滤 user_id。为 orders.user_id 和 bills.order_id 创建索引。

## 4. 初始化与固定演示数据

- 启动时创建目录、执行 `CREATE TABLE IF NOT EXISTS`；演示数据仅在 `BUSINESS_SEED_DEMO=true` 时写入。
- 使用固定主键、事务和 `INSERT ... ON CONFLICT DO NOTHING` 或等价方式保证幂等。重复启动不能重复插入、覆盖已有行或清空数据库。
- 不自动把 `anonymous`、`demo` 或未知用户映射为 u1001。没有数据的用户应得到空列表。
- 固定以下验收数据，时间可选择固定在 2026-10-01 至 2026-10-03：

| 用户 | 订单 | 商品 | 金额 | 订单状态 | 账单 | 账单状态 |
|---|---|---|---|---|---|---|
| u1001 | ORD-1001-001 | 无线键盘 | 129.00 元 | shipped | BILL-1001-001 | paid |
| u1001 | ORD-1001-002 | 鼠标 | 59.00 元 | pending_payment | BILL-1001-002 | unpaid |
| u1001 | ORD-1001-003 | USB-C 扩展坞 | 99.00 元 | completed | BILL-1001-003 | paid |
| u1002 | ORD-1002-001 | 机械键盘 | 199.00 元 | processing | BILL-1002-001 | paid |

四个订单 quantity 均为 1，数据库分别存储 12900、5900、9900、19900 分。为 paid 账单填写演示支付方式和支付时间；unpaid 账单不填写。

## 5. Python 查询接口

新增并挂载 `APIRouter(prefix="/business")`。建议使用同步 `def` 路由执行短 SQLite 查询，由 FastAPI 调度到线程池；每次操作单独打开并关闭连接，避免共享可变连接。

| 方法与路径 | 参数 | 行为 |
|---|---|---|
| GET /business/orders | user_id 必填；limit 默认 10，范围 1～20 | 当前用户最近订单 |
| GET /business/orders/{order_id} | user_id 必填 | 当前用户的指定订单 |
| GET /business/bills | user_id 必填；order_id 可选；limit 默认 10，范围 1～20 | 当前用户最近账单，或指定订单的账单 |
| GET /business/bills/{bill_id} | user_id 必填 | 当前用户的指定账单 |

约定：

- 列表按 created_at DESC、编号 DESC 稳定排序；返回 `{items, count, source: "demo_business_db"}`，其中 count 是本次返回条数，不是总条数。本期不做翻页和时间范围筛选。
- 详情返回 `{item, source: "demo_business_db"}`。订单和账单金额保留整数分字段，账单返回关联订单号。
- 无数据列表返回 HTTP 200 和空 items。
- 详情不存在或不属于该用户，统一返回 HTTP 404 与相同的 not_found 错误，不暴露其他用户的记录。
- user_id 缺失或空白、limit 超出范围，返回参数错误；连接/查询故障返回 HTTP 503 和 business_unavailable，不伪装成空列表。
- 首期客户端传入的 user_id 只是演示身份选择；SQL 过滤实现的是数据归属检查，不是登录鉴权。README 明确这个边界，暂不引入鉴权系统。
- 这些接口可通过 `/docs` 查看和手动调用。

## 6. Pi 查询工具与用户上下文

新增 `agent-service/src/tools/business-tools.ts`，实现四个工具：

| 工具 | 模型可填写的参数 | 调用接口 |
|---|---|---|
| list_orders | limit 可选 | GET /business/orders |
| get_order | order_id 必填 | GET /business/orders/{order_id} |
| list_bills | order_id、limit 可选 | GET /business/bills |
| get_bill | bill_id 必填 | GET /business/bills/{bill_id} |

接入要求：

1. 将 `userId` 加入 `AgentRunInput`，从 `runChat()` 传至 `runWithFallback()`、执行器以及所有需要该输入的调用点，包含降级路径。
2. 工具参数 schema 不包含 user_id。工具工厂在本次 run 中闭包绑定标准化后的 userId，HTTP 请求由代码附加它。模型不能选择其他用户。
3. Agent 实例池会复用执行器，不能把 userId 写入执行器、HTTP 客户端或共享工具对象的可变字段。每次 run 创建独立的业务工具数组。
4. General 和 Billing 都提供这四个只读工具，覆盖订单查询、账单查询、复合问题和 Billing 降级到 General 的场景；Technical 不增加业务查询工具。
5. 保留现有角色工具与知识工具。业务工具是否提供独立于 allowKnowledge，不能因为关闭知识检索而失去数据库查询能力。
6. 工具返回结构化 content 与 details，供模型与现有 trace 使用。HTTP 404 映射成正常的 `{found: false}` 查询结果；超时、503 等保持工具执行失败语义，使 trace.is_error 为 true。
7. 业务工具的调用记录继续进入 tools_used 和 tool_calls；knowledge_used 仍只代表 knowledge_search 成功，不因为查订单而变成 true。
8. 更新 availableToolNames() 与角色 tool_scope，使 `/health` 展示的新工具与实际能力一致；不要为健康检查伪造一个演示用户或实际执行查询。
9. 查询由固定接口和参数化 SQL 完成，不提供模型生成 SQL 或任意执行 SQL 的工具。

## 7. 路由、提示词和既有界面

- 复用现有 order_status 和 billing 等意图，不新增 Agent 类型。
- 确保“我的订单”“查订单”“订单列表”等明确查询语句能够进入 General，不被低置信度澄清拦截；只补充必要的确定性规则。
- 修改 General/Billing 提示词、角色契约、工作流和 Skills 中冲突的描述：用户询问个人订单或账单时先调用业务工具，政策问题调用知识工具。
- 没有订单号时，“查我的订单/账单”直接查询列表；用户要具体详情且缺少编号时再澄清。
- 查到记录后，只按工具返回描述状态、金额和时间；首次告知为演示数据。已查询成功时不再让用户自行去后台查看同一条信息。
- 不存在时说明“未查到该订单/账单”；服务故障时说明“查询服务暂时不可用”；不能把两者混为一谈。
- 发货只报告数据库里的 shipped 状态，本期没有物流轨迹或预计送达时间。退款、开票等操作仍说明当前不支持执行。
- 保持现有 `/chat`、`/chat/stream` 响应契约和 trace 接口兼容；前端继续使用 Python 入口和原聊天界面，用户 ID 保持 u1001 即可演示。

## 8. 建议文件划分

新增文件控制在必要范围：

```text
python-service/business/__init__.py
python-service/business/store.py       # 建表、连接和四个固定查询
python-service/business/seed.py        # 固定数据与幂等导入
python-service/business/api.py         # Router、请求校验与错误映射
python-service/tests/test_business.py  # 临时 SQLite + API 测试
agent-service/src/tools/business-tools.ts
agent-service/test/business-tools.test.ts
```

修改前述接入文件、受影响测试和 README。`docker-compose.yml` 的 python-infra environment 增加：

```yaml
BUSINESS_DB_PATH: /app/data/business/business.sqlite3
BUSINESS_SEED_DEMO: "true"
```

本地运行可使用相同环境变量；建议非 Compose 默认路径为项目根目录下的 `data/business/business.sqlite3`，默认不导入演示数据。在 `.gitignore` 忽略 `data/business/`，不要提交数据库二进制、凭证或覆盖现有 `.env`。

## 9. 实施顺序与检查

1. **建立基线**：先运行现有 TypeScript 检查与 Python 测试，记录已有失败，确认本地源码状态；不要以容器旧 dist 为修改基线。
2. **数据库与 API**：完成两张表、幂等 seed、四个查询接口；通过无模型测试及直接 HTTP 查询验证数据准确和归属过滤。
3. **Agent 工具**：完成 HTTP 客户端、请求用户上下文传递、四个工具、角色能力列表和必要规则/提示词更新。
4. **回归与 Docker**：运行相关测试和现有检查，重新构建两个应用服务，用真实模型完成聊天冒烟测试。
5. **交付文档**：记录启动命令、演示用户与编号、测试结果和实际 limitations；保留 SQLite 与现有 Redis/Chroma 数据。

必要测试：

- 重复初始化行数不变，已有行不会被 seed 覆盖；数据库重新打开后数据仍存在。
- u1001 查到三条订单和三条账单，订单/账单详情金额与状态准确，按订单过滤账单正确。
- u1002 只能查到自己的记录；u1001 查询 ORD-1002-001 或 BILL-1002-001 得到与不存在相同的 404。
- 无数据用户列表为空，缺少 user_id/非法 limit 被拒绝，查询失败与空数据区分清楚。
- 工具 HTTP 请求中的用户来自 run 输入，模型 schema 不含 user_id；交错执行两个用户的工具不会串号。
- 正常执行及降级路径都保留用户上下文、工具结果和 trace；Technical 没有这四个工具。
- Python API 测试使用临时数据库和独立测试 app 挂载 router，不启动 Redis/Chroma，不写正式业务库。
- TypeScript 工具测试 mock HTTP，不消耗模型额度。真正 `/chat` 验收另做少量冒烟测试，记录实际返回和 request_id；模型不可用时明确说明尚未完成该项，不能用 mock 充当通过。

建议命令，均从对应目录执行：

```bash
# LumenMind-pi/agent-service
npm run typecheck
npm test
npm run build

# LumenMind-pi/python-service，使用已有虚拟环境
.venv/bin/python -m pytest tests -q

# LumenMind-pi
docker compose config --quiet
docker compose up -d --build python-infra pi-agent
```

已有 Python 环境不可用时，先创建临时测试环境并按现有测试依赖安装；业务实现不新增运行依赖。不要输出解析后的完整 Compose 环境变量，不执行 `docker compose down -v`。

## 10. 最终验收示例

先检查直接查询，不依赖模型：

```bash
curl -fsS 'http://localhost:8000/business/orders?user_id=u1001'
curl -fsS 'http://localhost:8000/business/orders/ORD-1001-001?user_id=u1001'
curl -fsS 'http://localhost:8000/business/bills?user_id=u1001&order_id=ORD-1001-001'
curl -fsS 'http://localhost:8000/business/bills/BILL-1001-001?user_id=u1001'
```

再从现有兼容聊天入口查询：

```bash
curl -fsS http://localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"user_id":"u1001","message":"订单 ORD-1001-001 发货了吗？"}'
```

预期：回复明确为演示订单，商品为无线键盘，金额 129.00 元，状态已发货；tools_used 包含 get_order；通过响应 request_id 查询 trace 可看到该订单的数据库返回值。

继续验证：

| 提问 | 验收结果 |
|---|---|
| 查一下我的订单 | list_orders；u1001 的三条订单，无需先提供订单号 |
| 查看我的账单 | list_bills；三条账单，区分已支付与未支付 |
| 订单 ORD-1001-001 的账单 | list_bills 按 order_id 过滤；129.00 元，paid |
| 查询账单 BILL-1001-002 | get_bill；59.00 元，unpaid |
| 查询订单 ORD-9999-999 | 说明未查到，不编造 |
| 查询订单 ORD-1002-001（当前用户 u1001） | 说明未查到，不暴露 u1002 数据 |

还需确认 Pi 原生 `/chat` 和 `/chat/stream` 能走相同工具路径；重启 python-infra 后仍能查询到同样的订单和账单，seed 不增加重复行。

最终交付应包含代码、相关测试、README 使用说明，以及实际通过的 API/聊天验收结果。不要扩展成支付系统、管理后台或新的微服务架构。
