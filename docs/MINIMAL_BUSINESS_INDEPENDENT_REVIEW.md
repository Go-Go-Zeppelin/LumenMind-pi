# LumenMind-pi 最小订单与账单查询独立验收报告

验收日期：2026-10-08（Asia/Shanghai）。本报告按原计划及本轮独立运行结果编写，实施方 ACCEPTANCE 文件仅作为待验证材料；下列 request_id 均由本轮新请求取得。

## 总体结论

**完成。按原计划的最小功能范围，可以交付。没有发现阻止交付的 P0/P1/P2 问题。**

独立执行 TypeScript 类型检查、56 项测试、构建、51 项 Python 测试和 Compose 配置检查；47 项 Docker HTTP 检查、13 次真实模型聊天及其 trace 核对通过。另有 19 项独立补充检查，使用临时 SQLite、真实 Express/FastAPI HTTP，只有 Pi 模型/事件及记忆返回为模拟，用于降级、身份传递和故障路径。临时库另验证了10项数据库性质。

Python 应用重启前后，正式业务库的4条订单、4条账单、全部字段及文件 SHA256 保持一致。源码及 `.env` 的验收前后哈希未变化；本轮只新增验收报告和证据文件。没有改实现代码、配置、清库或重建镜像。独立重新编译后容器文件已经一致，因此只按许可重启了 python-infra。真实聊天会按原应用逻辑记录新验收会话记忆，业务表没有改变。

存在一项 P3 非阻断问题：部分成功账单查询附带人工建议，触发现有 escalated 关键字判定。历史跨目录改动无法追溯，真实模型下强制故障未执行；均在下文明确标记，不影响已允许离线验证的本期功能验收。

完整独立响应、trace、HTTP检查与数据库对比：[机器可读证据](MINIMAL_BUSINESS_INDEPENDENT_REVIEW.json)。脚本、原始日志和快照：[可复核证据包](MINIMAL_BUSINESS_REVIEW_EVIDENCE.zip)。

## 按原计划的验收表

