"""Validated evaluation contracts shared by the HTTP API and offline runner."""

import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvalIntentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1)
    expected_intent: str = Field(min_length=1)
    context: Optional[Dict[str, Any]] = None


class ProcessExpectations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required_tools: List[str] = Field(default_factory=list)
    forbidden_tools: List[str] = Field(default_factory=list)
    tool_inputs: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    tool_order: List[str] = Field(default_factory=list)
    required_skills: List[str] = Field(default_factory=list)
    forbidden_skills: List[str] = Field(default_factory=list)
    expected_agent: Optional[str] = None
    expected_escalated: Optional[bool] = None
    require_knowledge: Optional[bool] = None
    allow_fallback: bool = True
    max_latency_ms: Optional[float] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_order(self):
        if len(set(self.tool_order)) != len(self.tool_order):
            raise ValueError("tool_order 每个工具只列一次，表示严格阶段顺序")
        if set(self.required_tools) & set(self.forbidden_tools):
            raise ValueError("同一工具不能同时要求和禁止")
        return self


class EvalDialogInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: Optional[str] = None
    category: str = "general"
    priority: Literal["P0", "P1", "P2"] = "P1"
    question: Optional[str] = Field(default=None, min_length=1)
    turns: Optional[List[str]] = Field(default=None, min_length=1)
    user_id: Optional[str] = None
    conv_id: Optional[str] = None
    expected_intent: Optional[str] = None
    expected_intents: Optional[List[Optional[str]]] = None
    reference_answer: Optional[str] = None
    reference_answers: Optional[List[Optional[str]]] = None
    required_points: List[Any] = Field(default_factory=list)
    forbidden_patterns: List[Any] = Field(default_factory=list)
    process: ProcessExpectations = Field(default_factory=ProcessExpectations)
    turn_process: Optional[List[ProcessExpectations]] = None
    outcome_required_points: List[str] = Field(default_factory=list)
    outcome_forbidden_patterns: List[str] = Field(default_factory=list)
    expected_outcome: Optional[str] = None

    @model_validator(mode="after")
    def validate_case(self):
        if bool(self.question) == bool(self.turns):
            raise ValueError("必须提供 question 或 turns，且只能提供其中一种")
        turns = self.turns or [self.question]
        if any(not str(turn).strip() for turn in turns):
            raise ValueError("对话轮次不能为空")
        for name in ("expected_intents", "reference_answers", "turn_process"):
            value = getattr(self, name)
            if value is not None and len(value) != len(turns):
                raise ValueError(f"{name} 必须与 turns 等长")
        for name in ("required_points", "forbidden_patterns"):
            value = getattr(self, name)
            if value and all(isinstance(item, list) for item in value):
                if len(value) != len(turns):
                    raise ValueError(f"{name} 的逐轮列表必须与 turns 等长")
                strings = [item for row in value for item in row]
            else:
                strings = value
            if any(not isinstance(item, str) or not item.strip() for item in strings):
                raise ValueError(f"{name} 只能包含非空字符串或逐轮字符串列表")
            if name == "forbidden_patterns":
                for pattern in strings:
                    try:
                        re.compile(pattern)
                    except re.error as error:
                        raise ValueError(f"非法评测正则: {pattern}") from error
        for pattern in self.outcome_forbidden_patterns:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(f"非法会话正则: {pattern}") from error
        return self


class EvalRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_cases: Optional[List[EvalIntentInput]] = None
    dialog_cases: Optional[List[EvalDialogInput]] = None
    record_metrics: bool = True
    repeats: int = Field(default=1, ge=1, le=10)
    judge_mode: Literal["llm", "rules"] = "llm"
    dataset_version: str = "custom"
    persist_report: bool = False
