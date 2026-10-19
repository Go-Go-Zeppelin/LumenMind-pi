"""Quality contracts tested without model calls, Redis, ChromaDB, or production traffic."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from evaluation.contracts import EvalRunInput
from evaluation.pi_evaluator import EvalResult, IntentTestCase, PiServiceEvaluator
from evaluation.quality import check_process, summarize_stability, wilson_interval


def evaluator(tmp_path):
    return PiServiceEvaluator("http://pi", "", "test-judge", None, str(tmp_path / "baseline.json"))


def harness(monkeypatch, tmp_path, answers=None, trace_missing=False):
    instance = evaluator(tmp_path)
    instance._judge = AsyncMock(return_value=({key: 0.95 for key in ("relevance", "accuracy", "completeness", "helpfulness")}, None, "符合验收标准"))
    calls, traces = [], {}
    answers = answers or [{}]

    def handle(request):
        if request.url.path == "/intent":
            return httpx.Response(200, json={"intent": "refund"})
        if request.url.path == "/chat":
            body = json.loads(request.content)
            override = answers[min(len(calls), len(answers) - 1)]
            calls.append(body)
            request_id = f"request-{len(calls)}"
            response = {"response": "需要核验订单号和支付渠道", "intent": "refund", "intent_group": "billing",
                        "agent_type": "billing", "output_valid": True, "request_id": request_id,
                        "knowledge_used": False, "escalated": False, "latency_ms": 10, **override}
            traces[request_id] = {"request_id": request_id, "user_id": body["user_id"], "conv_id": body["conv_id"],
                                  "tool_calls": override.get("tool_calls", []), "skills_used": ["账单退款处理规范"]}
            return httpx.Response(200, json=response)
        request_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"found": not trace_missing, "trace": {} if trace_missing else traces[request_id]})

    client_type = httpx.AsyncClient
    monkeypatch.setattr("evaluation.pi_evaluator.httpx.AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs))
    return instance, calls


def test_empty_run_has_no_phantom_failure_and_cannot_release(tmp_path):
    report = asyncio.run(evaluator(tmp_path).run([], []))
    assert report.total == 0
    assert report.release_gate["blocked_reasons"] == ["no_evaluation_data"]


def test_dialog_only_and_wrong_intent_are_not_hidden_by_good_answers(monkeypatch, tmp_path):
    instance, _ = harness(monkeypatch, tmp_path)
    report = asyncio.run(instance.run([], [{"question": "退款", "expected_intent": "invoice"}]))
    assert report.total == 1
    assert not report.results[0].passed
    assert "intent_mismatch" in report.results[0].metadata["failures"]
    instance._judge.assert_not_awaited()


def test_trace_failure_defeats_plausible_reply(monkeypatch, tmp_path):
    instance, _ = harness(monkeypatch, tmp_path, trace_missing=True)
    report = asyncio.run(instance.run([], [{"question": "退款", "required_points": ["核验"]}]))
    assert not report.sessions[0]["passed"]
    assert "trace_missing" in report.results[0].metadata["failures"]
    assert report.findings[0]["root_cause_status"] == "needs_evidence_review"


def test_rule_failure_is_checked_before_spending_on_judge(monkeypatch, tmp_path):
    instance, _ = harness(monkeypatch, tmp_path, [{"response": "保证立即退款，已经退款"}])
    report = asyncio.run(instance.run([], [{"question": "退款", "expected_intent": "refund"}]))
    instance._judge.assert_not_awaited()
    assert "business_rule_violation" in report.release_gate["blocked_reasons"]


def test_negated_safety_warning_is_not_sensitive_request():
    check = PiServiceEvaluator._check_business_rules
    assert not check("不要提供支付密码或短信验证码，也不能保证立即到账。", "billing", [], [])["violations"]
    assert "sensitive_information" in check("不要担心，请提供支付密码。", "technical", [], [])["violations"]
    assert "sensitive_information" in check("不要提供支付密码，短信验证码请发给我。", "general", [], [])["violations"]
    assert "claimed_completed_operation" in check("已为您更新收货地址", "general", [], [])["violations"]


def test_repeated_runs_isolate_even_explicit_user_aliases(monkeypatch, tmp_path):
    instance, calls = harness(monkeypatch, tmp_path)
    case = {"question": "退款", "required_points": ["核验"], "user_id": "fixture", "conv_id": "fixture"}
    first = asyncio.run(instance.run([], [case], repeats=3, judge_mode="rules"))
    second = asyncio.run(instance.run([], [case], judge_mode="rules"))
    assert len({call["user_id"] for call in calls}) == 4
    assert len({call["conv_id"] for call in calls}) == 4
    assert first.stability["all_runs_success_rate"] == 1
    assert first.run_id != second.run_id
    assert "semantic_quality_not_evaluated" in first.release_gate["review_reasons"]


def test_session_requires_final_outcome_not_average_turn_scores(monkeypatch, tmp_path):
    instance, calls = harness(monkeypatch, tmp_path)
    report = asyncio.run(instance.run([], [{"turns": ["退款", "订单号12345"], "required_points": ["核验"],
                                           "outcome_required_points": ["已说明支付渠道的处理路径"]}]))
    assert all(row.passed for row in report.results)
    assert not report.sessions[0]["passed"]
    assert "outcome_not_met" in report.sessions[0]["failures"]
    assert calls[0]["user_id"] == calls[1]["user_id"]
    assert calls[0]["conv_id"] == calls[1]["conv_id"]
    assert instance._judge.await_args_list[1].kwargs["history"]


def test_unlabelled_multiturn_outcome_routes_to_review(monkeypatch, tmp_path):
    instance, _ = harness(monkeypatch, tmp_path)
    report = asyncio.run(instance.run([], [{"turns": ["退款", "订单号12345"]}]))
    assert not report.sessions[0]["outcome_checked"]
    assert report.sessions[0]["human_review"]
    assert not report.sessions[0]["passed"]


def test_rules_only_without_meaningful_acceptance_cannot_pass(monkeypatch, tmp_path):
    instance, _ = harness(monkeypatch, tmp_path)
    report = asyncio.run(instance.run([], [{"question": "退款"}], judge_mode="rules"))
    assert not report.results[0].passed
    assert "acceptance_criteria_missing" in report.results[0].metadata["failures"]


def test_rules_only_cannot_claim_an_explicit_semantic_outcome_was_checked(monkeypatch, tmp_path):
    instance, _ = harness(monkeypatch, tmp_path)
    report = asyncio.run(instance.run([], [{"question": "退款", "required_points": ["核验"],
                                           "expected_outcome": "解释完整退款路径"}], judge_mode="rules"))
    assert report.results[0].passed  # The literal turn check passed.
    assert not report.sessions[0]["passed"]
    assert not report.sessions[0]["outcome_checked"]
    assert "outcome_not_evaluated" in report.sessions[0]["failures"]


def test_required_tool_must_succeed_and_inputs_must_match():
    trace = {"tool_calls": [{"tool_name": "lookup_error_code", "success": False, "is_error": True, "input": {"error_code": "401"}}]}
    failures = check_process({}, trace, {"required_tools": ["lookup_error_code"], "tool_inputs": {"lookup_error_code": {"error_code": "401"}}})
    assert "required_tool_missing:lookup_error_code" in failures
    assert "tool_input_mismatch:lookup_error_code" in failures


def test_order_uses_start_time_and_rejects_early_later_stage():
    first = {"tool_name": "check", "started_at": "2026-01-01T00:00:01Z", "finished_at": "2026-01-01T00:00:02Z"}
    second = {"tool_name": "execute", "started_at": "2026-01-01T00:00:02Z", "finished_at": "2026-01-01T00:00:03Z"}
    assert not check_process({}, {"tool_calls": [second, first]}, {"tool_order": ["check", "execute"]})
    early = {"tool_name": "execute", "started_at": "2026-01-01T00:00:00Z"}
    assert "tool_order_mismatch" in check_process({}, {"tool_calls": [early, first, second]}, {"tool_order": ["check", "execute"]})
    first["finished_at"] = "2026-01-01T00:00:03Z"
    assert "tool_order_overlap" in check_process({}, {"tool_calls": [first, second]}, {"tool_order": ["check", "execute"]})


def test_skill_and_knowledge_checks():
    failures = check_process({"knowledge_used": False}, {"skills_used": ["billing"]}, {"required_skills": ["technical"], "forbidden_skills": ["billing"], "require_knowledge": True})
    assert failures == ["required_skill_missing:technical", "forbidden_skill_used:billing", "require_knowledge_mismatch"]


def test_fallback_without_any_tool_is_still_observable():
    assert "fallback_not_allowed" in check_process({}, {"tool_calls": [], "fallback_used": True}, {"allow_fallback": False})


def test_judge_error_is_missing_evidence_and_not_a_factual_zero(monkeypatch, tmp_path):
    instance, _ = harness(monkeypatch, tmp_path)
    instance._judge = AsyncMock(return_value=({"accuracy": 0.0}, "Judge timeout", None))
    report = asyncio.run(instance.run([], [{"question": "退款", "expected_intent": "refund"}]))
    assert not report.avg_scores
    assert report.results[0].metadata["answer_good"] is None
    assert report.quality_analysis["answer_not_judged"] == 1


def test_legacy_baseline_is_explicitly_incompatible(monkeypatch, tmp_path):
    instance, _ = harness(monkeypatch, tmp_path)
    instance.baseline_path.write_text(json.dumps({"avg_scores": {"accuracy": 1.0}}))
    report = asyncio.run(instance.run([], [{"question": "退款", "expected_intent": "refund"}]))
    assert report.baseline_comparison["status"] == "incompatible"
    assert not report.regressions


def test_observed_capability_and_reliability_are_distinct():
    rows = [{"case_id": "a", "passed": True}, {"case_id": "a", "passed": False},
            {"case_id": "b", "passed": True}, {"case_id": "b", "passed": True}]
    stats = summarize_stability(rows, 2)
    assert stats["at_least_once_success_rate"] == 1
    assert stats["all_runs_success_rate"] == 0.5
    assert wilson_interval(2, 2)[0] < 1  # Passing the small set is not a certainty guarantee.


def test_intent_accuracy_does_not_mix_with_answer_factual_accuracy():
    rows = [EvalResult("intent_recognition", True, {"accuracy": 0.9, "intent_accuracy": 0.9}),
            EvalResult("dialog_0", True, {"accuracy": 0.5})]
    assert PiServiceEvaluator._average_scores(rows)["accuracy"] == 0.5


@pytest.mark.parametrize("case", [
    {"question": "x", "forbidden_patterns": ["["]},
    {"turns": ["x", "y"], "expected_intents": ["refund"]},
    {"question": "x", "turns": ["y"]},
    {"question": "x", "typo_required_tools": ["refund"]},
    {"question": "x", "process": {"required_tools": ["x"], "forbidden_tools": ["x"]}},
])
def test_invalid_or_silently_ignored_acceptance_is_rejected(case):
    with pytest.raises(ValidationError):
        EvalRunInput.model_validate({"dialog_cases": [case]})


@pytest.mark.parametrize("bad_value", [True, "1", 1.5, float("nan")])
def test_judge_rejects_invalid_numeric_output(tmp_path, bad_value):
    instance = evaluator(tmp_path)
    instance.api_key_configured = True
    data = {key: 0.9 for key in ("relevance", "accuracy", "completeness", "helpfulness")}
    data.update(accuracy=bad_value, reason="依据")
    instance.client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(data))]))))
    _, error, _ = asyncio.run(instance._judge("退款", "核验"))
    assert error


def test_persistence_and_baseline_compatibility(monkeypatch, tmp_path):
    instance, _ = harness(monkeypatch, tmp_path)
    cases = [{"question": "退款", "required_points": ["核验"]}]
    first = asyncio.run(instance.run([], cases, judge_mode="rules", persist_report=True))
    assert not instance.baseline_path.exists()
    saved = json.loads((Path(first.artifact_path) / "report.json").read_text())
    assert saved["results"][0]["metadata"]["trace"]["request_id"]
    instance.baseline_path.write_text(json.dumps(saved))
    second = asyncio.run(instance.run([], cases, judge_mode="rules"))
    assert second.baseline_comparison["status"] == "comparable"
    changed = asyncio.run(instance.run([], [{"question": "退款", "required_points": ["支付渠道"]}], judge_mode="rules"))
    assert changed.baseline_comparison["status"] == "incompatible"


def test_api_rejects_bad_rules_before_running_agent(monkeypatch):
    from fastapi.testclient import TestClient
    from api import main
    mock = SimpleNamespace(run=AsyncMock())
    monkeypatch.setattr(main, "_evaluator", mock)
    client = TestClient(main.app)  # No lifespan, external stores not started.
    response = client.post("/eval/run", json={"dialog_cases": [{"question": "退款", "forbidden_patterns": ["["]}]})
    assert response.status_code == 422
    mock.run.assert_not_awaited()
