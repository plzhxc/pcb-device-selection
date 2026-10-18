# PCBAgent 合并树（需求分析 + 器件选型）

把**需求分析工作台**（陈鑫）与**器件选型阶段**（郑智齐）合到一个可运行的工程树里，
两边的改动都已应用，不需要再打补丁。

## 目录

| 目录 | 来源 | 说明 |
| --- | --- | --- |
| `requirements_studio/` | 陈鑫 | 需求分析工作台（FastAPI + React/Vite），**含器件选型下拉菜单改动** |
| `device_selection/` | 郑智齐 | 器件选型阶段（候选召回、参数核对、三态判定、下游交接） |
| `netlist_end_to_end/` | 陈鑫 | 网表生成主流程；`generation_adapter` 依赖它 |
| `agent_framework/` | 陈鑫 | Agent 运行与 Trace 框架 |
| `validation_platform/` | 陈鑫 | 校验平台（器件类型表等） |
| `datasheet_automation/` | 陈鑫 | 数据手册自动化 |
| `pyproject.toml` | 陈鑫 | 打包声明 |

`requirements_studio` 的 `handoff.generation_adapter` 通过
`Path(__file__).parents[2]` 找 `netlist_end_to_end`，在本树里正好是根目录，
所以**目录层级不要打散**。

## 两处改动

### 1. 器件选型阶段（新增）

完整实现见 `device_selection/README.md`，要点：

- 按 taxonomy 类别从真实器件目录召回候选，不用模型记忆造型号；
- 参数逐条核对，结论只有「满足 / 不满足 / 未知」，未知不当满足；
- 单位归一化四道闸门（文本优先、基准单位、严格解析、单位族校验）；
- 结构化表缺的字段去 Milvus 手册补，带原文引用与页码；
- 用户决定单独落盘，只导出「已确认」的器件到 `pcbagent.netlist_input.v1`。

### 2. 需求分析工作台（修改 2 个文件）

| 文件 | 改动 |
| --- | --- |
| `frontend/src/App.tsx` | 顶栏「导出」右边加「器件选型 ▾」下拉 |
| `frontend/crypto-shim.cjs` | 新增；仅 Node < 18 构建时需要 |

下拉两个选项：

| 选项 | 行为 |
| --- | --- |
| 打开选型界面 | 新标签打开 `:8899/?project=<编号>`，自动载入该项目 |
| 自动选型并导出下游文件 | 不跳转，后台跑完选型 → 全部确认 → 下载 `netlist_input` |

## 运行

### 依赖

- Python 3.12（`fastapi`、`uvicorn`、`pydantic`、`mysql-connector-python`、`openai`）
- Node ≥ 18（构建前端；本树里 `frontend/dist` 未提交）
- MySQL：`device_parameters_dev`（只读）
- Milvus：`http://127.0.0.1:19530`（可选）

### 启动

```bash
# 1) 构建工作台前端
cd requirements_studio/frontend && npm ci && npm run build && cd ../..

# 2) 启动工作台（8878）
cd requirements_studio && uvicorn backend.app:app --host 127.0.0.1 --port 8878

# 3) 启动器件选型（8899）
cd device_selection && ./run_web.sh
```

工作台顶栏点「器件选型 ▾」→「打开选型界面」，或直接访问
<http://127.0.0.1:8899/>。

### 环境变量（都有默认值）

```bash
DEVICE_SELECTION_DB_MODE=socket|tcp
DEVICE_SELECTION_DB_HOST=10.131.254.102
DEVICE_SELECTION_DB_USER=datas_rw
DEVICE_SELECTION_DB_PASSWORD=...
DEVICE_SELECTION_STUDIO=http://127.0.0.1:8878
DEVICE_SELECTION_PCBAGENT_ROOT=<本树根>      # 一般不用设
DEVICE_SELECTION_STUDIO_ROOT=<本树根>        # 一般不用设
DEVICE_SELECTION_LLM=off                     # 关掉大模型辅助
```

## 验证

```bash
cd device_selection
python3 cli.py health
python3 tools/run_cases.py             # 31 个回归案例
python3 tools/run_chenxin_specs.py --writeback   # 46 份真实交接件
```

`tools/run_chenxin_specs.py` 需要读到本树的 `requirements_studio`（同目录即可）。

## 已知边界

- `requirements_studio/frontend/node_modules` 与 `dist` 未提交，需自行 `npm ci && npm run build`。
- 工作台的 `.runtime/`（项目数据）未提交，首次启动会重建空库。
- 器件库没有价格字段，成本只能来自预置元件库；查不到标「待确认」，不按 0 元算。
- 候选排序目前只按「结论 → 证据完整度 → device_id」，偏好条件未进入排序。
