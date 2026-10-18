"""Local common-device compilation through the unified PCBAgent CLI."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Sequence

from .compiler import compile_circuit
from .errors import CommonDeviceError
from .registry import TemplateRegistry


_OUTPUT_ROOT = Path(__file__).resolve().parents[3] / "07_TEMP" / "common-devices"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CommonDeviceError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise CommonDeviceError(f"Non-finite JSON number is not allowed: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise CommonDeviceError(f"JSON number exceeds finite range: {value}")
    return number


def _read_plan(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = json.load(
            stream,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    if not isinstance(value, dict):
        raise CommonDeviceError("Circuit input must be a JSON object")
    return value


def _output_path(value: str) -> Path:
    output = Path(os.path.abspath(Path(value).expanduser()))
    try:
        relative = output.relative_to(_OUTPUT_ROOT)
    except ValueError as exc:
        raise CommonDeviceError(f"Output must be inside {_OUTPUT_ROOT}") from exc
    if not relative.parts:
        raise CommonDeviceError("Output must be a child directory of the runtime root")

    current = _OUTPUT_ROOT.parent
    for part in (_OUTPUT_ROOT.name, *relative.parts):
        if current.is_symlink():
            raise CommonDeviceError(f"Output path contains a symbolic link: {current}")
        current = current / part
        if current.is_symlink():
            raise CommonDeviceError(f"Output path contains a symbolic link: {current}")
        if current.exists() and not current.is_dir():
            raise CommonDeviceError(f"Output parent is not a directory: {current}")
    if not output.resolve().is_relative_to(_OUTPUT_ROOT.resolve()):
        raise CommonDeviceError("Resolved output escapes the runtime root")
    if output.exists() and any(output.iterdir()):
        raise CommonDeviceError(f"Refusing non-empty output directory: {output}")
    return output


def _remove_owned(path: Path, identity: tuple[int, int]) -> None:
    try:
        actual = path.lstat()
    except FileNotFoundError:
        return
    if (actual.st_dev, actual.st_ino) == identity:
        path.unlink()


def _write_result(output: Path, result: Any) -> None:
    # Serialize before creating any artifacts.  Publish the manifest last.
    payloads = [
        ("netlist.json", result.netlist),
        ("core_devices.json", result.core_devices),
        ("manifest.json", result.manifest),
    ]
    encoded = [
        (name, (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))
        for name, value in payloads
    ]
    created_directories: list[Path] = []
    owned: dict[Path, tuple[int, int]] = {}
    lock = output / ".common-devices-write.lock"
    try:
        missing: list[Path] = []
        current = output
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
            created_directories.append(directory)
        _output_path(str(output))

        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        info = os.fstat(descriptor)
        owned[lock] = (info.st_dev, info.st_ino)
        os.close(descriptor)
        if any(path != lock for path in output.iterdir()):
            raise CommonDeviceError(f"Output directory changed during creation: {output}")

        for name, content in encoded:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".write-", suffix=".json", dir=output)
            temporary = Path(temporary_name)
            info = os.fstat(descriptor)
            owned[temporary] = (info.st_dev, info.st_ino)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            final = output / name
            # Unlike replace(), link() never overwrites an existing destination.
            os.link(temporary, final)
            owned[final] = owned[temporary]
            _remove_owned(temporary, owned[temporary])
            del owned[temporary]

        _remove_owned(lock, owned[lock])
        del owned[lock]
    except BaseException:
        for path, identity in reversed(list(owned.items())):
            try:
                _remove_owned(path, identity)
            except OSError:
                pass
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcbagent templates",
        description="Compile local common-device templates without database or LLM calls.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list the built-in template registry as JSON")
    compile_parser = commands.add_parser("compile", help="compile a circuit JSON file")
    compile_parser.add_argument("input", type=Path)
    compile_parser.add_argument("--output", required=True, help="empty directory under 07_TEMP/common-devices")
    example_parser = commands.add_parser("example", help="compile a built-in circuit example")
    example_parser.add_argument("name")
    example_parser.add_argument("--output", required=True, help="empty directory under 07_TEMP/common-devices")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        registry = TemplateRegistry.load_builtin()
        if args.command == "list":
            print(json.dumps(registry.list_templates(), ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        output = _output_path(args.output)
        plan = _read_plan(args.input) if args.command == "compile" else registry.load_example(args.name)
        result = compile_circuit(plan, registry=registry)
        _write_result(output, result)
    except (CommonDeviceError, OSError, ValueError, UnicodeError) as exc:
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output": str(output), "files": ["netlist.json", "manifest.json", "core_devices.json"]}, ensure_ascii=False))
    return 0