| 要求 | 状态 | 独立证据 |
|---|---|---|
| 当前实现与范围 | 通过 | 业务模块、四个 GET 路由、工具和部署位于 LumenMind-pi；没有支付、退款、开票、录入后台或登录操作接口。 定位：`python-service/business/api.py:44；README.md:108`。 |
| 历史上是否仅改动 LumenMind-pi | 未验证 | 无 Git 仓库或实施前跨目录快照，不能证明历史上完全没有修改其他目录。当前运行功能范围已检查。 定位：`git status --short exit 128`。 |
| 两张业务表、整数分、字段与状态 | 通过 | 正式库仅 orders/bills 两张业务表；四组演示数据全部符合计划；金额 typeof 为 integer。 定位：`business/store.py:32；offline_database_checks；persistence.before`。 |
| 外键及索引 | 通过 | 每次 connect 开启 foreign_keys；临时库实际插入不存在订单的账单被 IntegrityError 拒绝；两项索引存在；正式库 foreign_key_check 为空。 定位：`business/store.py:21,45,53；offline_database_checks`。 |
| 固定参数化 SQL | 通过 | 四个查询均使用 ? 参数；账单 JOIN 订单并过滤用户。Docker HTTP 注入形态 user_id 返回空列表。 定位：`business/store.py:64,71,78,88；live.api`。 |
| 演示数据与 seed 开关 | 通过 | u1001 三组、u1002 一组，12900/5900/9900/19900 分；paid/unpaid 信息正确；未开启 seed 时为空由既有测试独立重跑验证。 定位：`business/seed.py:4；api/main.py:79；test_business.py:22`。 |
| 初始化幂等、不覆盖、重开持久化 | 通过 | 临时库修改订单和账单后连续初始化三次保留修改；正式库重启 Python 前后 4+4 行及 SHA256 完全相同。 定位：`offline_database_checks；persistence.checks`。 |
| 测试不污染正式业务数据 | 通过 | 写入测试使用 tmp_path/独立 app 或 /tmp 临时库；真实聊天只读正式业务库。验收前后正式业务文件 SHA256 一致。 定位：`test_business.py:11,22；persistence.checks.same_sha256`。 |
| 四个 HTTP 接口及接口文档 | 通过 | 列表与详情、必填身份、source/count/整数分返回符合约定；OpenAPI 具备四个路径。 定位：`live.api；live.openapi_business_routes`。 |
| 稳定排序与 limit | 通过 | Docker 默认/1/2/20及0/21/bad/1.5参数验证；临时库相同 created_at 时编号降序验证通过。 定位：`live.api；offline_database_checks.order_tie_sort/bill_tie_sort`。 |
| 按订单过滤账单 | 通过 | ORD-1001-001 仅得到 BILL-1001-001、12900分、paid；u1001 过滤其他用户订单为空。 定位：`live.api；bills-filter trace`。 |
| 身份隔离、统一 404 | 通过 | u1001/u1002 各自记录准确；订单与账单越权和不存在均返回相同 404 not_found；真实模型越权订单得到 found=false。 定位：`live.api；order-foreign trace`。 |
| anonymous/demo/未知用户不映射 | 通过 | 两类列表对 anonymous/demo/unknown 均返回空列表；真实模型 unknown 查询为空。 定位：`live.api；native-unknown trace`。 |
| 数据库故障是 503，不是空列表 | 通过（离线真实 SQL） | 独立临时服务把路径切到不存在的父目录，四个接口实际连接失败均为 503 business_unavailable；未破坏正式库。 定位：`offline-http.jsonl mode=missing；offline.cases`。 |
| userId 正常链路 | 通过 | 实际 Express /chat → 编排器 → 执行器 → 工具闭包 → FastAPI HTTP → SQLite，带空格用户标准化后结果正确。真实模型正常路径也验证。 定位：`offline normal-u1001/normal-u1002；live.chats`。 |
| Billing 异常回退 General | 通过（模拟模型，真实 HTTP/SQL） | 注入 Billing 模型中断，原查询和 General 再查询的 HTTP 均 user_id=u1002，返回其一条账单，两条 trace 保留 fallback_used。 定位：`offline billing-exception-fallback；offline-http.jsonl`。 |
| 监控降级到 General | 通过（模拟模型，真实 HTTP/SQL） | 设置 Billing 路由惩罚后 primary_agent=general；实际 HTTP 仍用 u1002，并只取该用户账单。 定位：`offline monitor-fallback`。 |
| 并发与复用不串用户 | 通过 | 真实模型两用户同 conv_id 同时请求，分别返回3+3与1+1；离线8次交错 /chat 和同一 executor 并发 run 均正确。 定位：`live concurrent-u1001/concurrent-u1002；offline.cases`。 |
| 模型 schema 不包含 user_id | 通过 | 四个 schema 无 user_id 且 additionalProperties=false；直接给 handler 附加 user_id=u1002 仍使用闭包 u1001 发 HTTP 查询。 定位：`business-tools.ts:24,42；offline schema-and-model-user-override-ignored`。 |
| 角色工具白名单与 allowKnowledge=false | 通过 | General/Billing 四个业务工具，Technical 无；关闭知识检索时同一个执行器仍分别查询两用户并保留 trace。 定位：`executor.ts:159；offline shared-executor-no-knowledge-* / technical-no-business-tools`。 |
| 404 工具正常 found=false | 通过 | 真实模型不存在/越权订单均 success=true、is_error=false、found=false；账单404的映射由重跑的独立工具测试及接口404验证。 定位：`live order-missing/order-foreign；business-tools.test.ts`。 |
| 503/超时保持工具失败与错误 trace | 通过（模拟模型，真实 HTTP 故障） | 临时库真实连接故障和30ms客户端对300ms延迟的真实HTTP超时，均 trace.is_error=true、success=false，未伪装空结果。 定位：`offline actual-db-connection-error-503-trace / actual-http-timeout-trace`。 |
| tools_used/tool_calls/实际返回/knowledge_used | 通过 | 13 次真实模型 trace 都含正确用户及本次调用；业务工具输出逐字段与独立即时 GET 比较一致；全部 knowledge_used=false。 定位：`live.chats[*].checks；live.chats[*].trace`。 |
| /health 能力列表一致 | 通过 | 在线 General/Billing 各有四个业务工具，Technical 无；available_tools 与 tool_scope 一致，离线构造实际 Agent tools 再验证。 定位：`live.health.agents；offline technical-no-business-tools`。 |
| 提示词、工作流、Skills 不阻挡查询 | 通过 | 13 次真实聊天都先取得工具结果，列表无需提供编号；回复均说明演示数据、准确换算金额，没有索取字段阻挡查询。存在非阻断多余人工建议，见P3。 定位：`prompts.ts:29；workflow.ts:67；live.chats`。 |
| TypeScript 类型检查、测试、构建 | 通过 | typecheck、56 tests、build 均 exit 0。 定位：`commands；typescript-*.log`。 |
| Python 全部测试及 Compose | 通过 | 51 tests；compose config --quiet exit 0；没有失败，只有1项既有弃用警告。 定位：`commands；python-tests.log`。 |
| Docker 为当前源码构建产物 | 通过 | 先独立 npm run build，再逐字节比对容器和本地 41 个 Python/JavaScript 文件，全部一致；Skills 由本目录只读挂载。 定位：`container_comparison；docker inspect Mounts`。 |
| Python代理/Pi原生/SSE/计划例句 | 通过 | 7个计划例句、原生用户2/未知用户/复合问题、SSE、并发均通过；SSE有 agent_start/tool_start/tool_end/done，done含可查询request_id。 定位：`live.chats 共13次`。 |
| Python重启后数据与seed | 通过 | docker compose restart python-infra成功；健康恢复，4+4行全部字段及SHA256一致；HTTP仍各3条。 定位：`persistence.checks`。 |
| 真实模型下强制故障/强制降级 | 未验证（按许可用离线验证） | 未对 Docker 正式数据库或真实模型注入故障；降级与错误语义已用模拟Pi事件+真实业务HTTP/SQL验证，不冒称真实模型故障验证。 定位：`offline.mode；本报告限制`。 |

