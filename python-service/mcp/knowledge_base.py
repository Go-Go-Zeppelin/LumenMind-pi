"""
RAG 知识库 —— 基于 ChromaDB 的真实检索实现。

功能：
  1. 文档导入：将文本切片后存入 ChromaDB（自动生成 Embedding）
  2. 语义检索：根据 query 从知识库中检索最相关的文档片段
  3. 与 MCP 工具框架集成：作为 knowledge_search 工具的真实 handler

ChromaDB 在这里的角色：
  - memory/ 中用于存储对话记忆（情景记忆 + 用户画像）
  - 这里用于存储知识库文档（RAG 检索）
  两者是不同的 collection，互不干扰。
"""
import asyncio
import hashlib
import logging
from typing import Any, Dict, List, Optional

import chromadb

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """
    基于 ChromaDB 的 RAG 知识库。

    ChromaDB 内置了 Embedding 模型（all-MiniLM-L6-v2），
    调用 upsert() 时自动生成或更新向量，query() 时自动做语义匹配。
    不需要额外调用 Anthropic Embeddings API。
    """

    COLLECTION_NAME = "knowledge_base"

    def __init__(
        self,
        chroma_host: str = "localhost",
        chroma_port: int = 8000,
        chroma_path: str = "./data/chroma",
    ):
        # 优先连接独立 ChromaDB 服务（服务端内置 embedding 模型，客户端无需下载）
        self._use_server = False
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            self._client = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._client.heartbeat()
            self._use_server = True
            logger.info(f"知识库 ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception:
            logger.info(f"知识库 ChromaDB 服务不可用，使用本地模式: {chroma_path}")
            self._client = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # 使用服务端时不传 embedding_function，让服务端处理
        # 本地模式时也不传，使用 ChromaDB 默认的（会触发模型下载）
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={"description": "LumenMind RAG 知识库"},
        )

        # 启动时幂等同步默认文档。默认文档 ID 固定，upsert 不会重复积累，
        # 同时不会删除用户通过接口导入的其他文档。
        self._load_default_docs()

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    def add_documents(self, documents: List[Dict[str, str]], *, upsert: bool = True) -> int:
        """
        批量导入文档到知识库。

        documents 格式: [{"title": "...", "content": "..."}, ...]
        长文档会自动切片（每片 500 字）。

        默认使用 upsert，保证重复导入幂等，并在同一稳定 ID 的内容变化时
        同步更新文档和 Embedding。需要显式保留旧的 add 行为时传入 upsert=False。
        """
        ids, docs, metas = [], [], []

        for doc in documents:
            title   = doc.get("title", "")
            content = doc.get("content", "")
            chunks  = self._chunk_text(content, chunk_size=500)

            for i, chunk in enumerate(chunks):
                doc_id = hashlib.md5(f"{title}_{i}_{chunk[:50]}".encode()).hexdigest()
                ids.append(doc_id)
                docs.append(chunk)
                metas.append({"title": title, "chunk_index": i, "total_chunks": len(chunks)})

        if ids:
            # 稳定 ID + upsert 让重复导入幂等，同时让同 ID 内容更新时 Embedding 同步更新。
            operation = self._collection.upsert if upsert else self._collection.add
            operation(ids=ids, documents=docs, metadatas=metas)
            logger.info(f"知识库{'同步' if upsert else '导入'} {len(ids)} 个文档片段")

        return len(ids)

    async def add_documents_async(self, documents: List[Dict[str, str]]) -> int:
        """异步导入文档；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.add_documents, documents)

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        语义检索：根据 query 返回最相关的文档片段。

        ChromaDB 内部自动将 query 转为向量，与存储的文档向量做余弦相似度匹配。
        """
        results = self._collection.query(
            query_texts=[query],
            n_results=top_k,
        )

        items = []
        if results["documents"] and results["documents"][0]:
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                items.append({
                    "title":    meta.get("title", ""),
                    "content":  doc,
                    "score":    round(1.0 - dist, 4),  # ChromaDB 返回距离，转为相似度
                    "chunk":    meta.get("chunk_index", 0),
                })

        return items

    async def search_async(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """异步检索；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.search, query, top_k)

    @property
    def doc_count(self) -> int:
        return self._collection.count()

    async def doc_count_async(self) -> int:
        """异步获取文档片段数量。"""
        return await asyncio.to_thread(self._collection.count)

    # ── MCP 工具 handler ─────────────────────────────────────────────────────

    async def search_handler(self, params: Dict[str, Any], context: Any) -> List[Dict]:
        """
        作为 MCP 工具的 handler 注册。

        MCPToolManager.register(Tool(
            name="knowledge_search",
            handler=kb.search_handler,
            ...
        ))
        """
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        return await self.search_async(query, top_k=top_k)

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _chunk_text(self, text: str, chunk_size: int = 500) -> List[str]:
        """将长文本按 chunk_size 切片，保留语义完整性（按句号/换行切分）。"""
        if len(text) <= chunk_size:
            return [text] if text.strip() else []

        chunks = []
        current = ""
        # 按句子切分
        sentences = text.replace("\n", "。").split("。")
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if len(current) + len(sent) + 1 > chunk_size:
                if current:
                    chunks.append(current)
                current = sent
            else:
                current = f"{current}。{sent}" if current else sent

        if current:
            chunks.append(current)

        return chunks

    def _load_default_docs(self) -> None:
        """导入默认知识库文档（客服场景常见问题）。"""
        default_docs = [
            {
                "title": "退款政策",
                "content": (
                    "退款政策说明。"
                    "用户在购买后 7 天内可以申请无理由退款。"
                    "退款申请提交后，系统会在 1-3 个工作日内审核。"
                    "审核通过后，款项将在 5-7 个工作日内退回原支付账户。"
                    "如果商品已发货，需要先完成退货流程才能退款。"
                    "退货运费由用户承担，除非是商品质量问题。"
                    "超过 7 天但未超过 30 天的订单，需要提供商品质量问题的证据才能退款。"
                ),
            },
            {
                "title": "订单查询",
                "content": (
                    "订单查询指南。"
                    "用户可以通过订单号查询订单状态。"
                    "订单状态包括：待支付、已支付、已发货、运输中、已签收、已完成。"
                    "如果订单显示已发货但超过 7 天未收到，可以联系客服申请查件。"
                    "物流信息通常在发货后 24 小时内更新。"
                    "如果订单显示异常，请提供订单号联系客服处理。"
                ),
            },
            {
                "title": "账户安全",
                "content": (
                    "账户安全说明。"
                    "建议用户定期修改密码，密码长度至少 8 位，包含字母和数字。"
                    "如果忘记密码，可以通过绑定的手机号或邮箱重置。"
                    "发现账户异常登录时，系统会自动锁定账户并发送通知。"
                    "用户可以在安全设置中开启两步验证，提高账户安全性。"
                    "不要将密码分享给他人，客服人员不会索要用户密码。"
                ),
            },
            {
                "title": "技术故障排查",
                "content": (
                    "常见技术问题排查。"
                    "应用崩溃：请尝试清除缓存后重启应用，如果问题持续请更新到最新版本。"
                    "登录失败 401 错误：表示认证失败，请检查用户名密码是否正确，或尝试重置密码。"
                    "页面加载慢：检查网络连接，尝试切换 WiFi 或移动数据。"
                    "支付失败：确认银行卡余额充足，检查是否开启了网上支付功能。"
                    "500 服务器错误：这是服务端问题，请稍后重试，如果持续出现请联系技术支持。"
                ),
            },
            {
                "title": "会员与积分",
                "content": (
                    "会员积分规则。"
                    "每消费 1 元累积 1 积分。"
                    "积分可以在下次购物时抵扣，100 积分 = 1 元。"
                    "会员等级分为：普通会员、银卡会员（累计消费 1000 元）、金卡会员（累计消费 5000 元）。"
                    "银卡会员享受 95 折优惠，金卡会员享受 9 折优惠。"
                    "积分有效期为 1 年，过期自动清零。"
                    "生日当月消费可获得双倍积分。"
                ),
            },
            {
                "title": "配送说明",
                "content": (
                    "配送服务说明。"
                    "标准配送：3-5 个工作日送达，免运费（订单满 99 元）。"
                    "加急配送：1-2 个工作日送达，运费 15 元。"
                    "同城配送：当日达或次日达，运费 10 元。"
                    "偏远地区可能需要额外 2-3 天。"
                    "配送时间为每天 9:00-18:00，节假日可能延迟。"
                    "如果需要修改收货地址，请在发货前联系客服。"
                ),
            },
            {
                "title": "发票与开票说明",
                "content": (
                    "发票与开票说明。"
                    "申请发票时通常需要提供订单号、发票类型、发票抬头和税号等开票信息。"
                    "个人发票通常填写个人姓名，企业发票应核对企业名称和纳税人识别号。"
                    "电子发票开具后可在订单详情或发票记录中查看和下载。"
                    "如果发票抬头或税号填写错误，应尽快提供订单号和正确的开票信息，申请作废或重开。"
                    "是否支持补开发票以及可补开的时间范围，应以订单状态和当前开票规则为准。"
                    "客服不能在未核验订单的情况下承诺发票已经开具或修改完成。"
                ),
            },
            {
                "title": "订阅与账单说明",
                "content": (
                    "订阅与账单说明。"
                    "用户可以在账户的订阅或账单页面查看当前套餐、计费周期、续费日期和历史账单。"
                    "账单金额通常由套餐费用、增值服务、折扣和税费等项目组成，具体以账单明细为准。"
                    "取消自动续费通常只影响下一个计费周期，不代表当前周期的服务会立即失效。"
                    "如果账单金额与预期不一致，应记录账单周期、订单号、金额和支付时间后再核验。"
                    "不要向客服提供完整银行卡号、支付密码或短信验证码。"
                    "涉及真实扣款、套餐变更和退款状态时，需要由账单后台或人工客服完成核验。"
                ),
            },
            {
                "title": "支付异常处理",
                "content": (
                    "支付异常处理指南。"
                    "支付失败时可以先确认网络连接、账户余额、银行卡状态、支付限额和网上支付功能是否正常。"
                    "如果页面显示支付失败但账户已经扣款，不要连续重复支付，应先保存支付时间、金额、订单号和支付渠道。"
                    "同一订单出现两笔相同或相近扣款时，应分别记录两笔交易的时间和金额，交由账单后台核验。"
                    "扣款成功但订单仍显示待支付时，可以稍后刷新订单状态；问题持续时应联系人工客服核对支付流水。"
                    "客服不能仅凭用户描述认定重复扣款，也不能在未核验前承诺退款已经发起。"
                    "任何情况下都不应索取支付密码、短信验证码或完整银行卡号。"
                ),
            },
            {
                "title": "登录与验证码排查",
                "content": (
                    "登录与验证码问题排查。"
                    "无法登录时先确认账号、登录方式和网络环境是否正确，并检查密码输入是否包含多余空格。"
                    "401 通常表示身份验证失败或凭证已经失效，可以尝试重新登录或重置密码。"
                    "收不到验证码时，应检查手机号或邮箱是否填写正确、垃圾邮件或拦截记录，以及验证码发送是否过于频繁。"
                    "验证码具有时效性，多次请求后应使用最新收到的一条，避免继续频繁点击发送。"
                    "如果更换设备或网络后仍持续失败，应记录发生时间、客户端版本和错误提示。"
                    "客服不会索取密码或验证码；涉及账号锁定、异常登录或身份核验时应升级人工处理。"
                ),
            },
            {
                "title": "订单修改与取消",
                "content": (
                    "订单修改与取消说明。"
                    "订单能否取消或修改，取决于订单当前状态。"
                    "待支付订单通常可以直接取消；已经进入备货或发货流程的订单可能无法直接修改。"
                    "修改收货地址时应尽早处理，并提供订单号和新的收货地区，但不要在公共渠道发送不必要的隐私信息。"
                    "订单已经发货后，如需变更地址，应联系承运方或人工客服确认是否支持改址。"
                    "取消订单不等于退款已经到账；已经支付的订单仍需按照退款流程处理。"
                    "客服在没有连接订单后台时只能说明通用流程，不能声称订单已取消、地址已修改或退款已完成。"
                ),
            },
            {
                "title": "人工客服与升级处理",
                "content": (
                    "人工客服与升级处理说明。"
                    "用户明确要求人工客服、主管或升级处理时，应记录升级原因和已知问题，不再反复要求用户描述同样的信息。"
                    "涉及真实资金操作、账户身份核验、后台日志、管理员权限或多次排查仍无法解决的问题，应建议人工处理。"
                    "升级信息可以包括问题发生时间、订单号、错误码、客户端版本和已尝试步骤。"
                    "升级过程中不要收集密码、短信验证码、支付密码、完整银行卡号、私钥或完整 API Key。"
                    "智能客服只能生成交接摘要，不能假装已经创建工单、连接人工或完成后台操作。"
                    "紧急且影响范围较大的故障应在交接摘要中标记优先级，并保留 request_id 和日志时间点。"
                ),
            },
        ]
        self.add_documents(default_docs, upsert=True)
        logger.info(f"已同步默认知识库: {len(default_docs)} 篇文档")
