"""Retrieval behavior over arbitrary documents, without a model or Chroma server."""
import sys
import threading
import types

# The CI test environment intentionally omits the runtime-only Chroma client.
# KnowledgeBase is exercised with a fake collection below, so keep collection
# importable without downloading or installing ChromaDB.
if "chromadb" not in sys.modules:
    sys.modules["chromadb"] = types.ModuleType("chromadb")

from mcp.hybrid_retrieval import LexicalIndex, fuse, query_tokens, tokenize
from mcp.knowledge_base import KnowledgeBase


def document(key, title, content, score=0.0):
    return {"id": key, "title": title, "content": content, "chunk": 0, "score": score}


def test_chinese_and_ascii_terms_and_payment_synonyms():
    assert {"设备", "离线", "e204"} <= set(tokenize("设备离线：E204"))
    assert {"付款", "支付"} <= query_tokens("付款失败")


def test_retrieves_arbitrary_titles_and_covers_two_topics():
    docs = [document("a", "设备手册", "设备离线时先检查电源及网络连接。"),
            document("b", "服务条款", "设备保修一年，维修需要购买凭证。"),
            document("c", "活动", "夏季活动可领取积分。")]
    sparse = LexicalIndex(docs).rank("设备离线还在保修期该怎么办")
    # Deliberately wrong dense order; lexical evidence recovers both documents.
    result = fuse([docs[2], docs[0], docs[1]], sparse, 2)
    assert {row["id"] for row in result} == {"a", "b"}


def test_no_lexical_overlap_preserves_dense_fallback():
    dense = [document("b", "B", "内容乙", .8), document("a", "A", "内容甲", .7)]
    assert fuse(dense, [], 1) == dense[:1]


def test_multiple_chunks_do_not_flood_all_positions():
    chunks = [document(str(i), "同一文档", "网络连接问题") for i in range(5)]
    other = document("other", "另一文档", "网络故障排查")
    result = fuse(chunks + [other], [(row, 1.0) for row in chunks + [other]], 3)
    assert "另一文档" in [row["title"] for row in result]
    assert len({row["id"] for row in result}) == 3
    assert [row["score"] for row in result] == sorted((row["score"] for row in result), reverse=True)


def test_diversity_penalty_is_reflected_in_score_and_raw_rrf_is_preserved():
    docs = [document("a", "同一文档", "网络连接问题", .9),
            document("b", "同一文档", "网络连接问题", .8),
            document("c", "另一文档", "网络故障排查", .7)]
    result = fuse(docs, [(row, 1.0) for row in docs], 3)
    assert [row["id"] for row in result] == ["a", "c", "b"]
    assert [row["score"] for row in result] == sorted((row["score"] for row in result), reverse=True)
    assert all(row["rrf_score"] >= row["score"] for row in result)


class Collection:
    def __init__(self):
        self.rows = {"a": ("温度设置请查看设备面板。", {"title": "用户手册", "chunk_index": 0})}
        self.depths = []

    def count(self):
        return len(self.rows)

    def get(self, **_kwargs):
        return {"ids": list(self.rows), "documents": [v[0] for v in self.rows.values()],
                "metadatas": [v[1] for v in self.rows.values()]}

    def query(self, **kwargs):
        self.depths.append(kwargs["n_results"])
        page = self.get()
        return {key: [value] for key, value in {**page, "distances": [0.2] * self.count()}.items()}

    def upsert(self, ids, documents, metadatas):
        self.rows.update({key: (text, meta) for key, text, meta in zip(ids, documents, metadatas)})


def knowledge():
    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb._collection = Collection()
    kb.retrieval_mode = "hybrid"
    kb._index_lock = threading.RLock()
    kb._lexical_snapshot = None
    kb._lexical_updated = 0.0
    return kb


def test_empty_and_tiny_corpora_never_request_too_many_neighbors():
    kb = knowledge()
    assert len(kb.search("温度", 5)) == 1
    assert kb._collection.depths == [1]
    assert kb.search("  ", 5) == []
    assert kb.search("温度", 0) == []
    kb._collection.rows.clear()
    assert kb.search("温度", 5) == []


def test_upsert_immediately_invalidates_lexical_snapshot():
    kb = knowledge()
    assert kb.search("温度")[0]["title"] == "用户手册"
    old = kb._lexical_snapshot
    kb.add_documents([{"title": "新能源", "content": "电池充电时间为两小时。"}])
    assert kb._lexical_snapshot is None
    assert kb.search("电池充电")[0]["title"] == "新能源"
    assert kb._lexical_snapshot is not old


def test_hybrid_expands_dense_pool_before_truncation():
    kb = knowledge()
    kb._collection.rows.update({str(i): ("内容", {"title": str(i)}) for i in range(30)})
    assert len(kb.search("温度", 5)) == 5
    assert kb._collection.depths == [20]