## userId 逐段审查

下表静态定位与独立实际 HTTP/SQL 验证结合，不以类型定义代替实际查询。

| 环节 | 实现与验证 |
|---|---|
| chat入口 | [agent-service/src/app.ts:83](../agent-service/src/app.ts:83)：从请求取 user_id；缺省 anonymous。 |
| chat → runChat | [agent-service/src/agents/orchestrator.ts:194](../agent-service/src/agents/orchestrator.ts:194)：trim 后局部 userId，传入 runChat，未写共享字段。 |
| runChat → runWithFallback | [agent-service/src/agents/orchestrator.ts:353](../agent-service/src/agents/orchestrator.ts:353)：userId 作为参数传给每个目标 Agent。 |
| 执行输入 | [agent-service/src/agents/orchestrator.ts:499](../agent-service/src/agents/orchestrator.ts:499)：input.userId 保持请求身份。 |
| executor.run → 本次闭包 | [agent-service/src/agents/executor.ts:162](../agent-service/src/agents/executor.ts:162)：每次 run 调用 createBusinessTools(infrastructure,input.userId)，不把身份存在池化执行器。 |
| 闭包 → HTTP客户端 | [agent-service/src/tools/business-tools.ts:42](../agent-service/src/tools/business-tools.ts:42)：四个 query 闭包引用 userId；schema 没有用户字段。 |
| HTTP 参数 | [agent-service/src/clients/infrastructure.ts:118](../agent-service/src/clients/infrastructure.ts:118)：URLSearchParams 附加 user_id，路径编号编码；客户端无当前用户状态。 |
| HTTP → SQL | [python-service/business/api.py:15](../python-service/business/api.py:15)：去空白后传固定查询；store使用 WHERE user_id=? 或 JOIN orders 后 WHERE o.user_id=?。 |
| SQL归属 | [python-service/business/store.py:64](../python-service/business/store.py:64)：订单查询与详情过滤user；账单查询第78行、详情第88行JOIN订单过滤user。 |
| 异常回退 | [agent-service/src/agents/orchestrator.ts:526](../agent-service/src/agents/orchestrator.ts:526)：General.run({...input}) 保留userId；原与回退调用轨迹合并。 |
| 监控降级 | [agent-service/src/agents/orchestrator.ts:292](../agent-service/src/agents/orchestrator.ts:292)：改变路由角色，不改变runChat局部userId。 |

补充运行结果：带空格的 u1001/u1002 经真实 `/chat` HTTP 标准化后分别取3/1条账单；Billing中断和监控降级后HTTP仍为u1002。8次交错请求无串号；共享同一个executor并发run、allowKnowledge=false时，仍分别取3/1条订单。收到的业务HTTP参数和响应状态保存在证据包 `offline-http.jsonl` 及JSON的 `offline_http` 中。

在线真实模型并发两请求使用完全相同的 conv_id，但不同 user_id，分别得到 u1001 的3条订单+3条账单和u1002的1条订单+1条账单；请求执行时间区间重叠。这里只验证演示身份归属过滤，不把客户端可选择的user_id描述成登录鉴权。

## 实际运行的命令及结果

完整输出在JSON的logs与证据包同名文件中；以下为实际执行结果，不引用实施方旧日志。

