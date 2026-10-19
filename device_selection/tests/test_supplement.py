"""需求回填规则测试：只提有把握的建议，不用猜测覆盖待定需求。"""

from __future__ import annotations

from types import SimpleNamespace

from src.supplement import pending, propose


class Attr:
    def __init__(
        self,
        field_key: str,
        name_zh: str,
        value: str,
        *,
        missing: bool = False,
        rank: int = 1,
    ) -> None:
        self.field_key = field_key
        self.name_zh = name_zh
        self.display_value = value
        self.is_missing = missing
        self.evidence_status = "EXPECTED_BUT_MISSING" if missing else "PROVEN"
        self.priority_rank = rank
        self.quantity = None


def _raw(name: str, state: str = "tbd", value: str = "待确认") -> dict:
    return {
        "requirements": [
            {
                "id": "REQ-001",
                "name": name,
                "value": value,
                "unit": "",
                "state": state,
                "category": "performance",
            }
        ]
    }


def _result(related: list[str], models: list[tuple[str, int]]) -> dict:
    return {
        "items": [
            {
                "selection_item": {"id": "SEL-001", "related_requirements": related},
                "candidates": [
                    {"model": model, "device_id": device_id}
                    for model, device_id in models
                ],
            }
        ]
    }


def test_linked_requirement_gets_device_value() -> None:
    proposals = propose(
        raw_spec=_raw("开关频率"),
        result=_result(["REQ-001"], [("A", 1)]),
        decisions={},
        attributes_for=lambda _: {
            "clock_frequency": Attr("clock_frequency", "开关频率", "500 kHz")
        },
    )

    assert len(proposals) == 1
    assert proposals[0].proposed_value == "500 kHz"
    assert proposals[0].model == "A"


def test_missing_parameter_is_not_proposed() -> None:
    proposals = propose(
        raw_spec=_raw("工作电压"),
        result=_result(["REQ-001"], [("A", 1)]),
        decisions={},
        attributes_for=lambda _: {
            "operating_voltage": Attr(
                "operating_voltage", "工作电压", "", missing=True
            )
        },
    )

    assert proposals == []


def test_confirmed_candidate_wins_over_first_candidate() -> None:
    proposals = propose(
        raw_spec=_raw("工作电压"),
        result=_result(["REQ-001"], [("A", 1), ("B", 2)]),
        decisions={"SEL-001::B": {"decision": "confirmed"}},
        attributes_for=lambda device_id: {
            "operating_voltage": Attr(
                "operating_voltage", "工作电压", "3.3 V" if device_id == 2 else "5 V"
            )
        },
    )

    assert len(proposals) == 1
    assert proposals[0].model == "B"
    assert proposals[0].proposed_value == "3.3 V"


def test_unlinked_requirement_only_uses_unique_value() -> None:
    unique = propose(
        raw_spec=_raw("工作温度"),
        result={
            "items": [
                {
                    "selection_item": {"id": "SEL-001", "related_requirements": []},
                    "candidates": [{"model": "A", "device_id": 1}],
                },
                {
                    "selection_item": {"id": "SEL-002", "related_requirements": []},
                    "candidates": [{"model": "B", "device_id": 2}],
                },
            ]
        },
        decisions={},
        attributes_for=lambda _: {
            "operating_temperature": Attr(
                "operating_temperature", "工作温度", "-40℃~+85℃"
            )
        },
    )
    assert len(unique) == 1
    assert unique[0].proposed_value == "-40℃~+85℃"

    ambiguous = propose(
        raw_spec=_raw("工作温度"),
        result={
            "items": [
                {
                    "selection_item": {"id": "SEL-001", "related_requirements": []},
                    "candidates": [{"model": "A", "device_id": 1}],
                },
                {
                    "selection_item": {"id": "SEL-002", "related_requirements": []},
                    "candidates": [{"model": "B", "device_id": 2}],
                },
            ]
        },
        decisions={},
        attributes_for=lambda device_id: {
            "operating_temperature": Attr(
                "operating_temperature",
                "工作温度",
                "-40℃~+85℃" if device_id == 1 else "-20℃~+70℃",
            )
        },
    )
    assert ambiguous == []


def test_stated_requirement_is_left_alone_and_pending_lists_only_tbd() -> None:
    raw = _raw("工作电压", state="stated", value="3.3 V")
    raw["requirements"].append(
        {
            "id": "REQ-002",
            "name": "外设接口数量",
            "value": "待确认",
            "state": "tbd",
        }
    )
    proposals = propose(
        raw_spec=raw,
        result=_result(["REQ-001"], [("A", 1)]),
        decisions={},
        attributes_for=lambda _: {
            "operating_voltage": Attr("operating_voltage", "工作电压", "5 V")
        },
    )

    assert proposals == []
    assert pending(raw) == [
        {
            "id": "REQ-002",
            "name": "外设接口数量",
            "current_value": "待确认",
        }
    ]


def test_conflicting_values_across_linked_items_are_not_proposed() -> None:
    result = {
        "items": [
            {
                "selection_item": {"id": "SEL-001", "related_requirements": ["REQ-001"]},
                "candidates": [{"model": "A", "device_id": 1}],
            },
            {
                "selection_item": {"id": "SEL-002", "related_requirements": ["REQ-001"]},
                "candidates": [{"model": "B", "device_id": 2}],
            },
        ]
    }
    proposals = propose(
        raw_spec=_raw("工作电压"),
        result=result,
        decisions={},
        attributes_for=lambda device_id: {
            "operating_voltage": Attr(
                "operating_voltage", "工作电压", "3.3 V" if device_id == 1 else "5 V"
            )
        },
    )

    assert proposals == []
