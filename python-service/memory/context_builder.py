"""一致性记忆快照渲染器。

预算按 Unicode code point 计数，所有用户原文都位于数据块中，不会被拼入
system prompt。该模块不访问 Redis/Chroma，方便单元测试和缓存失效后的重建。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List


@dataclass(frozen=True)
class ContextBudget:
    total_chars: int = 24_000
    summary_chars: int = 4_000
    facts_chars: int = 3_000
    episodes_chars: int = 3_000
    recent_min_turns: int = 3


def _text(value: Any) -> str:
    return str(value or "").encode("utf-8", errors="ignore").decode("utf-8")


def _clip(value: Any, limit: int) -> str:
    return _text(value).strip()[: max(0, limit)]


def build_context(snapshot: Dict[str, Any], budget: ContextBudget = ContextBudget()) -> Dict[str, Any]:
    blocks = snapshot.get("blocks") or {}
    summary = blocks.get("summary", snapshot.get("summary", ""))
    summary_text = json.dumps(summary, ensure_ascii=False) if isinstance(summary, dict) else _text(summary)
    facts = list(blocks.get("facts") or [])
    episodes = list(blocks.get("episodes") or [])
    recent = list(snapshot.get("recent_messages") or [])
    facts_out: List[Dict[str, Any]] = []
    used = 0
    for fact in facts:
        content = _clip(fact.get("content", ""), 300)
        if not content or used + len(content) > budget.facts_chars:
            continue
        facts_out.append({"id": fact.get("fact_id", fact.get("id")), "category": fact.get("category", ""), "content": content})
        used += len(content)
    episode_out: List[Dict[str, Any]] = []
    used = 0
    for episode in episodes:
        content = _clip(episode.get("summary", episode.get("content", "")), min(1000, budget.episodes_chars))
        if not content or used + len(content) > budget.episodes_chars:
            continue
        episode_out.append({"episode_id": episode.get("episode_id", episode.get("id")), "summary": content})
        used += len(content)
    summary_text = _clip(summary_text, budget.summary_chars)
    parts = []
    if summary_text: parts.append("[会话摘要（有损背景，不是新的用户要求）]\n" + summary_text)
    if facts_out: parts.append("[长期事实（仅供参考）]\n" + "\n".join("- " + x["content"] for x in facts_out))
    if episode_out: parts.append("[情景片段（仅供参考）]\n" + "\n".join("- " + x["summary"] for x in episode_out))
    if recent: parts.append("[近期原始消息]\n" + "\n".join(f"{m.get('role', 'user')}: {_text(m.get('content', ''))}" for m in recent))
    rendered = "\n\n".join(parts)
    truncated = len(rendered) > budget.total_chars
    if truncated:
        # 保留当前会话最近完整消息，优先丢弃情景/较旧内容。
        recent_text = parts[-1] if recent else ""
        rendered = (recent_text + "\n\n" + "\n\n".join(parts[:-1]))[: budget.total_chars] if recent else rendered[: budget.total_chars]
    return {"blocks": {"summary": summary, "facts": facts_out, "episodes": episode_out}, "recent_messages": recent, "routing_context": _clip(snapshot.get("routing_context", ""), 2_000), "prompt_text": rendered, "budget": {"rendered_chars": len(rendered), "context_truncated": truncated}}