| 工作目录 | 命令 | 结果 |
|---|---|---|
| `../agent-service` | `npm run typecheck` | 通过 |
| `../agent-service` | `npm test` | 10 个文件，56 项通过 |
| `../agent-service` | `npm run build` | 通过 |
| `../python-service` | `.venv/bin/python -m pytest tests -q` | 51 项通过，1 项 Starlette/AnyIO 弃用警告 |
| `..` | `docker compose config --quiet` | 通过 |
| `..` | `python3 /tmp/lumenmind-pi-independent-review.6LsoPy/live_review.py` | 47 项 HTTP 检查、13 次真实模型聊天通过 |
| `../python-service` | `.venv/bin/python /tmp/lumenmind-pi-independent-review.6LsoPy/offline_server.py` | 临时独立 FastAPI 服务；检查完毕 SIGTERM 正常停止，shell exit 143 |
| `../agent-service` | `node --loader /tmp/lumenmind-pi-independent-review.6LsoPy/pi-loader.mjs /tmp/lumenmind-pi-independent-review.6LsoPy/offline_review.mjs` | 19 项检查通过：Pi 模型/事件模拟，业务 HTTP 与 SQL 真实 |
| `..` | `docker cp <container>:/app/<module> -（Python）及 docker cp <container>:/app/dist/src -（Pi），与本地文件逐字节比较` | 独立重新编译后 41 个 .py/.js 文件一致；无需重新构建镜像 |
| `..` | `docker compose restart python-infra` | 成功；重启后健康，所有业务字段、行数、SHA256 一致 |
| `..` | `git status --short` | 目录没有 Git 仓库，无法追溯实施期间跨目录历史改动 |

独立脚本最初运行于 `/tmp/lumenmind-pi-independent-review.6LsoPy/`，已收入证据包。`live_review.py`直接使用urllib请求Docker服务、读取每次trace并把业务工具输出与独立即时GET逐字段比较；`offline_server.py`加载未修改的business路由，使用独立临时库；`offline_review.mjs`加载本轮编译的实际客户端、编排器、执行器和Express入口，仅通过临时loader替换Pi的模型事件。补充检查未修改应用实现文件，也没有把模拟事件当作真实模型通过。

Python测试有1项现有Starlette/AnyIO弃用警告；补充Node脚本有experimental loader警告。两者没有测试失败。临时服务器在检查后已停止，Docker四服务保持健康。

## 本轮真实模型聊天与trace

模型由在线 `/health` 确认是 `deepseek-v4-flash`，Pi SDK 0.82.1。各次工具output均与本轮独立GET相等，trace.user_id及conv_id匹配，output_valid=true，knowledge_used=false。人工核对最终回复的商品、金额、状态和演示标记；不存在/越权请求不泄露其他用户商品与金额。

| 场景/入口 | 用户 | 本轮 request_id | trace关键返回 |
|---|---|---|---|
| 订单列表 / Python | u1001 | `0f875a3b-3039-49f4-a9e5-4c0ca8d4b54c` | list_orders → count=3；ORD-1001-003,ORD-1001-002,ORD-1001-001 |
| 是否发货 / Python | u1001 | `c25d2b42-f756-4fea-b6bd-65040655c72e` | get_order → ORD-1001-001；；12900分；shipped |
| 账单列表 / Python | u1001 | `e18d00cf-6053-4bd6-969e-3465561c8a41` | list_bills → count=3；BILL-1001-003,BILL-1001-002,BILL-1001-001 |
| 订单关联账单 / Python | u1001 | `3a76d1ac-4c44-4693-b5d3-fe99fcd224f0` | list_bills → count=1；BILL-1001-001 |
| 未支付账单详情 / Python | u1001 | `d5d09904-0343-4b02-9dd2-139efe465f5a` | get_bill → ORD-1001-002；BILL-1001-002；5900分；unpaid |
| 不存在订单 / Python | u1001 | `00d11e19-2397-4a5a-beff-f035a1c86563` | get_order → found=false；success=true；is_error=false |
| 越权订单 / Python | u1001 | `36c7d89d-3a72-435a-930f-52096d14efe8` | get_order → found=false；success=true；is_error=false |
| 订单列表 / Pi原生 | u1002 | `5d8bec15-7748-4e5e-8f09-929c49bdd9fd` | list_orders → count=1；ORD-1002-001 |
| 未知用户账单 / Pi原生 | unknown | `318d76c4-fc9b-4e97-9651-92207c8e784f` | list_bills → count=0； |
| 订单+账单 / Pi原生 | u1001 | `04a8b4c7-bb1d-4e4e-9034-516aa3591f40` | list_orders → count=3；ORD-1001-003,ORD-1001-002,ORD-1001-001<br>list_bills → count=3；BILL-1001-003,BILL-1001-002,BILL-1001-001 |
| 发货查询 / Pi SSE | u1001 | `34f78eea-7946-49a1-85c5-5ecf81d6123a` | get_order → ORD-1001-001；；12900分；shipped |
| 并发订单+账单 / Pi原生 | u1001 | `6f88c74d-4777-4a83-8d81-d6cd06303215` | list_orders → count=3；ORD-1001-003,ORD-1001-002,ORD-1001-001<br>list_bills → count=3；BILL-1001-003,BILL-1001-002,BILL-1001-001 |
| 并发订单+账单 / Pi原生 | u1002 | `1925ee07-64b6-400e-af19-b799856c7553` | list_orders → count=1；ORD-1002-001<br>list_bills → count=1；BILL-1002-001 |

