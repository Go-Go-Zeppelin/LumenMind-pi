"""基于 ChromaDB Embedding 的意图候选索引。

该模块只负责语义召回，不负责最终裁决：Pi IntentRouter 会把候选交给
LLM 做最终判断。相似度是用于排序/阈值的归一化分数，不是校准后的概率。
"""
import asyncio
import hashlib
import logging
from typing import Any, Dict, List

import chromadb

logger = logging.getLogger(__name__)


INTENT_EXAMPLES: Dict[str, List[str]] = {
    "refund": [
        "我想申请退款",
        "退款多久到账",
        "这笔钱什么时候退回来",
        "订单取消后多久退款",
        "退款显示成功但没到账",
    ],
    "invoice": [
        "帮我开发票",
        "发票抬头怎么填写",
        "如何获取电子发票",
        "发票开错了可以重开吗",
        "公司报销需要增值税专用发票",
    ],
    "payment_issue": [
        "为什么被重复扣款",
        "支付失败了",
        "我的银行卡扣了两次钱",
        "付款时一直提示支付失败",
        "订单支付成功但订单未更新",
    ],
    "billing": [
        "我想查看账单",
        "订阅费用怎么计算",
        "账单金额有问题",
        "这个月账单为什么变多了",
        "自动续费怎么关闭",
    ],
    "technical_login": [
        "登录失败怎么办",
        "登录时报 401",
        "收不到登录验证码",
        "账号密码正确但无法登录",
        "登录页面一直加载不出来",
    ],
    "technical_crash": [
        "应用一直崩溃",
        "页面打开就闪退",
        "接口返回 500 错误",
        "软件启动后就闪退",
        "操作到一半页面崩溃了",
    ],
    "technical": [
        "接口报错怎么排查",
        "SDK 调用超时了",
        "系统出现错误",
        "调用接口返回超时",
        "程序运行时报未知错误",
    ],
    "logistics": [
        "快递到哪里了",
        "什么时候可以送到",
        "物流信息没有更新",
        "包裹显示签收但我没收到",
        "物流一直停在同一个地方",
    ],
    "order_status": [
        "订单处理到哪一步了",
        "发货了吗",
        "查询订单状态",
        "为什么订单还没发货",
        "订单状态一直显示待支付",
    ],
    "account_security": [
        "我的账号可能被盗了",
        "发现异常登录",
        "如何重置密码保护账户",
        "收到不是本人操作的登录提醒",
        "账号被锁定了怎么解锁",
    ],
    "account": [
        "如何修改邮箱",
        "我想注销账户",
        "怎么修改账户资料",
        "绑定手机号怎么更换",
        "怎么修改账户昵称和头像",
    ],
    "complaint": [
        "我要投诉",
        "你们的服务太差了",
        "这个问题让我等了很久",
        "客服一直不回复我",
        "我对处理结果非常不满意",
    ],
    "feedback": [
        "你们服务很好",
        "我很满意",
        "谢谢客服的帮助",
        "这次问题解决得很快",
        "给客服点个赞",
    ],
    "request": [
        "请帮我处理一下",
        "帮我修改订单地址",
        "我想取消订单",
        "能帮我加急处理吗",
        "请帮我修改收货人电话",
    ],
    "query": [
        "我想咨询一个问题",
        "请问应该怎么做",
        "这个功能在哪里",
        "这个规则是怎么规定的",
        "能不能介绍一下具体流程",
    ],
    "escalation": [
        "我要找经理",
        "请升级给主管处理",
        "我想联系更高级别客服",
        "请把工单转给负责人",
        "这个问题你们解决不了 我要投诉到上级",
    ],
    "human_handoff": [
        "请转人工",
        "我想找人工客服",
        "这个问题需要人工处理",
        "转接人工客服",
        "不要机器人 我要人工",
    ],
    "greeting": [
        "你好",
        "您好",
        "嗨，很高兴认识你",
        "早上好",
        "在吗",
    ],
    "other": [
        "我不太清楚怎么描述",
        "这个情况有点奇怪",
        "我有一个问题",
        "你帮我看看这是什么问题",
        "说不上来 就是感觉不对劲",
    ],
}


class IntentEmbeddingIndex:
    COLLECTION_NAME = "intent_examples"
    # 示例集变更时递增此版本，已存在的 collection 会自动重新同步。
    SEED_VERSION = "v2-5-examples-per-intent"

    def __init__(self, chroma_host: str = "localhost", chroma_port: int = 8000, chroma_path: str = "./data/chroma"):
        try:
            self._client = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._client.heartbeat()
            logger.info("意图 Embedding 索引已连接: %s:%s", chroma_host, chroma_port)
        except Exception:
            logger.info("意图 Embedding 索引使用本地模式: %s", chroma_path)
            self._client = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={
                "description": "LumenMind 意图分类示例 Embedding",
                "seed_version": self.SEED_VERSION,
            },
        )
        if self._needs_seed():
            self._seed()

    @staticmethod
    def _expected_count() -> int:
        return sum(len(examples) for examples in INTENT_EXAMPLES.values())

    def _needs_seed(self) -> bool:
        metadata = self._collection.metadata or {}
        return (
            self._collection.count() != self._expected_count()
            or metadata.get("seed_version") != self.SEED_VERSION
        )

    def _seed(self) -> None:
        ids: List[str] = []
        docs: List[str] = []
        metas: List[Dict[str, Any]] = []
        for intent, examples in INTENT_EXAMPLES.items():
            for index, example in enumerate(examples):
                ids.append(hashlib.md5(f"{intent}:{index}:{example}".encode()).hexdigest())
                docs.append(example)
                metas.append({"intent": intent})

        # 旧版本可能只有部分示例：先删除不在新全集里的 ID，再 upsert 完整示例集。
        desired_ids = set(ids)
        try:
            existing_ids = set(self._collection.get()["ids"])
        except Exception:
            existing_ids = set()
        stale_ids = existing_ids - desired_ids
        if stale_ids:
            self._collection.delete(ids=list(stale_ids))

        self._collection.upsert(ids=ids, documents=docs, metadatas=metas)
        self._collection.modify(metadata={
            "description": "LumenMind 意图分类示例 Embedding",
            "seed_version": self.SEED_VERSION,
        })
        logger.info("意图 Embedding 索引已同步 %s 条示例，版本 %s", len(ids), self.SEED_VERSION)

    def search(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        # 每个意图 5 条示例，检索深度同步扩大，避免单一意图占满近邻候选。
        n_results = max(1, min(self._collection.count(), max(top_k * 5, top_k)))
        result = self._collection.query(query_texts=[query], n_results=n_results)
        grouped: Dict[str, Dict[str, Any]] = {}
        documents = result.get("documents", [[]])[0] or []
        metadatas = result.get("metadatas", [[]])[0] or []
        distances = result.get("distances", [[]])[0] or []
        for example, metadata, distance in zip(documents, metadatas, distances):
            intent = str((metadata or {}).get("intent", "other"))
            # Chroma 返回距离；转成便于阈值判断的 0~1 相似度参考分数。
            score = max(0.0, min(1.0, 1.0 - float(distance)))
            if intent not in grouped or score > grouped[intent]["score"]:
                grouped[intent] = {"intent": intent, "score": round(score, 4), "example": str(example)}
        return sorted(grouped.values(), key=lambda item: item["score"], reverse=True)[:top_k]

    async def search_async(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.search, query, top_k)

    def count(self) -> int:
        return self._collection.count()
