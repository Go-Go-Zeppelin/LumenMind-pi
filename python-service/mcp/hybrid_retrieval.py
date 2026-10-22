"""Corpus-derived Chinese/English BM25 retrieval and weighted rank fusion.

No benchmark questions, relevance labels or document-title routing tables are
used. Character bigrams avoid requiring a Chinese segmenter or model download.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any


def tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    for span in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(span[i:i + 2] for i in range(len(span) - 1))
        if len(span) == 1:
            tokens.append(span)
    return tokens


# Short business synonyms, independent of any particular knowledge document.
SYNONYMS = (("付款", "支付"), ("账号", "账户"), ("开票", "发票"),
            ("真人", "人工"), ("包裹", "快递", "物流"), ("退钱", "退款"))


def query_tokens(query: str) -> set[str]:
    terms = set(tokenize(query))
    for group in SYNONYMS:
        if any(word in query for word in group):
            terms.update(group)
    return terms


class LexicalIndex:
    def __init__(self, items: list[dict[str, Any]]):
        self.items = items
        self.bodies = [Counter(tokenize(item["content"])) for item in items]
        self.titles = [Counter(tokenize(item["title"])) for item in items]
        self.lengths = [sum(body.values()) for body in self.bodies]
        self.avg_length = sum(self.lengths) / max(1, len(items)) or 1
        df: Counter[str] = Counter()
        for body, title in zip(self.bodies, self.titles):
            df.update(set(body) | set(title))
        self.idf = {word: math.log(1 + (len(items) - count + 0.5) / (count + 0.5))
                    for word, count in df.items()}

    def rank(self, query: str) -> list[tuple[dict[str, Any], float]]:
        terms = query_tokens(query)
        ranked = []
        for item, body, title, length in zip(self.items, self.bodies, self.titles, self.lengths):
            score = 0.0
            for term in terms:
                # BM25F: title mentions carry twice the field weight.
                tf = body[term] + 2 * title[term]
                if tf:
                    score += self.idf.get(term, 0) * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / self.avg_length))
            if score > 0:
                ranked.append((item, score))
        return sorted(ranked, key=lambda pair: (-pair[1], pair[0]["id"]))


def fuse(dense: list[dict[str, Any]], sparse: list[tuple[dict[str, Any], float]],
         top_k: int, *, dense_weight: float = 0.2) -> list[dict[str, Any]]:
    """Weighted RRF; opaque dense distances never share a scale with BM25."""
    if not sparse:
        return dense[:top_k]
    candidates: dict[str, dict[str, Any]] = {}
    scores: Counter[str] = Counter()
    for rank, item in enumerate(dense, 1):
        key = item["id"]
        candidates[key] = {**item, "dense_score": item["score"], "lexical_score": 0.0}
        scores[key] += dense_weight / (10 + rank)
    for rank, (item, score) in enumerate(sparse, 1):
        key = item["id"]
        candidates.setdefault(key, {**item, "dense_score": None})
        candidates[key]["lexical_score"] = round(score, 6)
        scores[key] += (1 - dense_weight) / (10 + rank)
    ranked = sorted(candidates, key=lambda key: (-scores[key], key))
    # Penalize repeated chunks after the first chunk from a title. Keep them
    # available for long-document answers while preventing one title flooding K.
    selected = []
    counts: Counter[str] = Counter()
    while ranked and len(selected) < top_k:
        key = max(ranked, key=lambda key: scores[key] / (1 + counts[candidates[key]["title"]]))
        ranked.remove(key)
        item = candidates[key]
        counts[item["title"]] += 1
        selected.append({**item, "score": round(scores[key], 6), "retrieval_method": "hybrid_rrf"})
    return selected
