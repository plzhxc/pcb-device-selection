"""Offline applicability pilot with label-blind retrieval and explicit abstention."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import json
import math
import re
from typing import Any

from .applicability import assess
from .models import Fragment, Target, canonical_digest


EXPERIMENT_SCHEMA = "pcbagent.application_circuit_experiment.v1"
REPORT_SCHEMA = "pcbagent.application_circuit_experiment_report.v1"
STATUSES = ("applicable", "inapplicable", "unresolved", "rejected")
METHODS = ("description_overlap", "identity_only", "conditional")


def _json_value(value: Any, path: str = "experiment") -> Any:
    """Copy JSON values while rejecting silent coercions and nonfinite numbers."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path}: numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError(f"{path}: object keys must be strings")
        return {key: _json_value(item, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise ValueError(f"{path}: expected a JSON value")


def _object(value: Any, fields: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{path}: expected exactly {', '.join(sorted(fields))}")
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: expected a nonempty string")
    return value


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _tokens(text: str) -> set[str]:
    # Individual CJK characters avoid treating an entire unsegmented sentence as
    # one token. This is deliberately a transparent control, not a RAG replica.
    return set(re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", text.casefold()))


def _overlap(query: str, description: str) -> float:
    query_tokens, document_tokens = _tokens(query), _tokens(description)
    union = query_tokens | document_tokens
    return len(query_tokens & document_tokens) / len(union) if union else 0.0


def _same_identity(fragment: Fragment, target: Target) -> bool:
    return all(
        getattr(fragment.device, field) == getattr(target.device, field)
        for field in ("manufacturer", "model", "package")
    )


def _validate(data: Mapping[str, Any]) -> tuple[dict[str, Any], list[Fragment], list[tuple[dict[str, Any], Target]]]:
    experiment = _object(
        _json_value(data),
        {"schema_version", "fragments", "accepted_digests", "tasks", "provenance"},
        "experiment",
    )
    if experiment["schema_version"] != EXPERIMENT_SCHEMA:
        raise ValueError(f"experiment.schema_version: expected {EXPERIMENT_SCHEMA}")
    if not isinstance(experiment["provenance"], dict):
        raise ValueError("experiment.provenance: expected a JSON object")
    accepted = experiment["accepted_digests"]
    if not isinstance(accepted, dict):
        raise ValueError("experiment.accepted_digests: expected an object")
    for source_id, digest in accepted.items():
        _text(source_id, "accepted_digests source id")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError(f"accepted_digests.{source_id}: expected a lowercase SHA-256 digest")
    for field in ("fragments", "tasks"):
        if not isinstance(experiment[field], list) or not experiment[field]:
            raise ValueError(f"experiment.{field}: expected a nonempty array")
    fragments = [Fragment.model_validate(item) for item in experiment["fragments"]]
    fragment_ids = [fragment.id for fragment in fragments]
    if len(set(fragment_ids)) != len(fragment_ids):
        raise ValueError("experiment.fragments: duplicate fragment id")
    tasks: list[tuple[dict[str, Any], Target]] = []
    task_ids: set[str] = set()
    for index, raw in enumerate(experiment["tasks"]):
        task = _object(raw, {"id", "query", "target", "expected"}, f"tasks[{index}]")
        task_id = _text(task["id"], f"tasks[{index}].id")
        _text(task["query"], f"tasks[{index}].query")
        if task_id in task_ids:
            raise ValueError(f"experiment.tasks: duplicate task id {task_id!r}")
        task_ids.add(task_id)
        target = Target.model_validate(task["target"])
        if target.id != task_id:
            raise ValueError(f"tasks[{index}]: target id must equal task id")
        expected = task["expected"]
        if not isinstance(expected, dict) or set(expected) != set(fragment_ids):
            raise ValueError(f"tasks[{index}].expected: must label every fragment exactly once")
        if any(status not in STATUSES for status in expected.values()):
            raise ValueError(f"tasks[{index}].expected: unknown applicability status")
        tasks.append((task, target))
    return experiment, fragments, tasks


def _predict(
    task: Mapping[str, Any], target: Target, fragments: list[Fragment], accepted: Mapping[str, str]
) -> dict[str, Any]:
    """Prediction sees query and target only; no expected labels are accepted."""
    ranking = sorted(
        ({"fragment_id": item.id, "score": _overlap(task["query"], item.description)} for item in fragments),
        key=lambda item: (-item["score"], item["fragment_id"]),
    )
    identity = {item.id: _same_identity(item, target) for item in fragments}
    assessments = {
        item.id: _json_value(assess(item, target, accepted_digests=accepted)) for item in fragments
    }
    for fragment_id, assessment in assessments.items():
        if not isinstance(assessment, dict) or assessment.get("status") not in STATUSES:
            raise ValueError(f"assess returned an invalid status for {fragment_id!r}")
    candidates = {
        "description_overlap": [item for item in ranking if item["score"] > 0],
        "identity_only": [item for item in ranking if item["score"] > 0 and identity[item["fragment_id"]]],
        "conditional": [item for item in ranking if assessments[item["fragment_id"]]["status"] == "applicable"],
    }
    return {
        "task_id": task["id"],
        "ranking": ranking,
        "identity_eligible": identity,
        "assessments": assessments,
        "selected": {method: items[0]["fragment_id"] if items else None for method, items in candidates.items()},
    }


def evaluate_experiment(data: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate a completely annotated, curated applicability pilot.

    Expected labels are held outside prediction and used only after all methods
    have selected candidates. This evaluates the supplied annotations, not their
    truth, visual extraction quality, or downstream electrical functionality.
    """
    experiment, fragments, tasks = _validate(data)
    predictions = [
        _predict({"id": task["id"], "query": task["query"]}, target, fragments, experiment["accepted_digests"])
        for task, target in tasks
    ]
    counters: dict[str, Counter[str]] = {method: Counter() for method in METHODS}
    confusion = {expected: {predicted: 0 for predicted in STATUSES} for expected in STATUSES}
    pairwise: list[dict[str, Any]] = []
    task_results: list[dict[str, Any]] = []
    oracle_tasks = 0
    for (task, _target), prediction in zip(tasks, predictions, strict=True):
        expected = task["expected"]
        oracle_available = "applicable" in expected.values()
        oracle_tasks += int(oracle_available)
        selections = {}
        for method in METHODS:
            selected = prediction["selected"][method]
            selected_expected = expected[selected] if selected is not None else None
            correct = selected_expected == "applicable"
            unsafe = selected is not None and not correct
            counters[method].update({"selected": int(selected is not None), "correct": int(correct), "unsafe": int(unsafe)})
            selections[method] = {
                "selected_fragment_id": selected,
                "selected_expected_status": selected_expected,
                "correct_selection": correct,
                "unsafe_selection": unsafe,
                "abstained": selected is None,
            }
        for ranked in prediction["ranking"]:
            fragment_id = ranked["fragment_id"]
            assessment = prediction["assessments"][fragment_id]
            predicted = assessment["status"]
            confusion[expected[fragment_id]][predicted] += 1
            pairwise.append({
                "task_id": task["id"], "fragment_id": fragment_id,
                "description_overlap_score": ranked["score"],
                "identity_eligible": prediction["identity_eligible"][fragment_id],
                "expected_status": expected[fragment_id], "predicted_status": predicted,
                "assessment": assessment,
            })
        task_results.append({
            "task_id": task["id"], "oracle_has_applicable_fragment": oracle_available,
            "ranking": prediction["ranking"], "selections": selections,
        })
    task_count, pair_count = len(tasks), len(pairwise)
    metrics = {}
    for method, counts in counters.items():
        metrics[method] = {
            "tasks": task_count, "selected_tasks": counts["selected"],
            "abstained_tasks": task_count - counts["selected"],
            "coverage": _ratio(counts["selected"], task_count),
            "correct_selections": counts["correct"],
            "correct_selection_rate_all_tasks": _ratio(counts["correct"], task_count),
            "correct_selection_recall_eligible_tasks": _ratio(counts["correct"], oracle_tasks),
            "unsafe_selections": counts["unsafe"],
            "unsafe_selection_rate_selected_tasks": _ratio(counts["unsafe"], counts["selected"]),
        }
    actual_applicable = sum(confusion["applicable"].values())
    predicted_applicable = sum(row["applicable"] for row in confusion.values())
    true_applicable = confusion["applicable"]["applicable"]
    report = {
        "schema_version": REPORT_SCHEMA,
        "experiment_digest": canonical_digest(experiment),
        "study_type": "curated applicability pilot",
        "limitations": [
            "Supplied labels are curated annotations; their correctness is not established by this evaluator.",
            "This is not automatic visual extraction, an electrical functionality test, or a complete RAG benchmark.",
            "description_overlap is a transparent token-Jaccard retrieval control, not a MuaLLM reproduction.",
            "An applicable decision is scoped to represented constraints and accepted evidence; it is not a circuit correctness proof.",
        ],
        "protocol": {
            "ranking": "Descending token-set Jaccard overlap of query and fragment description; ties use ascending fragment id.",
            "description_overlap": "Select the highest positive-overlap candidate; abstain if there are none.",
            "identity_only": "Use the same positive-overlap ranking, restricted to exact manufacturer/model/package identity.",
            "conditional": "Assess every candidate; select the highest-ranked applicable candidate, including zero-overlap candidates; otherwise abstain.",
            "unsafe_selection": "A selected fragment has any curated expected status other than applicable, including unresolved or rejected.",
            "label_separation": "Expected labels are used only for scoring after all predictions are computed.",
        },
        "provenance": experiment["provenance"],
        "oracle": {"eligible_tasks": oracle_tasks, "tasks": task_count, "eligible_coverage": _ratio(oracle_tasks, task_count)},
        "methods": metrics,
        "conditional_pairwise": {
            "pairs": pair_count, "confusion_matrix": confusion,
            "actual_applicable": actual_applicable, "predicted_applicable": predicted_applicable,
            "false_applicable": predicted_applicable - true_applicable,
            "applicable_precision": _ratio(true_applicable, predicted_applicable),
            "applicable_recall": _ratio(true_applicable, actual_applicable),
            "applicable_prediction_coverage": _ratio(predicted_applicable, pair_count),
        },
        "tasks": task_results, "pairwise": pairwise,
    }
    # Fail here, rather than let a downstream writer emit non-standard NaN JSON.
    json.dumps(report, allow_nan=False)
    return report
