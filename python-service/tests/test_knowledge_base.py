"""KnowledgeBase 的写入契约测试。

测试只覆盖稳定 ID 和默认 upsert 语义，不启动 ChromaDB 或下载 embedding 模型。
"""

import sys
import types

if "chromadb" not in sys.modules:
    # CI 只运行这个契约测试时也不要求安装完整的 ChromaDB 客户端。
    sys.modules["chromadb"] = types.ModuleType("chromadb")

from mcp.knowledge_base import KnowledgeBase


def test_repeated_document_import_uses_upsert_by_default():
    class FakeCollection:
        def __init__(self):
            self.calls = []

        def upsert(self, **kwargs):
            self.calls.append(("upsert", kwargs))

        def add(self, **kwargs):
            self.calls.append(("add", kwargs))

    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb._collection = FakeCollection()
    documents = [{"title": "退款政策", "content": "7 天内可以申请退款。"}]

    assert kb.add_documents(documents) == 1
    assert kb.add_documents(documents) == 1
    assert [kind for kind, _ in kb._collection.calls] == ["upsert", "upsert"]
    assert kb._collection.calls[0][1]["ids"] == kb._collection.calls[1][1]["ids"]


def test_add_can_explicitly_keep_non_idempotent_semantics():
    class FakeCollection:
        def __init__(self):
            self.add_calls = 0
            self.upsert_calls = 0

        def add(self, **_kwargs):
            self.add_calls += 1

        def upsert(self, **_kwargs):
            self.upsert_calls += 1

    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb._collection = FakeCollection()

    kb.add_documents([{"title": "测试", "content": "内容"}], upsert=False)

    assert kb._collection.add_calls == 1
    assert kb._collection.upsert_calls == 0
