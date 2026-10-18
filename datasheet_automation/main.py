#!/usr/bin/env python
"""Sole command-line entry point for datasheet automation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


# Direct script execution needs the numbered source root on ``sys.path``.
# Installed console scripts and ``python -m datasheet_automation.main`` already
# have it.
project_root = str(Path(__file__).resolve().parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from datasheet_automation.core.application import run


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        prog="datasheet-automation",
        description="Run one configured datasheet acquisition/processing workflow.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    run_command = subcommands.add_parser("run", help="Run a YAML request")
    run_command.add_argument("config", help="Path to config.yaml")
    ingest = subcommands.add_parser("ingest-case", help="Build the v2 knowledge base for one evaluation case")
    ingest.add_argument("--case", required=True, help="Case directory name under 02_CASE/02_dataset, e.g. case1-keyboard")
    ingest.add_argument("--only", nargs="+", default=(), help="Restrict to these confirmed C/S device IDs")
    ingest.add_argument("--limit", type=int, default=None, help="Process at most N devices (most-used first)")
    ingest.add_argument("--reuse-only", action="store_true", help="Only ingest devices whose run directory already exists")
    ingest.add_argument("--workers", type=int, default=3, help="Devices processed concurrently (default 3)")
    args = parser.parse_args(argv)

    if args.command == "ingest-case":
        from datasheet_automation.knowledge.case_ingest import ingest_case

        try:
            summary = ingest_case(args.case, only=args.only, limit=args.limit, reuse_only=args.reuse_only, workers=args.workers)
        except (ValueError, FileNotFoundError) as exc:
            print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 0 if summary["ok"] == summary["devices_total"] and summary["devices_total"] > 0 else 1

    response = run(args.config)
    print(json.dumps(response, ensure_ascii=False, indent=2, default=str))
    return 0 if response.get("status") in {"ok", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
