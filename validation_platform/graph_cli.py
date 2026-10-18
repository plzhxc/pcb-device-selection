"""Cleaned-format CLI implementation behind the unified validate entry."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .netlist_compare import compare_netlist_graphs, compare_pin_subgraphs
from .netlist_graph import load_case_graphs


def _terminal(graph, text):
    component, separator, pin = text.partition(":")
    key = (graph.source, component, pin)
    if not separator or key not in graph.pins:
        raise ValueError(f"unknown terminal {text!r}; use gge_id:pin_id")
    return key


def run_graph_comparison(args: Any) -> int:
    if args.identity_map:
        raise ValueError("--identity-map is a legacy supplier alias map; graph matching uses topology")
    if args.ref_enet:
        raise ValueError("cleaned graph comparison requires cleaned JSON on both sides")
    source = Path(args.ref or args.ref_source)
    generated = Path(args.gen)
    if bool(args.gen_pins) != bool(args.ref_pins):
        raise ValueError("--gen-pins and --ref-pins must be supplied together")
    if args.gen_pins and (not source.is_file() or not generated.is_file()):
        raise ValueError("pin subgraph comparison requires one file on each side")
    refs = load_case_graphs(source)
    gens = load_case_graphs(generated)
    if source.is_file() and generated.is_file():
        pairs = [(source.name, next(iter(gens.values())), next(iter(refs.values())))]
        missing = extra = []
    else:
        pairs = [(key, gens[key], refs[key]) for key in sorted(refs.keys() & gens.keys())]
        missing = sorted(refs.keys() - gens.keys())
        extra = sorted(gens.keys() - refs.keys())
    results = {}
    options = {"strict_net_names": args.strict_net_names, "max_states": args.max_match_states,
               "timeout_seconds": args.match_timeout}
    for key, gen, ref in pairs:
        if not ref.components:
            raise ValueError(f"empty reference netlist: {ref.source}")
        if args.gen_pins:
            result = compare_pin_subgraphs(gen, ref,
                       generated_pins=tuple(_terminal(gen, p) for p in args.gen_pins),
                       reference_pins=tuple(_terminal(ref, p) for p in args.ref_pins), **options)
        else:
            result = compare_netlist_graphs(gen, ref, **options)
        results[key] = result
        regions = result["peripheral_circuits"]
        values = result["values"]
        print(f"[{key}] {result['status']}: core_C_IDs={result['core_anchors']['status']}, topology={result['topology']['equivalent']}, "
              f"peripheral regions={regions['reference_regions']}/{regions['generated_regions']}, "
              f"values matched={values['matched']}, different={values['different']}, unresolved={values['unresolved']}, "
              f"not_applicable={values['not_applicable']}")
    passed = False if missing or extra or any(r["pass"] is False for r in results.values()) else (
        None if any(r["pass"] is None for r in results.values()) else True)
    report = next(iter(results.values())) if source.is_file() and generated.is_file() else {
        "schema": "pcbagent.netlist_graph_batch.v1", "pass": passed,
        "document_pairing": "relative_path; files are independent; compare renamed files explicitly",
        "missing_documents": missing, "extra_documents": extra, "comparisons": results,
    }
    if missing or extra:
        print(f"Missing documents: {missing}; extra documents: {extra}")
    print(f"Topology comparison pass={passed}; symmetric component pairing is reported explicitly.")
    if args.out:
        args.out.mkdir(mode=0o700, parents=True, exist_ok=True)
        args.out.chmod(0o700)
        (args.out / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Report: {args.out / 'metrics.json'}")
    return 0 if passed is True else 1 if passed is False else 2
