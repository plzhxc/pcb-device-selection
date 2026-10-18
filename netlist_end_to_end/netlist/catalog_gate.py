"""Deterministic SQL identity and pin gate for generated netlists."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..tools.netlist_evidence.common import abstract_device_type, core_device_type


class CatalogGateViolation(ValueError):
    """A generated core-device identity cannot be proved by SQL evidence."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


class CatalogBindingGate:
    """Canonicalize plans and outputs against prefetched catalog records."""

    @classmethod
    def bind_plan(
        cls,
        plan: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        canonical = dict(plan)
        candidates = cls._catalog_devices(evidence)
        raw_main = plan.get("main_chip")
        main = dict(raw_main) if isinstance(raw_main, Mapping) else {}
        abstract_kind = cls._abstract_type(main)
        if not candidates:
            if main and cls._requires_binding(main):
                raise CatalogGateViolation(
                    "catalog_identity_mismatch",
                    "planner emitted a chip without an exact SQL catalog record",
                )
            return canonical, None
        if not main or abstract_kind:
            raise CatalogGateViolation(
                "catalog_identity_mismatch",
                "planner omitted main_chip although SQL resolved a selected device",
            )

        candidate = cls._match_device(
            main,
            candidates,
            path="plan.main_chip",
            allow_single_fallback=True,
        )
        required_c = cls._required_supplier_part(evidence)
        if required_c and candidate["supplier_part"].upper() != required_c:
            raise CatalogGateViolation(
                "catalog_identity_mismatch",
                "plan.main_chip does not use module.confirmed_device " f"{required_c}",
            )
        main["device_name"] = candidate["device_name"]
        main["supplier_part"] = candidate["supplier_part"]
        main["device_type"] = "chip"
        main["pins"] = cls._canonicalize_pins(
            main.get("pins"),
            candidate,
            path="plan.main_chip.pins",
            require_nonempty=True,
        )
        canonical["main_chip"] = main
        binding = {
            "device_name": candidate["device_name"],
            "supplier_part": candidate["supplier_part"],
            "designator": str(main.get("designator") or "U1").strip() or "U1",
            "pin_count": candidate.get("pin_count"),
        }
        return canonical, binding

    @classmethod
    def validate_output(
        cls,
        netlist_info: Mapping[str, Any],
        evidence: Mapping[str, Any],
        *,
        expected_binding: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        canonical = dict(netlist_info)
        raw_netlist = netlist_info.get("netlist")
        if not isinstance(raw_netlist, Mapping):
            raise CatalogGateViolation(
                "catalog_identity_mismatch",
                "generated netlist has no device mapping to validate",
            )
        candidates = cls._catalog_devices(evidence)
        expected = dict(expected_binding or {})
        expected_c = str(expected.get("supplier_part") or "").strip().upper()
        if not expected_c:
            expected_c = cls._required_supplier_part(evidence)
        expected_designator = str(expected.get("designator") or "").strip()
        rebuilt: dict[str, Any] = {}
        bindings: list[dict[str, Any]] = []

        for raw_key, raw_device in raw_netlist.items():
            if not isinstance(raw_device, Mapping):
                rebuilt[str(raw_key)] = raw_device
                continue
            device = dict(raw_device)
            raw_props = raw_device.get("props")
            props = dict(raw_props) if isinstance(raw_props, Mapping) else {}
            abstract_kind = cls._abstract_type(props)
            supplied_c = str(props.get("supplierPart") or "").strip().upper()
            designator = str(props.get("designator") or "").strip()
            candidate: dict[str, Any] | None = None

            if supplied_c:
                matches = [
                    row
                    for row in candidates
                    if row["supplier_part"].upper() == supplied_c
                ]
                if len(matches) != 1:
                    raise CatalogGateViolation(
                        "catalog_identity_mismatch",
                        f"{designator or raw_key} uses unproved "
                        f"supplierPart {supplied_c!r}",
                    )
                candidate = matches[0]
            else:
                device_name = str(props.get("deviceName") or "").strip()
                name_matches = [
                    row
                    for row in candidates
                    if device_name
                    and row["device_name"].casefold() == device_name.casefold()
                ]
                if len(name_matches) == 1:
                    candidate = name_matches[0]
                elif (
                    expected_c
                    and expected_designator
                    and designator == expected_designator
                ):
                    candidate = next(
                        (
                            row
                            for row in candidates
                            if row["supplier_part"].upper() == expected_c
                        ),
                        None,
                    )

            if candidate is not None:
                if abstract_kind:
                    raise CatalogGateViolation(
                        "catalog_identity_mismatch",
                        f"{designator or raw_key} labels a SQL-resolved core IC as abstract",
                    )
                props["deviceName"] = candidate["device_name"]
                props["supplierPart"] = candidate["supplier_part"]
                props["deviceType"] = "chip"
                props["is_abstract"] = False
                device["pins"] = cls._canonicalize_pins(
                    raw_device.get("pins"),
                    candidate,
                    path=f"netlist.{raw_key}.pins",
                    require_nonempty=True,
                )
                bindings.append(
                    {
                        "designator": designator or str(raw_key),
                        "device_name": candidate["device_name"],
                        "supplier_part": candidate["supplier_part"],
                        "connected_pin_count": len(device["pins"]),
                    }
                )
            elif cls._requires_binding(props):
                raise CatalogGateViolation(
                    "catalog_identity_mismatch",
                    f"{designator or raw_key} is a chip without exact SQL "
                    "identity evidence",
                )
            else:
                device["pins"] = cls._common_pins(raw_device.get("pins"), path=f"netlist.{raw_key}.pins")

            device["props"] = props
            if "pins" not in device:
                device["pins"] = raw_device.get("pins", {})
            rebuilt[str(raw_key)] = device

        if expected_c and not any(
            row["supplier_part"].upper() == expected_c for row in bindings
        ):
            raise CatalogGateViolation(
                "catalog_identity_mismatch",
                "generated netlist does not contain planned catalog device "
                f"{expected_c}",
            )
        if candidates and not bindings:
            raise CatalogGateViolation(
                "catalog_identity_mismatch",
                "generated netlist contains none of the SQL-resolved selected devices",
            )
        canonical["netlist"] = rebuilt
        return canonical, bindings

    @staticmethod
    def _catalog_devices(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
        catalog = evidence.get("catalog")
        catalog_row = catalog if isinstance(catalog, Mapping) else {}
        raw_candidates = catalog_row.get("resolved_candidates")
        rows = (
            raw_candidates
            if isinstance(raw_candidates, Sequence)
            and not isinstance(raw_candidates, (str, bytes))
            else []
        )
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            nested = raw.get("device")
            device = nested if isinstance(nested, Mapping) else raw
            if CatalogBindingGate._is_catalog_connector(device):
                continue
            device_name = str(device.get("device_name") or "").strip()
            supplier_part = str(device.get("supplier_part") or "").strip().upper()
            pins = device.get("pins")
            if (
                not device_name
                or not supplier_part
                or supplier_part in seen
                or not isinstance(pins, Sequence)
                or isinstance(pins, (str, bytes))
            ):
                continue
            seen.add(supplier_part)
            result.append(
                {
                    **dict(device),
                    "device_name": device_name,
                    "supplier_part": supplier_part,
                    "pins": [dict(pin) for pin in pins if isinstance(pin, Mapping)],
                }
            )
        return result

    @staticmethod
    def _required_supplier_part(evidence: Mapping[str, Any]) -> str:
        catalog = evidence.get("catalog")
        catalog_row = catalog if isinstance(catalog, Mapping) else {}
        required = str(catalog_row.get("required_supplier_part") or "").strip().upper()
        for row in catalog_row.get("resolved_candidates") or []:
            if not isinstance(row, Mapping):
                continue
            device = row.get("device", row)
            if (isinstance(device, Mapping) and CatalogBindingGate._is_catalog_connector(device)
                    and str(device.get("supplier_part") or "").strip().upper() == required):
                return ""
        return required

    @staticmethod
    def _is_catalog_connector(device: Mapping[str, Any]) -> bool:
        # Legacy selection artifacts may include a purchased connector. Its
        # catalog presence does not make it an IC or require a main_chip.
        return str(device.get("category_level_1") or "").strip().lower() in {"connector", "connectors", "连接器"}

    @staticmethod
    def _abstract_type(device: Mapping[str, Any]) -> str | None:
        try:
            return abstract_device_type(device)
        except ValueError as exc:
            raise CatalogGateViolation("catalog_identity_mismatch", str(exc)) from exc

    @staticmethod
    def _common_pins(raw_pins: Any, *, path: str) -> dict[str, str]:
        if not isinstance(raw_pins, Mapping) or not raw_pins:
            raise CatalogGateViolation("common_pin_invalid", f"{path} must be a nonempty pin-to-net mapping")
        result: dict[str, str] = {}
        for number, net in raw_pins.items():
            if (not isinstance(number, str) or not number.strip() or number.strip() in result
                    or not isinstance(net, str) or not net.strip()):
                raise CatalogGateViolation("common_pin_invalid", f"{path} contains an invalid or duplicate pin/net")
            result[number.strip()] = net
        return result

    @staticmethod
    def _match_device(
        identity: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        *,
        path: str,
        allow_single_fallback: bool,
    ) -> dict[str, Any]:
        supplied_c = (
            str(identity.get("supplier_part") or identity.get("supplierPart") or "")
            .strip()
            .upper()
        )
        device_name = str(
            identity.get("device_name") or identity.get("deviceName") or ""
        ).strip()
        if supplied_c:
            matches = [
                row
                for row in candidates
                if str(row.get("supplier_part") or "").upper() == supplied_c
            ]
        elif device_name:
            matches = [
                row
                for row in candidates
                if str(row.get("device_name") or "").casefold()
                == device_name.casefold()
            ]
        elif allow_single_fallback and len(candidates) == 1:
            matches = list(candidates)
        else:
            matches = []
        if len(matches) != 1:
            requested = supplied_c or device_name or "<blank>"
            raise CatalogGateViolation(
                "catalog_identity_mismatch",
                f"{path} identity {requested!r} does not select one exact SQL candidate",
            )
        return dict(matches[0])

    @staticmethod
    def _canonicalize_pins(
        raw_pins: Any,
        candidate: Mapping[str, Any],
        *,
        path: str,
        require_nonempty: bool,
    ) -> dict[str, str]:
        if not isinstance(raw_pins, Mapping):
            raise CatalogGateViolation(
                "catalog_pin_not_found",
                f"{path} must be a pin-to-net mapping",
            )
        allowed: dict[str, str] = {}
        folded: dict[str, list[str]] = {}
        for raw in candidate.get("pins") or []:
            if not isinstance(raw, Mapping):
                continue
            number = str(raw.get("pin_number") or "").strip()
            if not number:
                continue
            allowed[number] = number
            folded.setdefault(number.casefold(), []).append(number)
        normalized: dict[str, str] = {}
        for raw_pin, raw_net in raw_pins.items():
            pin = str(raw_pin).strip()
            exact = allowed.get(pin)
            if exact is None:
                alternatives = folded.get(pin.casefold(), [])
                exact = alternatives[0] if len(alternatives) == 1 else None
            if exact is None:
                raise CatalogGateViolation(
                    "catalog_pin_not_found",
                    f"{path} contains pin {pin!r}, absent from SQL device "
                    f"{candidate.get('supplier_part')}",
                )
            if exact in normalized:
                raise CatalogGateViolation(
                    "catalog_pin_not_found",
                    f"{path} maps SQL pin {exact!r} more than once",
                )
            normalized[exact] = str(raw_net)
        if require_nonempty and not normalized:
            raise CatalogGateViolation(
                "catalog_pin_not_found",
                f"{path} does not connect any SQL-verified pin",
            )
        return normalized

    @staticmethod
    def _requires_binding(device: Mapping[str, Any]) -> bool:
        kinds = {str(device.get(key) or "").strip().lower().replace("-", "_").replace(" ", "_")
                 for key in ("type", "device_type", "deviceType")}
        return any(core_device_type(kind) for kind in kinds)


__all__ = ["CatalogBindingGate", "CatalogGateViolation"]
