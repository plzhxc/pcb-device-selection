"""validation_platform — 独立网表验证平台（case-agnostic）。

吃下参考网表（baseline）与目标网表，给出正式三指标的数值和一份网表比较报告：

- 元件数量偏差
- 元件取值准确率
- strict pin 线网连接准确率

新格式 v1.2：component/pin/net 图；核心 C 编号锚点、逐层拓扑匹配、核心引脚间外围子图及取值比较。
公开图 API 见 ``parse_netlist_graph``、``compare_netlist_graphs``、``compare_pin_subgraphs``。
``compute_coverage`` 对新格式返回带 schema 的完整图比较报告，旧格式维持三指标响应。

旧格式输入：两个 flat netlist (gge_id → device dict)，或 gen netlist + .enet 参考目录
（reference/ 子包统一适配 enet / BOM / EasyEDA PCB extraction 多种参考格式）。
旧 topology/connect F1、supplier_part 覆盖率、R/C/L 分类平均覆盖率不再导出。

模块::

    graph.py                    器件类型和值解析 helper
    netlist_graph.py            新格式完整图与核心引脚子图提取
    netlist_compare.py          拓扑同构、外围子图、对应位置取值比较
    electrical_values.py        严格单位与十进制数值归一化
    graph_cli.py                新格式单文件/目录/端点子图比较
    merge.py                    跨 module 网表合并（union-find + 启发式同名 net union）
    coverage.py                 正式三指标
    connect_abstraction.py      pin 级连接路径抽象
    connect_compare.py          连接比较核心
    device_conduction_rules.py  器件导通规则
    ref_parser.py               EasyEDA .enet 多页参考网表 → 扁平 dict
    reference/                  多格式参考网表适配（enet / bom / easyeda_pcb + registry）
    status_report.py            评测状态报告渲染
    cli.py                      命令行入口
    __main__.py                 ``python -m validation_platform`` 转发到 cli

用法::

    validation-platform --gen gen.json --ref ref.json --out report/
    validation-platform --gen gen.json --ref-enet enet_dir/ --out report/
"""
from validation_platform.graph import normalize_type
from validation_platform.coverage import compute_coverage
from validation_platform.merge import merge_netlists, collect_gen_netlists
from validation_platform.ref_parser import parse_all_sheets, device_count_summary
from validation_platform.reference import load_reference, load_reference_dir
from validation_platform.netlist_graph import (
    NetlistGraph, NetlistFormatError, parse_netlist_graph, load_netlist_graph,
    load_case_graphs, extract_pin_subgraph,
)
from validation_platform.netlist_compare import (
    compare_cleaned_netlists, compare_netlist_graphs, compare_pin_subgraphs,
)

__all__ = [
    "normalize_type",
    "compute_coverage",
    "merge_netlists", "collect_gen_netlists",
    "parse_all_sheets", "device_count_summary",
    "load_reference", "load_reference_dir",
    "NetlistGraph", "NetlistFormatError", "parse_netlist_graph", "load_netlist_graph",
    "load_case_graphs", "extract_pin_subgraph", "compare_cleaned_netlists",
    "compare_netlist_graphs", "compare_pin_subgraphs",
]
