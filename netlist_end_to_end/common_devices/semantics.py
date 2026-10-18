"""Parameter relationships and explicit limits of common-device validation.

The registry validates the template and parameter JSON Schemas first.  This
module does not inspect pin bindings, resolve reference documents, query a
catalog, or simulate an operating circuit.  Its diagnostics therefore remain
separate from logical connectivity readiness.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import CommonDeviceError


_SUPPORTED_TEMPLATES = frozenset(
    {
        "common.passive.resistor",
        "common.passive.capacitor",
        "common.passive.inductor",
        "common.passive.resistor_array",
        "common.passive.potentiometer",
        "common.passive.thermistor",
        "common.passive.photoresistor",
        "common.passive.variable_capacitor",
        "common.passive.ferrite_bead",
        "common.passive.transformer",
        "common.passive.common_mode_choke",
        "common.clock.passive_crystal",
        "common.passive.fuse",
        "common.passive.varistor",
        "common.passive.gas_discharge_tube",
    }
)

# These parameter predicates are already enforced by the current definitions'
# JSON Schemas.  They are not reimplemented with a second set of comparisons.
_SCHEMA_RULES: dict[str, frozenset[str]] = {
    "common.passive.capacitor": frozenset({"polarization_explicit"}),
    "common.passive.thermistor": frozenset({"condition_required"}),
    "common.passive.photoresistor": frozenset({"reference_conditions"}),
    "common.passive.ferrite_bead": frozenset({"impedance_pair"}),
    "common.passive.varistor": frozenset({"paired_test_condition"}),
    "common.passive.gas_discharge_tube": frozenset({"sparkover_condition"}),
}

# No operating-point, waveform, curve contents, or physical source evidence is
# supplied to this API.  Even a non-null rating/reference cannot prove these
# capabilities.  Messages name the missing validation scope instead of treating
# the device's explicit parameters as invalid.
_CAPABILITY_LIMITS: dict[str, dict[str, str]] = {
    "common.passive.resistor": {
        "power_check_not_covered": (
            "未提供完整工作条件；额定功率即使有值，也不能据此判定功耗校核通过。"
        ),
    },
    "common.passive.capacitor": {
        "voltage_polarity_check": (
            "未提供两端工作电压及适用条件；极性参数和额定电压不能证明"
            "实际电压方向或耐压校核通过。"
        ),
    },
    "common.passive.inductor": {
        "current_check_scope": (
            "未提供电流波形、额定定义及工作条件；电流、饱和和温升校核保持未决。"
        ),
    },
    "common.passive.resistor_array": {
        "physical_pairing_evidence": (
            "本参数检查器未接收实际封装单元配对及其来源，不能判定物理配对已确认。"
        ),
    },
    "common.passive.potentiometer": {
        "ratio_is_not_voltage": (
            "wiper_ratio 只声明理想电阻分配；未提供供电、负载和接触条件，"
            "不能判定滑动端电压。"
        ),
    },
    "common.passive.ferrite_bead": {
        "rating_evidence": (
            "阻抗仅对应 impedance_frequency_hz 指定的参考点；未提供频率曲线、"
            "电流和直流工作条件，不能判定任意频率阻抗、滤波性能或直流压降。"
        ),
    },
    "common.passive.transformer": {
        "no_performance_guarantee": (
            "匝比及同名端声明不证明实际输出电压、隔离等级、功率、工作频段或饱和裕量；"
            "未提供相应工作条件和证据。"
        ),
    },
    "common.passive.common_mode_choke": {
        "no_filter_guarantee": (
            "单个频率的共模阻抗和四端连接不证明共模衰减、差模行为或电流适用性；"
            "缺少完整测试及工作条件。"
        ),
    },
    "common.clock.passive_crystal": {
        "oscillation_not_proven": (
            "未提供完整振荡电路及条件，不能判定起振、驱动功率、稳定度或负载匹配；"
            "load_capacitance_f 不直接确定两个外接电容值。"
        ),
    },
    "common.passive.fuse": {
        "rating_kind": (
            "voltage_kind 仅说明额定类别；未提供实际工作电压及其 AC/DC 类别，"
            "不能判定额定电压适用。"
        ),
        "protection_evidence": (
            "曲线引用或分断能力有值也不证明保护有效；未加载曲线及故障条件，"
            "短路保护和选择配合保持未决。"
        ),
    },
    "common.passive.varistor": {
        "surge_unresolved": (
            "未加载钳位曲线并核对浪涌波形、能量、电流及工作条件，"
            "不能判定浪涌保护有效。"
        ),
    },
    "common.passive.gas_discharge_tube": {
        "impulse_unknown": (
            "直流击穿参考点或资料引用不能证明冲击钳位、续流及恢复行为；"
            "未提供完整冲击、续流和配合条件。"
        ),
    },
}


def validate_semantics(
    template: Mapping[str, Any], parameters: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Reject inconsistent parameters and report unevaluated capabilities.

    Call only after JSON Schema validation.  Missing optional parameters and
    explicit null both mean unknown; valid zero values are never treated as
    missing.  Hard inconsistencies raise ``CommonDeviceError``.  Returned
    ``unresolved`` and ``not_evaluated`` diagnostics are not successful physical
    checks and need not prevent an otherwise complete logical netlist.
    """

    template_id = str(template["template_id"])
    diagnostics: list[dict[str, Any]] = []
    evaluated_rules: set[str] = set(_SCHEMA_RULES.get(template_id, ()))

    def add(rule_id: str, message: str, *, status: str = "unresolved") -> None:
        diagnostics.append(
            {
                "code": f"{template_id}:{rule_id}",
                "status": status,
                "message": message,
            }
        )

    if template_id == "common.passive.variable_capacitor":
        lower = parameters["capacitance_min_f"]
        upper = parameters["capacitance_max_f"]
        setting = parameters.get("capacitance_set_f")
        if lower > upper:
            raise CommonDeviceError(
                f"{template_id}: capacitance_min_f must be <= capacitance_max_f"
            )
        if setting is not None and not lower <= setting <= upper:
            raise CommonDeviceError(
                f"{template_id}: capacitance_set_f must be within "
                "[capacitance_min_f, capacitance_max_f]"
            )
        evaluated_rules.update(
            {"range_order", "setpoint_range", "fixed_operating_value"}
        )
        if setting is None:
            add(
                "fixed_operating_value",
                "capacitance_set_f 尚未给定；允许保留范围连接，确定工作电容量的验证未决。",
            )

    elif template_id == "common.passive.common_mode_choke":
        impedance = parameters.get("common_mode_impedance_ohm")
        frequency = parameters.get("impedance_frequency_hz")
        if (impedance is None) != (frequency is None):
            raise CommonDeviceError(
                f"{template_id}: common_mode_impedance_ohm and "
                "impedance_frequency_hz must both have values or both be unknown"
            )
        evaluated_rules.add("impedance_pair")

    elif template_id == "common.clock.passive_crystal":
        mode = parameters["resonance_mode"]
        load = parameters.get("load_capacitance_f")
        if mode == "load_specified" and load is None:
            raise CommonDeviceError(
                f"{template_id}: resonance_mode=load_specified requires "
                "a non-null load_capacitance_f"
            )
        if mode == "series" and load is not None:
            raise CommonDeviceError(
                f"{template_id}: resonance_mode=series does not accept "
                "a non-null load_capacitance_f in this template"
            )
        evaluated_rules.update({"load_value_required", "resonance_condition"})
        if mode == "unknown":
            add(
                "resonance_condition",
                "resonance_mode 为 unknown，频率条件和振荡接口适配未决；"
                "即使提供负载值也不自动推断谐振模式。",
            )

    if template_id in {
        "common.passive.transformer",
        "common.passive.common_mode_choke",
    }:
        evaluated_rules.add("relative_polarity_known")
        if parameters["relative_polarity"] == "unknown":
            add(
                "relative_polarity_known",
                "relative_polarity 为 unknown，依赖同名端关系或绕向的相位检查未决。",
            )

    if template_id == "common.passive.potentiometer":
        evaluated_rules.add("unknown_wiper_position")
        if parameters["wiper_ratio"] is None:
            add(
                "unknown_wiper_position",
                "wiper_ratio 为 null，位置相关电阻分配未决；不填充默认中点。",
            )

    curve_rules = {
        "common.passive.thermistor": (
            "rt_curve_ref",
            "rt_missing",
            "其他温度下的阻值或过温保护",
        ),
        "common.passive.photoresistor": (
            "response_curve_ref",
            "curve_missing",
            "其他光照及测量条件下的阻值",
        ),
    }
    if template_id in curve_rules:
        parameter, rule_id, capability = curve_rules[template_id]
        evaluated_rules.add(rule_id)
        reason = (
            f"{parameter} 为 null，未提供响应模型"
            if parameters[parameter] is None
            else f"{parameter} 仅为资料引用，本检查器未加载或验证响应模型"
        )
        add(rule_id, f"{reason}；不能判定{capability}。")

    for rule_id, message in _CAPABILITY_LIMITS.get(template_id, {}).items():
        evaluated_rules.add(rule_id)
        add(rule_id, message)

    if template_id in _SUPPORTED_TEMPLATES:
        add(
            "physical_validation_unresolved",
            "本接口未接收实际引脚来源及完整工作条件，只检查参数关系；"
            "不能据此宣称物理实现、耐压、功耗或保护能力已验证。",
        )
    else:
        add(
            "template_semantics_not_implemented",
            "尚未实现此 template_id 的参数语义；JSON Schema 或逻辑连接通过"
            "不代表本模板的工程规则已经验证。",
            status="not_evaluated",
        )

    for rule in template.get("rules", ()):
        rule_id = rule["id"]
        if rule_id not in evaluated_rules:
            add(
                rule_id,
                "本参数检查器未评估该规则；拓扑、物理证据或其他工程检查需由"
                f"对应校验器完成。规则说明：{rule['description']}",
                status="not_evaluated",
            )

    return diagnostics


__all__ = ["validate_semantics"]