发货查询的最终回复确认：无线键盘×1、129.00元、已发货，并说明演示数据不含物流轨迹或预计送达时间。BILL-1001-002回复为59.00元、未支付、无支付方式和支付时间。不存在与越权查询都只说明未查到，没有冒充后台查到其他人的记录。

SSE独立解析到agent_start、tool_start、tool_end、done；done的request_id再请求 `/trace/tool/{request_id}`，得到get_order实际返回。并发两用户的同一conv_id为 `independent-concurrent-e73181e06c2b4889bdbec10588f00741`，不混淆用户和输出。

可复核例：

```bash
curl -fsS http://localhost:3000/trace/tool/c25d2b42-f756-4fea-b6bd-65040655c72e
curl -fsS http://localhost:3000/trace/tool/d5d09904-0343-4b02-9dd2-139efe465f5a
curl -fsS http://localhost:3000/trace/tool/34f78eea-7946-49a1-85c5-5ecf81d6123a
```

Pi的trace保存在内存，后续重启Pi或超过保留上限可能失去在线记录；本报告JSON已独立保存完整响应和trace。此次仅重启Python，不清除Pi trace。

## Docker构建与持久化证据

独立构建本地TypeScript后，比较容器中的41个.py/.js文件与当前Python源码或本地编译产物，全部逐字节一致。没有依赖镜像创建时间或实施方声称的“已重建”。Python的数据挂载指向本项目data；Pi Skills只读挂载本项目skills。无需额外重建服务。

正式业务文件SHA256重启前后均为：

```
e1a34878503e7d044178d2be0d00048bd6c16fafd37e63c00417732bd005653f
```

重启前后orders=4、bills=4，全部8条记录的每个字段相同；独立重新GET u1001仍各3条。源码及.env的46个已记录文件哈希未变化。幂等“不覆盖”另在临时库先修改订单商品和账单支付方式，再连续seed三次验证保留修改，没有拿正式库做写入故障实验。

## 问题，按严重程度排序

### P3，非阻断：成功只读账单查询会被标记为人工升级

定位：[agent-service/src/agents/orchestrator.ts:149](../agent-service/src/agents/orchestrator.ts:149) 与 [agent-service/src/agents/orchestrator.ts:383](../agent-service/src/agents/orchestrator.ts:383)；账单Skill的后续处理倾向见 [skills/billing_support/SKILL.md:20](../skills/billing_support/SKILL.md:20)。

触发：u1001发送“订单 ORD-1001-001 的账单”或“查询账单 BILL-1001-002”。本轮request_id分别为 `3a76d1ac-4c44-4693-b5d3-fe99fcd224f0` 和 `d5d09904-0343-4b02-9dd2-139efe465f5a`。工具均成功，回复金额/状态准确，但回复额外出现“人工审核”等建议，最终escalated=true。

影响：调用方可能把已完成的只读查询解释成需要人工升级。系统没有创建人工工单。本行为来自既有回复关键字判定，不是数据库或用户隔离故障，不阻止最小功能交付。

可选改进：成功只读查询减少无关资金操作说明；后续若要改善升级标记，再单独定义其结构化语义。无需为了本期验收引入工单或改造整套路由。

没有发现其他需要修复才能交付的问题。未将缺少登录、支付、退款、分页、录入后台、真实物流等原计划明确排除的能力列为缺陷。

## 未验证项与边界

1. **历史跨目录修改范围**：当前目录没有Git仓库，且没有实施前其他目录快照；只能确认当前实现位置、部署边界及本轮未修改实现，不能证明实施期间从未动过其他目录。
2. **真实模型下强制故障与降级**：未对正式数据库做破坏、锁库或模型故障注入；已按用户许可通过模拟Pi模型事件、真实HTTP/临时SQLite验证异常和监控回退、503与超时trace。此项没有冒充真实模型故障验证。
3. 本轮真实模型验收是列出的13次具体运行；报告只确认这些例句和路径的结果，未声称完成超出原计划的长期稳定性/压力验收。
