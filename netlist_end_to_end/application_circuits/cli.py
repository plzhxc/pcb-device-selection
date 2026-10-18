"""Offline application-fragment experiments behind the unified PCBAgent CLI."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import Any, Sequence

from ..netlist.scl import SCLSource
from .applicability import assess
from .evaluation import evaluate_experiment
from .instantiate import compile_application
from .models import Fragment, Target

_RUNTIME_ROOT = Path(__file__).resolve().parents[3] / "07_TEMP"
_MAX_INPUT_BYTES = 20_000_000


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number exceeds finite float range")
    return number


def read_json(path: str | Path) -> dict:
    source = Path(path)
    if source.stat().st_size > _MAX_INPUT_BYTES:
        raise ValueError("input exceeds the 20 MB experiment limit")
    value = json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                       parse_constant=_invalid_constant, parse_float=_finite_float)
    if not isinstance(value, dict):
        raise ValueError("input must be a JSON object")
    return value


def _write_result(value: Any, output: str | None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if output is None:
        print(text, end="")
        return
    destination = Path(output).resolve()
    if not destination.is_relative_to(_RUNTIME_ROOT.resolve()):
        raise ValueError("run outputs must be under Worklib/07_TEMP; source and case paths are read-only")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with destination.open("x", encoding="utf-8") as handle:
        handle.write(text)
    destination.chmod(0o600)
    print(str(destination))


def _compile(request: dict, catalog: dict) -> dict:
    required = {"fragment_id", "target", "context", "module", "device_alias", "port_bindings"}
    if set(request) - required - {"support_sources"} or required - set(request):
        raise ValueError("compile request requires fragment_id, target, context, module, device_alias, port_bindings")
    fragments = [Fragment.model_validate(f) for f in catalog["fragments"]]
    if len({f.id for f in fragments}) != len(fragments):
        raise ValueError("duplicate fragment IDs in catalog")
    matches = [f for f in fragments if f.id == request["fragment_id"]]
    if len(matches) != 1:
        raise ValueError("requested fragment is absent from the frozen catalog")
    sources = []
    for row in request.get("support_sources", []):
        if not isinstance(row, dict) or set(row) != {"name", "module", "text"}:
            raise ValueError("support_sources must use the SCLSource fields name, module, text")
        sources.append(SCLSource(**row))
    return compile_application(matches[0], Target.model_validate(request["target"]), request["context"],
        module=request["module"], device_alias=request["device_alias"],
        port_bindings=request["port_bindings"], accepted_digests=catalog["accepted_digests"],
        support_sources=sources)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline conditional application-circuit research; no database writes")
    sub = parser.add_subparsers(dest="command", required=True)
    schema = sub.add_parser("schema", help="print the strict fragment and target JSON schemas")
    schema.add_argument("--output")
    evaluate = sub.add_parser("evaluate", help="evaluate a curated applicability experiment")
    evaluate.add_argument("input")
    evaluate.add_argument("--output")
    assess_parser = sub.add_parser("assess", help="assess every catalog fragment for one frozen target")
    assess_parser.add_argument("catalog")
    assess_parser.add_argument("target")
    assess_parser.add_argument("--output")
    compile_parser = sub.add_parser("compile", help="instantiate and compile an explicitly selected partial fragment")
    compile_parser.add_argument("request")
    compile_parser.add_argument("--catalog", required=True)
    compile_parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        if args.command == "schema":
            result = {"fragment": Fragment.model_json_schema(), "target": Target.model_json_schema()}
        elif args.command == "evaluate":
            result = evaluate_experiment(read_json(args.input))
        elif args.command == "assess":
            catalog, target = read_json(args.catalog), Target.model_validate(read_json(args.target))
            fragments = [Fragment.model_validate(f) for f in catalog["fragments"]]
            if len({f.id for f in fragments}) != len(fragments):
                raise ValueError("duplicate fragment IDs in catalog")
            result = {f.id: assess(f, target, accepted_digests=catalog["accepted_digests"]) for f in fragments}
        else:
            result = _compile(read_json(args.request), read_json(args.catalog))
        _write_result(result, args.output)
        return 0 if args.command != "compile" or result["ok"] else 1
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        print(f"application-circuits: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
