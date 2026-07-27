#!/usr/bin/env python3
"""Validate the first four project foundations without requiring app code."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
POLICIES = ROOT / "policies"


class ValidationFailure(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationFailure(message)


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationFailure(f"无法读取 JSON {path.relative_to(ROOT)}: {exc}") from exc
    require(isinstance(value, dict), f"{path.relative_to(ROOT)} 的根节点必须是对象")
    return value


def parse_time(value: str, context: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationFailure(f"{context} 不是合法 ISO 8601 时间: {value}") from exc
    require(parsed.tzinfo is not None, f"{context} 必须包含时区: {value}")
    return parsed


def unique(values: Iterable[str], context: str) -> set[str]:
    result: set[str] = set()
    for value in values:
        require(value not in result, f"{context} 出现重复 ID: {value}")
        result.add(value)
    return result


def validate_all_json_parses() -> None:
    ignored_directories = {".git", ".venv", "node_modules", "data"}
    paths = sorted(
        path
        for path in ROOT.rglob("*.json")
        if not any(part in ignored_directories for part in path.relative_to(ROOT).parts)
    )
    require(paths, "项目中没有 JSON 文件")
    for path in paths:
        load_json(path)
    print(f"[PASS] {len(paths)} 个 JSON 文件均可解析")


def validate_state_machine() -> dict[str, Any]:
    machine = load_json(ROOT / "config" / "case_state_machine.json")
    states = machine["states"]
    state_ids = unique((item["id"] for item in states), "状态机")
    transition_ids = unique((item["id"] for item in machine["transitions"]), "状态转换")

    require(machine["initial_state"] in state_ids, "initial_state 未在 states 中定义")
    require(set(machine["terminal_states"]).issubset(state_ids), "terminal_states 含未知状态")
    declared_terminal = {item["id"] for item in states if item["is_terminal"]}
    require(declared_terminal == set(machine["terminal_states"]), "is_terminal 与 terminal_states 不一致")

    outgoing: dict[str, int] = defaultdict(int)
    transition_pairs: set[tuple[str, str, str]] = set()
    for transition in machine["transitions"]:
        require(transition["from"] in state_ids, f"{transition['id']} 的 from 状态不存在")
        require(transition["to"] in state_ids, f"{transition['id']} 的 to 状态不存在")
        pair = (transition["from"], transition["to"], transition["trigger"])
        require(pair not in transition_pairs, f"重复状态转换: {pair}")
        transition_pairs.add(pair)
        outgoing[transition["from"]] += 1

    for state_id in state_ids - set(machine["terminal_states"]):
        require(outgoing[state_id] > 0, f"非终态 {state_id} 没有任何离开路径")

    required_paths = {
        ("SUBMITTED", "EVIDENCE_LOCKED"),
        ("UNDER_INVESTIGATION", "READY_FOR_REVIEW"),
        ("HUMAN_REVIEW", "APPROVED"),
        ("EXECUTING", "RESOLVED"),
        ("RESOLVED", "APPEALED"),
        ("APPEALED", "REOPENED"),
        ("REOPENED", "UNDER_INVESTIGATION"),
    }
    actual_paths = {(item["from"], item["to"]) for item in machine["transitions"]}
    require(required_paths.issubset(actual_paths), "状态机缺少核心闭环路径")
    print(f"[PASS] 状态机：{len(state_ids)} 个状态，{len(transition_ids)} 条合法转换")
    return machine


def policy_for_time(index: dict[str, Any], policy_id: str, at: datetime) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for entry in index["policies"]:
        if entry["policy_id"] != policy_id or entry["status"] == "DRAFT":
            continue
        start = parse_time(entry["effective_from"], f"{entry['version']}.effective_from")
        end = parse_time(entry["effective_to"], f"{entry['version']}.effective_to") if entry["effective_to"] else None
        if at >= start and (end is None or at < end):
            matches.append(entry)
    require(len(matches) == 1, f"{policy_id} 在 {at.isoformat()} 应唯一选版，实际匹配 {len(matches)} 个")
    return matches[0]


def validate_policies() -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    index = load_json(POLICIES / "index.json")
    entries = index["policies"]
    unique((f"{item['policy_id']}@{item['version']}" for item in entries), "政策索引")
    policies: dict[tuple[str, str], dict[str, Any]] = {}
    intervals: dict[str, list[tuple[datetime, datetime | None, str]]] = defaultdict(list)

    for entry in entries:
        path = POLICIES / entry["path"]
        require(path.is_file(), f"政策索引文件不存在: {entry['path']}")
        policy = load_json(path)
        key = (policy["policy_id"], policy["version"])
        require(key == (entry["policy_id"], entry["version"]), f"索引与文件 ID/版本不一致: {entry['path']}")
        for field in ("status", "effective_from", "effective_to"):
            require(policy[field] == entry[field], f"索引与政策文件的 {field} 不一致: {entry['path']}")

        start = parse_time(policy["effective_from"], f"{entry['path']}.effective_from")
        end = parse_time(policy["effective_to"], f"{entry['path']}.effective_to") if policy["effective_to"] else None
        require(end is None or start < end, f"政策生效区间无效: {entry['path']}")

        requirement_ids = unique(
            (item["requirement_id"] for item in policy["evidence_requirements"]),
            f"{entry['path']} evidence_requirements",
        )
        rule_ids = unique((item["rule_id"] for item in policy["rules"]), f"{entry['path']} rules")
        authority_outcomes = set(policy["authority"]["allowed_outcomes"])
        unique(
            (item["code"] for item in policy["authority"]["mandatory_human_review_triggers"]),
            f"{entry['path']} mandatory_human_review_triggers",
        )
        for rule in policy["rules"]:
            require(set(rule["required_evidence_refs"]).issubset(requirement_ids), f"{rule['rule_id']} 引用不存在的证据要求")
            require(set(rule["allowed_outcomes"]).issubset(authority_outcomes), f"{rule['rule_id']} 使用未授权结果")
        require("DM-DEF-01" in rule_ids, f"{entry['path']} 缺少描述不符定义")
        require("DM-ESCALATE-01" in rule_ids, f"{entry['path']} 缺少强制人工规则")
        require(policy["authority"]["automatic_final_decision_allowed"] is False, "MVP 禁止自动最终裁决")
        require(policy["authority"]["human_review_required"] is True, "MVP 必须人工审核")

        policies[key] = policy
        if entry["status"] != "DRAFT":
            intervals[entry["policy_id"]].append((start, end, entry["version"]))

    for policy_id, versions in intervals.items():
        versions.sort(key=lambda item: item[0])
        for previous, current in zip(versions, versions[1:]):
            previous_end = previous[1]
            require(previous_end is not None, f"{policy_id}@{previous[2]} 无结束时间，但后面仍有版本")
            require(previous_end <= current[0], f"{policy_id} 的 {previous[2]} 与 {current[2]} 生效区间重叠")
            require(previous_end == current[0], f"{policy_id} 的 {previous[2]} 与 {current[2]} 之间存在未覆盖区间")

    require(policy_for_time(index, "marketplace.description_mismatch", parse_time("2025-07-01T12:00:00+08:00", "测试时间"))["version"] == "1.0.0", "2025 年测试交易未选中 v1")
    require(policy_for_time(index, "marketplace.description_mismatch", parse_time("2025-12-31T23:59:59+08:00", "边界测试时间"))["version"] == "1.0.0", "v1 结束边界前未选中 v1")
    require(policy_for_time(index, "marketplace.description_mismatch", parse_time("2026-01-01T00:00:00+08:00", "边界测试时间"))["version"] == "2.0.0", "v2 开始边界未选中 v2")
    require(policy_for_time(index, "marketplace.description_mismatch", parse_time("2026-07-01T12:00:00+08:00", "测试时间"))["version"] == "2.0.0", "2026 年测试交易未选中 v2")
    print(f"[PASS] 规则库：{len(policies)} 个版本，生效区间连续且互不重叠")
    return index, policies


def validate_router() -> dict[str, Any]:
    router = load_json(ROOT / "config" / "dispute_router.json")
    dispute_types = {
        "DESCRIPTION_MISMATCH",
        "MISSING_PARTS",
        "EMPTY_PACKAGE",
        "SHIPPING_DAMAGE",
        "COUNTERFEIT",
        "OTHER",
    }
    require(set(router["default_claim_types"]) == dispute_types, "Router 默认 Claim 类型未完整覆盖争议分类")
    require(router["thresholds"]["model_candidate_requires_human"] is True, "模型候选必须经过人工确认")
    require(0 <= router["thresholds"]["minimum_rule_confidence"] <= 1, "Router 最低置信度无效")
    require(0 <= router["thresholds"]["ambiguity_margin"] <= 1, "Router 歧义阈值无效")
    rule_ids = unique((item["rule_id"] for item in router["keyword_rules"]), "Router keyword_rules")
    for rule in router["keyword_rules"]:
        require(rule["issue_type"] in dispute_types, f"{rule['rule_id']} 使用未知 issue_type")
        require(bool(rule["phrases"]), f"{rule['rule_id']} 没有关键词")
        require(len(set(rule["phrases"])) == len(rule["phrases"]), f"{rule['rule_id']} 有重复关键词")
    for code, target in router["platform_reason_codes"].items():
        require(code == code.upper(), f"平台原因码必须大写: {code}")
        require(target["issue_type"] in dispute_types, f"平台原因码 {code} 使用未知 issue_type")
    print(f"[PASS] Router：{len(rule_ids)} 条文本规则，模型候选固定进入人工确认")
    return router


def validate_reference_list(values: Iterable[str], allowed: set[str], context: str) -> None:
    unknown = set(values) - allowed
    require(not unknown, f"{context} 引用未知 ID: {sorted(unknown)}")


def validate_example(
    machine: dict[str, Any],
    index: dict[str, Any],
    policies: dict[tuple[str, str], dict[str, Any]],
) -> None:
    case_file = load_json(ROOT / "examples" / "laptop_description_mismatch_case.json")
    case = case_file["case"]
    case_id = case["case_id"]
    require(case["state"] in {item["id"] for item in machine["states"]}, "示例案件状态不在状态机中")
    require(case_file["transaction_snapshot"]["transaction_id"] == case["transaction_id"], "案件与交易快照 ID 不一致")

    collections = {
        "claims": "claim_id",
        "evidence": "evidence_id",
        "evidence_assessments": "assessment_id",
        "timeline": "timeline_event_id",
        "policy_citations": "citation_id",
        "open_questions": "question_id",
        "party_analyses": "analysis_id",
        "evidence_policy_reports": "report_id",
        "decision_drafts": "decision_id",
        "guard_results": "guard_result_id",
        "resolution_actions": "action_id",
        "appeals": "appeal_id",
    }
    ids: dict[str, set[str]] = {}
    for collection, id_field in collections.items():
        ids[collection] = unique((item[id_field] for item in case_file[collection]), f"示例 {collection}")
        for item in case_file[collection]:
            require(item["case_id"] == case_id, f"{collection}.{item[id_field]} 的 case_id 不一致")

    claim_ids = ids["claims"]
    evidence_ids = ids["evidence"]
    citation_ids = ids["policy_citations"]
    question_ids = ids["open_questions"]
    decision_ids = ids["decision_drafts"]

    for claim in case_file["claims"]:
        validate_reference_list(claim["evidence_ids"], evidence_ids, claim["claim_id"])
        if claim.get("responds_to_claim_id"):
            require(claim["responds_to_claim_id"] in claim_ids, f"{claim['claim_id']} responds_to_claim_id 不存在")

    for evidence in case_file["evidence"]:
        validate_reference_list(evidence["related_claim_ids"], claim_ids, evidence["evidence_id"])
    for assessment in case_file["evidence_assessments"]:
        require(assessment["evidence_id"] in evidence_ids, f"{assessment['assessment_id']} 的 evidence_id 不存在")
    for event in case_file["timeline"]:
        validate_reference_list(event["source_evidence_ids"], evidence_ids, event["timeline_event_id"])

    paid_at = parse_time(case_file["transaction_snapshot"]["paid_at"], "transaction_snapshot.paid_at")
    selection = case["policy_selection"]
    require(parse_time(selection["basis_time"], "case.policy_selection.basis_time") == paid_at, "政策选择基准时间必须等于 paid_at")
    selected_entry = policy_for_time(index, selection["policy_id"], paid_at)
    require(selected_entry["version"] == selection["version"], "案件固定的政策版本与确定性选版结果不一致")
    selected_policy = policies[(selection["policy_id"], selection["version"])]
    selected_rule_ids = {item["rule_id"] for item in selected_policy["rules"]}

    for citation in case_file["policy_citations"]:
        require(citation["policy_id"] == selection["policy_id"], f"{citation['citation_id']} 引用了其他 policy_id")
        require(citation["policy_version"] == selection["version"], f"{citation['citation_id']} 引用了错误政策版本")
        require(citation["rule_id"] in selected_rule_ids, f"{citation['citation_id']} 引用了不存在的 rule_id")
        expected_key = f"{citation['policy_id']}@{citation['policy_version']}#{citation['rule_id']}"
        require(citation["citation_key"] == expected_key, f"{citation['citation_id']} citation_key 不一致")
        validate_reference_list(citation["applies_to_claim_ids"], claim_ids, citation["citation_id"])

    for question in case_file["open_questions"]:
        validate_reference_list(question["resolves_claim_ids"], claim_ids, question["question_id"])
        validate_reference_list(question.get("response_evidence_ids", []), evidence_ids, question["question_id"])
    for analysis in case_file["party_analyses"]:
        validate_reference_list((item["claim_id"] for item in analysis["claim_assessments"]), claim_ids, analysis["analysis_id"])
        validate_reference_list(analysis["proposed_question_ids"], question_ids, analysis["analysis_id"])
        for item in analysis["claim_assessments"]:
            validate_reference_list(item["supporting_evidence_ids"], evidence_ids, analysis["analysis_id"])
            validate_reference_list(item["contrary_evidence_ids"], evidence_ids, analysis["analysis_id"])
    for report in case_file["evidence_policy_reports"]:
        validate_reference_list(report["timeline_event_ids"], ids["timeline"], report["report_id"])
        validate_reference_list(report["evidence_assessment_ids"], ids["evidence_assessments"], report["report_id"])
        validate_reference_list(report["policy_citation_ids"], citation_ids, report["report_id"])
        for conflict in report["conflicts"]:
            validate_reference_list(conflict["evidence_ids"], evidence_ids, conflict["conflict_id"])

    material_claim_ids = {item["claim_id"] for item in case_file["claims"] if item["material"]}
    paid_amount = case_file["transaction_snapshot"]["paid_amount"]["amount_minor"]
    allowed_outcomes = set(selected_policy["authority"]["allowed_outcomes"])
    for decision in case_file["decision_drafts"]:
        require(decision["outcome"] in allowed_outcomes, f"{decision['decision_id']} 的 outcome 未被政策允许")
        finding_claim_ids = {item["claim_id"] for item in decision["claim_findings"]}
        require(material_claim_ids.issubset(finding_claim_ids), f"{decision['decision_id']} 遗漏关键主张")
        for finding in decision["claim_findings"]:
            require(finding["claim_id"] in claim_ids, f"{decision['decision_id']} 引用未知 claim_id")
            validate_reference_list(finding["evidence_ids"], evidence_ids, decision["decision_id"])
            validate_reference_list(finding["policy_citation_ids"], citation_ids, decision["decision_id"])
        for fact in decision["established_facts"]:
            require(fact["evidence_ids"], f"{fact['fact_id']} 没有证据")
            validate_reference_list(fact["evidence_ids"], evidence_ids, fact["fact_id"])
        validate_reference_list(decision["unresolved_question_ids"], question_ids, decision["decision_id"])
        if decision["refund_amount"] is not None:
            require(decision["refund_amount"]["amount_minor"] <= paid_amount, f"{decision['decision_id']} 退款超过实付金额")
        require(decision["requires_human_review"] is True, f"{decision['decision_id']} 绕过了 MVP 人工审核")

    for guard in case_file["guard_results"]:
        require(guard["decision_id"] in decision_ids, f"{guard['guard_result_id']} 引用未知 decision_id")
        if guard["passed"]:
            require(not any(item["severity"] == "BLOCK" for item in guard["violations"]), f"{guard['guard_result_id']} passed=true 但包含 BLOCK")
    unique((item["idempotency_key"] for item in case_file["resolution_actions"]), "示例 resolution_actions idempotency_key")
    for action in case_file["resolution_actions"]:
        require(action["decision_id"] in decision_ids, f"{action['action_id']} 引用未知 decision_id")
        if action["amount"] is not None:
            require(action["amount"]["amount_minor"] <= paid_amount, f"{action['action_id']} 金额超过实付金额")
    for appeal in case_file["appeals"]:
        require(appeal["decision_id"] in decision_ids, f"{appeal['appeal_id']} 引用未知 decision_id")
        validate_reference_list(appeal["evidence_ids"], evidence_ids, appeal["appeal_id"])

    print("[PASS] 示例案件的案件、主张、证据、规则、决定和 Guard 引用一致")


def optional_jsonschema_validation() -> None:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from referencing import Registry, Resource
    except ModuleNotFoundError:
        print("[SKIP] 未安装 jsonschema；已完成标准库语义校验，可安装 requirements-dev.txt 后补充 Schema 校验")
        return

    schema_paths = sorted(SCHEMAS.glob("*.schema.json"))
    schemas = {path.name: load_json(path) for path in schema_paths}
    registry = Registry()
    for schema in schemas.values():
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    for name, schema in schemas.items():
        Draft202012Validator.check_schema(schema)

    validations = [
        ("state-machine.schema.json", ROOT / "config" / "case_state_machine.json"),
        ("dispute-router.schema.json", ROOT / "config" / "dispute_router.json"),
        ("policy-index.schema.json", POLICIES / "index.json"),
        ("case-file.schema.json", ROOT / "examples" / "laptop_description_mismatch_case.json"),
    ]
    validations.extend(
        ("policy-version.schema.json", path)
        for path in sorted((POLICIES / "description_mismatch").glob("*.json"))
    )
    for schema_name, instance_path in validations:
        schema = schemas[schema_name]
        validator = Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(load_json(instance_path)), key=lambda item: list(item.absolute_path))
        if errors:
            detail = "; ".join(f"{list(error.absolute_path)}: {error.message}" for error in errors[:5])
            raise ValidationFailure(f"Schema 校验失败 {instance_path.relative_to(ROOT)}: {detail}")
    print(f"[PASS] {len(schema_paths)} 个 Schema 合法，{len(validations)} 个实例通过 Draft 2020-12 校验")


def main() -> int:
    try:
        validate_all_json_parses()
        machine = validate_state_machine()
        index, policies = validate_policies()
        validate_router()
        validate_example(machine, index, policies)
        optional_jsonschema_validation()
    except ValidationFailure as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    print("[PASS] 前四步基础设计校验完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
