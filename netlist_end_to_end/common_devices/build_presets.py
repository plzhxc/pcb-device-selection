"""Build ``assets/preset_components.json`` from the reference cases and the device catalog.

The preset library lists parts that need no LLM selection: passives (R / C / L /
ferrite beads), small discretes (LEDs, diodes, TVS, MOSFETs, BJTs), and whole
catalog categories (connectors, switches, crystals, LEDs, fuses, buzzers).  Two sources feed it, because the device catalog
(``device_parameters``) does not hold plain resistors, capacitors or inductors:

1. **Reference cases** (``02_CASE/02_dataset/*/netlist/*.enet``): every component
   with a valid LCSC C-number.  Its LCSC category comes from
   ``refer_netlist.device_table`` (``props.deviceType`` of the peripheral rows,
   filled by the ingest pipeline from the LCSC catalog).  Package, value,
   tolerance, rated voltage and JLCPCB part class come from the ``.enet``
   component properties (``Supplier Footprint``, ``LCSC Part Name``,
   ``JLCPCB Part Class``).
2. **Device catalog** (``device_parameters.devices`` + ``params_<category>``):
   whole categories whose members differ only in a few mechanical parameters
   (connectors, switches, crystals, LEDs, fuses, buzzers).  Each row is copied
   with its distinguishing parameters, price and pin count; parts that also
   appear in the reference cases carry their usage counts.

Selection rules applied here (documented again in the asset header):

* a case part enters only if its C-number is well-formed, it is not marked
  discontinued (``停产``), and — for R / C / L — the cases used it with a single
  value (parts that appear with many different values are schematic
  placeholders, e.g. C304773 used with 35 values);
* R / C / L / bead entries need a parseable value; entries without a package
  are dropped;
* every catalog row of a preset category is kept; within each *variant*
  (kind-specific parameter tuple, e.g. USB ``Type-C / Female / 16P / SMD``)
  rows are ranked by case usage (number of cases, then instances) and then by
  ascending price, and the first row is marked ``default``.

Run from ``01_PCBAgent``::

    PCBAGENT_MYSQL_PASSWORD=... python -m netlist_end_to_end.common_devices.build_presets

The script only reads; it writes the asset and prints a summary.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import mysql.connector

ROOT = Path(__file__).resolve().parents[3]
CASE_GLOB = str(ROOT / "02_CASE" / "02_dataset" / "*" / "netlist" / "*.enet")
ASSET = Path(__file__).with_name("assets") / "preset_components.json"

MYSQL = {
    "host": os.environ.get("PCBAGENT_MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("PCBAGENT_MYSQL_PORT", "3307")),
    "user": os.environ.get("PCBAGENT_MYSQL_USER", "root"),
    "password": os.environ.get("PCBAGENT_MYSQL_PASSWORD", ""),
    "charset": "utf8mb4",
}

C_NUMBER = re.compile(r"^C\d+$")

# --------------------------------------------------------------------------- #
# Kind definitions
# --------------------------------------------------------------------------- #
# Case-derived kinds: LCSC category prefix -> kind.  Order matters (first match).
CASE_KINDS: list[tuple[str, str]] = [
    ("Resistors-Chip Resistor", "resistor"),
    ("Resistors-Current Sense", "resistor"),
    ("Resistors-Through Hole", "resistor"),
    ("Capacitors-Multilayer Ceramic", "capacitor"),
    ("Capacitors-Through Hole Ceramic", "capacitor"),
    ("Capacitors-Tantalum", "capacitor_polar"),
    ("Capacitors-Aluminum Electrolytic", "capacitor_polar"),
    ("Capacitors-Polymer Aluminum", "capacitor_polar"),
    ("Filters-Ferrite Beads", "ferrite_bead"),
    ("Inductors, Coils, Chokes", "inductor"),
    ("Optoelectronics-LED Indication", "led"),
    ("Optoelectronics-RGB LEDs(Built-in IC)", "rgb_led_builtin_ic"),
    ("Diodes-Schottky", "diode_schottky"),
    ("Diodes-Switching", "diode_switching"),
    ("Diodes-Zener", "diode_zener"),
    ("Diodes-General Purpose", "diode_rectifier"),
    ("Transistors/Thyristors-ESD and Surge Protection", "tvs_esd"),
    ("Transistors/Thyristors-MOSFETs", "mosfet"),
    ("Transistors/Thyristors-Bipolar", "bjt"),
    ("Circuit Protection-Resettable Fuses", "fuse_resettable"),
]

# Designator prefix + name pattern fallback when refer_netlist has no LCSC category.
PREFIX_KINDS = {
    "R": ("resistor", re.compile(r"\d\s*[kKmM]?Ω")),
    "C": ("capacitor", re.compile(r"\d\s*(p|n|u|µ|μ|m)F\b")),
    "L": ("inductor", re.compile(r"\d\s*(n|u|µ|μ|m)?H\b")),
    "FB": ("ferrite_bead", re.compile(r"磁珠|Ω@")),
    "LED": ("led", re.compile(r"灯|LED", re.I)),
}

KIND_META: dict[str, dict[str, Any]] = {
    "resistor": {"device_type": "resistance", "designator_prefix": "R", "match": ["value", "package"], "default_package": "0603", "pins": 2},
    "capacitor": {"device_type": "capacitance", "designator_prefix": "C", "match": ["value", "package"], "default_package": "0603", "pins": 2},
    "capacitor_polar": {"device_type": "capacitance", "designator_prefix": "C", "match": ["value", "rated_voltage"], "default_package": None, "pins": 2},
    "inductor": {"device_type": "inductance", "designator_prefix": "L", "match": ["value"], "default_package": None, "pins": 2},
    "ferrite_bead": {"device_type": "inductance", "designator_prefix": "FB", "match": ["value", "package"], "default_package": "0603", "pins": 2},
    "led": {"device_type": "diode", "designator_prefix": "LED", "match": ["color", "package"], "default_package": "0603", "pins": 2},
    "rgb_led_builtin_ic": {"device_type": "diode", "designator_prefix": "LED", "match": ["mpn"], "default_package": None, "pins": 4},
    "diode_schottky": {"device_type": "diode", "designator_prefix": "D", "match": ["mpn"], "default_package": None, "pins": 2},
    "diode_switching": {"device_type": "diode", "designator_prefix": "D", "match": ["mpn"], "default_package": None, "pins": 2},
    "diode_zener": {"device_type": "diode", "designator_prefix": "ZD", "match": ["mpn"], "default_package": None, "pins": 2},
    "diode_rectifier": {"device_type": "diode", "designator_prefix": "D", "match": ["mpn"], "default_package": None, "pins": 2},
    "tvs_esd": {"device_type": "esd_protection", "designator_prefix": "D", "match": ["mpn"], "default_package": None, "pins": None},
    "mosfet": {"device_type": "mos", "designator_prefix": "Q", "match": ["mpn"], "default_package": None, "pins": 3},
    "bjt": {"device_type": "triode", "designator_prefix": "Q", "match": ["mpn"], "default_package": None, "pins": 3},
    "fuse_resettable": {"device_type": "fuse", "designator_prefix": "F", "match": ["mpn"], "default_package": None, "pins": 2},
}

# Catalog-derived kinds: category_level_2 -> parameters that define a variant.
CATALOG_KINDS: dict[str, dict[str, Any]] = {
    "usb_connector": {"device_type": "connector", "designator_prefix": "USB", "variant": ["connector_type", "male_and_female", "number_of_contacts", "installation_method"]},
    "pin_header": {"device_type": "connector", "designator_prefix": "J", "variant": ["spacing_noun", "number_of_rows", "total_pin_digits", "installation_method"]},
    "female_connector": {"device_type": "connector", "designator_prefix": "J", "variant": ["spacing_noun", "number_of_rows", "total_number_of_holes", "installation_method"]},
    "wire_to_board_pin_header": {"device_type": "connector", "designator_prefix": "J", "variant": ["reference_series", "spacing_noun", "number_of_rows", "total_pin_count", "installation_method"]},
    "dc_power_connector": {"device_type": "connector", "designator_prefix": "J", "variant": ["connector_type", "contact_inner_diameter", "contact_outer_diameter", "installation_method"]},
    "ffc_fpc_connector": {"device_type": "connector", "designator_prefix": "J", "variant": ["spacing_noun", "number_of_contacts", "contact_type", "locking_characteristic"]},
    "sd_card_memory_card_connector": {"device_type": "connector", "designator_prefix": "J", "variant": ["card_type", "card_connection_method", "additional_card_insertion_detection_pin"]},
    "sim_card_connector": {"device_type": "connector", "designator_prefix": "J", "variant": ["card_type", "card_connection_method", "card_insertion_detection"]},
    "rf_coaxial_connector": {"device_type": "connector", "designator_prefix": "J", "variant": ["radio_frequency_series", "interface_type", "style_pattern"]},
    "audio_connector_headphones": {"device_type": "connector", "designator_prefix": "J", "variant": ["connector_type"]},
    "hdmi_connector": {"device_type": "connector", "designator_prefix": "J", "variant": ["connector_type", "male_and_female", "installation_method"]},
    "ethernet_connector_rj45_rj11": {"device_type": "connector", "designator_prefix": "J", "variant": ["connector_type", "number_of_ports", "led", "poe", "shielding"]},
    "button_and_strip_battery_connector": {"device_type": "connector", "designator_prefix": "BT", "variant": ["applicable_battery_specifications", "connector_type"]},
    "screw_terminal_block": {"device_type": "connector", "designator_prefix": "J", "variant": ["spacing_noun", "number_of_rows", "total_pin_count", "installation_method"]},
    "plug_in_terminal_block": {"device_type": "connector", "designator_prefix": "J", "variant": ["connector_type", "spacing_noun", "number_of_rows", "total_pin_count"]},
    "spring_loaded_terminal_block": {"device_type": "connector", "designator_prefix": "J", "variant": ["spacing_noun", "number_of_rows", "total_pin_count", "installation_method"]},
    "idc_pierceable_connector_bull_horn_simplified_bull": {"device_type": "connector", "designator_prefix": "J", "variant": ["connector_type", "spacing_noun", "number_of_rows", "number_of_pins_per_row", "installation_method"]},
    "board_to_board_and_backplane_connectors": {"device_type": "connector", "designator_prefix": "J", "variant": ["connection_method", "spacing_noun", "number_of_rows", "total_number_of_pins"]},
    "pogo_pin_spring_probe_connector": {"device_type": "connector", "designator_prefix": "J", "variant": ["type", "total_pin_count"]},
    "test_point_test_loop": {"device_type": "connector", "designator_prefix": "TP", "variant": ["connector_type"]},
    "tactile_switch": {"device_type": "switch", "designator_prefix": "SW", "variant": ["switch_length", "switch_width", "switch_height", "installation_method", "pin_style"]},
    "slide_switch": {"device_type": "switch", "designator_prefix": "SW", "variant": ["circuit_structure", "installation_method", "pin_style"]},
    "dip_switch": {"device_type": "switch", "designator_prefix": "SW", "variant": ["switch_bit_count", "pin_style", "foot_spacing"]},
    "button_switch": {"device_type": "switch", "designator_prefix": "SW", "variant": ["circuit_structure", "self_locking_unlocking", "installation_method"]},
    "toggle_switch": {"device_type": "switch", "designator_prefix": "SW", "variant": ["circuit_structure", "installation_method", "pin_style"]},
    "rotary_encoder": {"device_type": "switch", "designator_prefix": "SW", "variant": ["number_of_nodes_number_of_pulses_incremental", "is_it_possible_to_press_down"]},
    "passive_crystal_oscillator": {"device_type": "oscillator", "designator_prefix": "Y", "variant": ["frequency", "encapsulation_specification", "load_capacitance"]},
    "active_crystal_oscillator": {"device_type": "oscillator", "designator_prefix": "Y", "variant": ["frequency", "encapsulation_specification", "output_mode", "operating_voltage"]},
    "light_emitting_diode_led": {"device_type": "diode", "designator_prefix": "LED", "variant": ["luminous_color", "encapsulation_specification"]},
    "rgb_led": {"device_type": "diode", "designator_prefix": "LED", "variant": ["diode_configuration", "encapsulation_specification"]},
    "buzzer": {"device_type": "other", "designator_prefix": "BZ", "variant": ["construction_type", "driving_mode", "rated_voltage", "installation_method"]},
    "self_resetting_fuse": {"device_type": "fuse", "designator_prefix": "F", "variant": ["hold_current_ihold", "encapsulation_specification"]},
    "disposable_fuse": {"device_type": "fuse", "designator_prefix": "F", "variant": ["rated_current", "rated_voltage", "encapsulation_specification"]},
}
# Electrical columns kept on every catalog row besides the variant fields.
CATALOG_KEEP_COLUMNS = {
    "rated_current", "rated_voltage", "rated_current_power_supply", "protocol_standard", "frequency", "load_capacitance",
    "frequency_stability", "encapsulation_specification", "operating_voltage", "forward_current", "forward_voltage_drop_vf",
    "hold_current_ihold", "trip_current_itrip", "impedance", "circuit_structure", "contact_current", "force_of_action",
}
CATALOG_SKIP_COLUMNS = {
    "device_id", "brand", "brand_origin", "product_tag", "certification_information", "packaging_method", "jialichuang_smt",
    "functional_description", "operating_temperature", "storage_temperature", "plugging_and_unplugging_lifespan", "rubber_core_color",
    "color", "button_cap_color", "light_strip", "working_life", "protection_level", "support_bracket", "flame_retardant_grade",
    "plastic_material", "contact_material", "contact_plating", "round_needle_policy", "additional_features", "carrier_tape_method",
    "lens_color", "shell_color",
}


# --------------------------------------------------------------------------- #
# Value parsing lives in the runtime module so the library and its lookups agree.
# --------------------------------------------------------------------------- #
from .presets import (
    parse_bead,
    parse_capacitance,
    parse_dielectric,
    parse_inductance,
    parse_led_color,
    parse_power,
    parse_resistance,
    parse_tolerance,
    parse_voltage,
)


def jlc_class(text: Any) -> str | None:
    folded = str(text or "").replace(" ", "").lower()
    if folded in {"basicpart", "基础库"}:
        return "basic"
    if folded in {"extendedpart", "扩展库"}:
        return "extended"
    return None


# --------------------------------------------------------------------------- #
# Source 1: reference cases
# --------------------------------------------------------------------------- #
def load_lcsc_categories(connection: Any) -> dict[str, str]:
    """C-number -> LCSC category string from refer_netlist peripheral rows."""

    cursor = connection.cursor()
    cursor.execute("SELECT peripheral_netlist FROM refer_netlist.device_table")
    categories: dict[str, Counter[str]] = defaultdict(Counter)
    for (payload,) in cursor:
        try:
            rows = json.loads(payload)
        except (TypeError, ValueError):
            continue
        for device in rows.values():
            props = device.get("props") or {}
            c_number = str(props.get("supplierPart") or "").strip().upper()
            category = str(props.get("deviceType") or "").strip()
            if C_NUMBER.match(c_number) and "-" in category:
                categories[c_number][category] += 1
    return {c: counter.most_common(1)[0][0] for c, counter in categories.items()}


def case_kind(category: str | None, designator: str, lcsc_name: str) -> str | None:
    if category:
        for prefix, kind in CASE_KINDS:
            if category.startswith(prefix):
                return kind
        return None
    prefix = re.match(r"([A-Za-z]+)", designator or "")
    letters = prefix.group(1).upper() if prefix else ""
    if letters in PREFIX_KINDS:
        kind, pattern = PREFIX_KINDS[letters]
        if pattern.search(lcsc_name or ""):
            return kind
    return None


def collect_case_parts(categories: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    files = sorted(glob.glob(CASE_GLOB))
    raw: dict[str, dict[str, Any]] = {}
    dropped: Counter[str] = Counter()
    instances_total = 0
    for path in files:
        case = Path(path).parents[1].name
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for component in (data.get("components") or {}).values():
            props = component.get("props", component) if isinstance(component, dict) else {}
            instances_total += 1
            c_number = str(props.get("Supplier Part") or "").strip().upper()
            if not C_NUMBER.match(c_number):
                continue
            lcsc_name = str(props.get("LCSC Part Name") or "")
            kind = case_kind(categories.get(c_number), str(props.get("Designator") or ""), lcsc_name)
            if kind is None:
                continue
            entry = raw.setdefault(
                c_number,
                {
                    "kind": kind,
                    "lcsc_category": categories.get(c_number),
                    "lcsc_name": lcsc_name,
                    "mpn": str(props.get("Manufacturer Part") or "").strip() or None,
                    "manufacturer": str(props.get("Manufacturer") or "").strip() or None,
                    "package": str(props.get("Supplier Footprint") or "").strip() or None,
                    "jlc_class": jlc_class(props.get("JLCPCB Part Class")),
                    "values": Counter(),
                    "cases": set(),
                    "instances": 0,
                },
            )
            entry["instances"] += 1
            entry["cases"].add(case)
            value = str(props.get("Value") or "").strip()
            if value and value.upper() not in {"NC", "DNP", "DNI"}:
                entry["values"][value] += 1
            if not entry["package"] and props.get("Supplier Footprint"):
                entry["package"] = str(props["Supplier Footprint"]).strip()

    parts: list[dict[str, Any]] = []
    for c_number, entry in raw.items():
        kind = entry["kind"]
        meta = KIND_META[kind]
        text = f"{entry['lcsc_name']} {' '.join(entry['values'])}"
        if "停产" in entry["lcsc_name"]:
            dropped["discontinued"] += 1
            continue
        part: dict[str, Any] = {
            "supplier_part": c_number,
            "kind": kind,
            "device_type": meta["device_type"],
            "mpn": entry["mpn"],
            "manufacturer": entry["manufacturer"],
            "package": entry["package"],
            "lcsc_name": entry["lcsc_name"] or None,
            "lcsc_category": entry["lcsc_category"],
            "jlc_class": entry["jlc_class"],
            "pins": meta["pins"],
            "source": "reference_cases",
            "usage": {"cases": len(entry["cases"]), "instances": entry["instances"]},
        }
        if kind in {"resistor", "capacitor", "capacitor_polar", "inductor", "ferrite_bead"}:
            distinct = {v.replace(" ", "").upper() for v in entry["values"]}
            if len(distinct) > 1:
                dropped["inconsistent_value"] += 1
                continue
            parser = {"resistor": parse_resistance, "capacitor": parse_capacitance, "capacitor_polar": parse_capacitance, "inductor": parse_inductance, "ferrite_bead": parse_bead}[kind]
            parsed = parser(entry["lcsc_name"]) or parser(text)
            if parsed is None:
                dropped["unparsed_value"] += 1
                continue
            if not part["package"]:
                dropped["no_package"] += 1
                continue
            part["value"], part["value_si"] = parsed
            part["tolerance"] = parse_tolerance(text)
            if kind in {"capacitor", "capacitor_polar"}:
                part["rated_voltage"] = parse_voltage(entry["lcsc_name"])
                part["dielectric"] = parse_dielectric(text)
            if kind == "resistor":
                part["power"] = parse_power(entry["lcsc_name"])
        elif kind == "led":
            color = parse_led_color(text)
            if color is None or not part["package"]:
                dropped["led_without_color_or_package"] += 1
                continue
            if re.search(r"双色|三色|[,，+]", entry["lcsc_name"]):
                dropped["led_multicolor"] += 1
                continue
            part["color"] = color
        else:
            if not part["mpn"]:
                dropped["no_mpn"] += 1
                continue
        parts.append(part)

    stats = {
        "enet_files": len(files),
        "component_instances": instances_total,
        "candidate_c_numbers": len(raw),
        "kept": len(parts),
        "dropped": dict(dropped),
    }
    return parts, stats


# --------------------------------------------------------------------------- #
# Source 2: device catalog
# --------------------------------------------------------------------------- #
def collect_catalog_parts(connection: Any, usage: dict[str, dict[str, int]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cursor = connection.cursor(dictionary=True)
    parts: list[dict[str, Any]] = []
    per_kind: dict[str, int] = {}
    for category, meta in CATALOG_KINDS.items():
        cursor.execute(
            f"SELECT d.supplier_part, d.device_name, d.price, d.pin_count, x.* "
            f"FROM device_parameters.devices d JOIN device_parameters.params_{category} x USING(device_id) "
            f"WHERE d.category_level_2 = %s ORDER BY d.device_id",
            (category,),
        )
        rows = cursor.fetchall()
        per_kind[category] = len(rows)
        for row in rows:
            params = {
                key: str(value).strip()
                for key, value in row.items()
                if key not in CATALOG_SKIP_COLUMNS
                and key not in {"supplier_part", "device_name", "price", "pin_count"}
                and not key.endswith(("_min", "_max", "_unit"))
                and value not in (None, "")
            }
            c_number = str(row["supplier_part"]).strip().upper()
            variant = " / ".join(params.get(field, "?") for field in meta["variant"])
            keep = set(meta["variant"]) | CATALOG_KEEP_COLUMNS
            params = {key: value for key, value in params.items() if key in keep}
            parts.append(
                {
                    "supplier_part": c_number,
                    "kind": category,
                    "device_type": meta["device_type"],
                    "mpn": str(row["device_name"]).strip(),
                    "variant": variant,
                    "parameters": params,
                    "price_cny": float(row["price"]) if row["price"] is not None else None,
                    "pins": int(row["pin_count"]) if row["pin_count"] is not None else None,
                    "source": "device_catalog",
                    "usage": usage.get(c_number, {"cases": 0, "instances": 0}),
                }
            )
    return parts, {"categories": per_kind, "kept": len(parts)}


def case_usage(categories: dict[str, str]) -> dict[str, dict[str, int]]:
    """Case usage for *every* C-number (catalog parts are not filtered by kind)."""

    usage: dict[str, dict[str, Any]] = {}
    for path in glob.glob(CASE_GLOB):
        case = Path(path).parents[1].name
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for component in (data.get("components") or {}).values():
            props = component.get("props", component) if isinstance(component, dict) else {}
            c_number = str(props.get("Supplier Part") or "").strip().upper()
            if C_NUMBER.match(c_number):
                entry = usage.setdefault(c_number, {"cases": set(), "instances": 0})
                entry["cases"].add(case)
                entry["instances"] += 1
    return {c: {"cases": len(e["cases"]), "instances": e["instances"]} for c, e in usage.items()}


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
def rank_key(part: dict[str, Any]) -> tuple:
    usage = part["usage"]
    price = part.get("price_cny")
    return (
        0 if part.get("jlc_class") == "basic" else 1,
        -usage["cases"],
        -usage["instances"],
        price if price is not None else 1e9,
        part["supplier_part"],
    )


def variant_of(part: dict[str, Any]) -> str:
    if part["source"] == "device_catalog":
        return part["variant"]
    fields = KIND_META[part["kind"]]["match"]
    return " / ".join(str(part.get(field) or "?") for field in fields)


def mark_defaults(parts: list[dict[str, Any]]) -> None:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for part in parts:
        part["variant"] = variant_of(part)
        groups[(part["kind"], part["variant"])].append(part)
    for rows in groups.values():
        rows.sort(key=rank_key)
        for index, part in enumerate(rows):
            part["rank"] = index + 1
            part["default"] = index == 0


# --------------------------------------------------------------------------- #
# Module routing rules (consumed by common_devices.presets)
# --------------------------------------------------------------------------- #
MODULE_PRESETS = [
    {
        "module_id": "5.2.4.5",
        "title": "USB 接口",
        "kind": "usb_connector",
        "block_if_parameters_match": r"桥|转换|CH34|CP210|FT23|hub|集线|PD 控制|PD控制|协议芯片",
        "variants": [
            {"when": r"type[- ]?c|typec|usb-c|usb c", "variant": "Type-C / Female / 16P / Surface Mount, Right Angle"},
            {"when": r"micro", "variant_prefix": "Micro-B / Female"},
            {"when": r"mini", "variant_prefix": "Mini-B / Female"},
            {"when": r"type[- ]?a|usb-a|usb a", "variant_prefix": "Type-A / Female"},
            {"when": r"type[- ]?b|usb-b", "variant_prefix": "Type-B / Female"},
        ],
        "fallback_variant": "Type-C / Female / 16P / Surface Mount, Right Angle",
        "parameter_fields": ["interface_type", "usb_version", "operating_mode"],
    },
    {
        "module_id": "5.2.5.5",
        "title": "按键子模块",
        "kind": "tactile_switch",
        "block_if_parameters_match": r"机械|磁轴|薄膜|专用|IC|芯片|矩阵",
        "variants": [
            {"when": r"侧按|side", "variant_prefix": "2.3mm / 4.7mm"},
            {"when": r"直插|through", "variant_prefix": "6mm / 6mm / 5mm / Through Hole"},
        ],
        "fallback_variant": "6mm / 6mm / 5mm / Surface Mount,Vertical / Gull Wing",
        "parameter_fields": ["key_type", "detection_method", "key_layout"],
    },
    {
        "module_id": "5.2.5.3",
        "title": "指示灯子模块",
        "kind": "light_emitting_diode_led",
        "block_if_parameters_match": r"驱动 ?IC|驱动芯片|恒流|WS2812|RGB|SK68|彩",
        "variants": [
            {"when": r"红|red", "variant_prefix": "Red / 0603"},
            {"when": r"绿|green", "variant_prefix": "Emerald"},
            {"when": r"蓝|blue", "variant_prefix": "Blue / 0603"},
            {"when": r"黄|yellow", "variant_prefix": "Yellow / 0603"},
        ],
        "fallback_variant": "Red / 0603",
        "parameter_fields": ["led_type", "drive_method", "control_method"],
    },
    {
        "module_id": "5.2.9.1",
        "title": "调试 / 测试接口",
        "kind": "pin_header",
        "block_if_parameters_match": r"板载调试器|DAP|ST-Link|J-Link|USB",
        "variants": [
            {"when": r"jtag", "variant_prefix": "2.54mm / 2 / 10P"},
            {"when": r"swd|串口|uart|烧录|debug|调试", "variant_prefix": "2.54mm / 1 / 4P"},
        ],
        "fallback_variant": None,
        "parameter_fields": ["debug_interface", "programming_interface", "test_points"],
    },
]


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    connection = mysql.connector.connect(**MYSQL)
    try:
        categories = load_lcsc_categories(connection)
        usage = case_usage(categories)
        case_parts, case_stats = collect_case_parts(categories)
        catalog_parts, catalog_stats = collect_catalog_parts(connection, usage)
    finally:
        connection.close()

    # A C-number present in both sources keeps the catalog row (it has pins and
    # price) and inherits nothing else; the case row is dropped.
    catalog_numbers = {p["supplier_part"] for p in catalog_parts}
    case_parts = [p for p in case_parts if p["supplier_part"] not in catalog_numbers]
    parts = case_parts + catalog_parts
    mark_defaults(parts)
    parts.sort(key=lambda p: (p["kind"], p["variant"], p["rank"]))

    kinds: dict[str, dict[str, Any]] = {}
    for kind, meta in KIND_META.items():
        kinds[kind] = {**meta, "source": "reference_cases", "count": sum(1 for p in parts if p["kind"] == kind)}
    for kind, meta in CATALOG_KINDS.items():
        kinds[kind] = {
            "device_type": meta["device_type"],
            "designator_prefix": meta["designator_prefix"],
            "match": ["variant"],
            "variant_fields": meta["variant"],
            "default_package": None,
            "pins": None,
            "source": "device_catalog",
            "count": sum(1 for p in parts if p["kind"] == kind),
        }
    kinds = {k: v for k, v in kinds.items() if v["count"]}

    asset = {
        "schema": "pcbagent.device_selection.preset-components.v1",
        "generated": date.today().isoformat(),
        "generator": "python -m netlist_end_to_end.common_devices.build_presets",
        "sources": {
            "reference_cases": {
                "path": "02_CASE/02_dataset/*/netlist/*.enet",
                "lcsc_category_from": "refer_netlist.device_table.peripheral_netlist[*].props.deviceType",
                "fields": {
                    "package": "Supplier Footprint",
                    "value/tolerance/rated_voltage/power": "LCSC Part Name (regex)",
                    "jlc_class": "JLCPCB Part Class",
                    "mpn/manufacturer": "Manufacturer Part / Manufacturer",
                },
                "filters": [
                    "C-number must match ^C\\d+$",
                    "LCSC Part Name must not contain 停产",
                    "R/C/L/bead: all case instances share one Value (placeholders with many values are dropped)",
                    "R/C/L/bead/LED: package required",
                    "R/C/L/bead: value must parse",
                ],
                **case_stats,
            },
            "device_catalog": {
                "database": "device_parameters",
                "tables": "devices JOIN params_<category_level_2>",
                "columns_dropped": sorted(CATALOG_SKIP_COLUMNS - {"device_id"}),
                **catalog_stats,
            },
        },
        "ranking": (
            "Within one (kind, variant): JLCPCB basic parts first, then more reference cases, "
            "then more instances, then lower price, then C-number; rank 1 is the default."
        ),
        "kinds": kinds,
        "module_presets": MODULE_PRESETS,
        "parts": parts,
    }
    ASSET.parent.mkdir(parents=True, exist_ok=True)
    ASSET.write_text(json.dumps(asset, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")

    print(f"wrote {ASSET.relative_to(ROOT)}: {len(parts)} parts")
    print("cases:", json.dumps(case_stats, ensure_ascii=False))
    print("catalog:", json.dumps(catalog_stats["categories"], ensure_ascii=False))
    by_kind = Counter(p["kind"] for p in parts)
    for kind, count in sorted(by_kind.items()):
        variants = len({p["variant"] for p in parts if p["kind"] == kind})
        print(f"  {kind:45s} parts={count:4d} variants={variants}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
