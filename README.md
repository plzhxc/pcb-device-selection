# PCBAgent 合并树（需求分析 + 器件选型）

把**需求分析工作台**（陈鑫）与**器件选型阶段**（郑智齐）合到一个可运行的工程树里，
两边的改动都已应用，不需要再打补丁。

2026-10-20 已同步陈鑫最终版：新增器件卡片联动复核、方案关系/变更影响/收敛检查三张图谱、
器件方案编辑与参考 BOM 保留等改动；我方「器件选型」标签页和需求回填逻辑已重新接上。

## 目录

| 目录 | 来源 | 说明 |
| --- | --- | --- |
| `requirements_studio/` | 陈鑫 | 需求分析工作台（FastAPI + React/Vite），**含器件选型同级标签页改动** |
| `device_selection/` | 郑智齐 | 器件选型阶段（候选召回、参数核对、三态判定、下游交接） |
| `netlist_end_to_end/` | 陈鑫 | 网表生成主流程；`generation_adapter` 依赖它 |
| `agent_framework/` | 陈鑫 | Agent 运行与 Trace 框架 |
| `validation_platform/` | 陈鑫 | 校验平台（器件类型表等） |
| `datasheet_automation/` | 陈鑫 | 数据手册自动化 |
| `pyproject.toml` | 陈鑫 | 打包声明 |

`requirements_studio` 的 `handoff.generation_adapter` 通过
`Path(__file__).parents[2]` 找 `netlist_end_to_end`，在本树里正好是根目录，
所以**目录层级不要打散**。

## 整合方式

### 1. 器件选型阶段（新增）

完整实现见 `device_selection/README.md`，要点：

- 按 taxonomy 类别从真实器件目录召回候选，不用模型记忆造型号；
- 参数逐条核对，结论只有「满足 / 不满足 / 未知」，未知不当满足；
- 单位归一化四道闸门（文本优先、基准单位、严格解析、单位族校验）；
- 结构化表缺的字段去 Milvus 手册补，带原文引用与页码；
- 用户决定单独落盘，只导出「已确认」的器件到 `pcbagent.netlist_input.v1`。

### 2. 需求分析工作台（增加一个同级标签页）

| 文件 | 改动 |
| --- | --- |
| `frontend/src/App.tsx` | 「需求说明书 / 收敛路径 / 模块方案」后面增加「器件选型」标签页 |
| `frontend/src/styles.css` | 选型页嵌入画布时的尺寸与边框 |
| `frontend/crypto-shim.cjs` | Node < 18 构建时的兼容垫片 |

切换到这个标签页时，画布内嵌入 `http://<当前主机>:8899/?project=<项目编号>&embed=1`。
选型页会隐藏自己的项目栏和对话栏，只保留选型正文，因此视觉上与另外三个子页面同级。
需求分析、模块方案和器件选型仍然由两个服务分别负责，通过 HTTP 和项目编号衔接。
原有的“自动选型并导出下游文件”快捷操作保留在选型页的「交付与交接」区域，
一次完成取最新交接件、选型、全部确认和生成 `generation-input.json`。

### 3. 选型结果回填需求说明书

需求分析阶段定不下来的条目会保留为 `tbd`（待确认）。器件选型完成后，
如果某个器件的实际参数已经能唯一确定这条需求，选型页会出现「需求补充建议」：

1. 只处理 `tbd` 或值为空的需求，不改用户已经明确写下的要求；
2. 优先使用用户已确认的器件，没有确认器件时才看首选候选；
3. 需求名和参数名必须属于同一类（频率、电流、电压、温度、封装、接口、容量、精度、通道），
   或名称互相包含；
4. 器件参数必须有确定值，`—`、`TBD`、缺失证据都不算确定；
5. 没有关联到具体选型项的待定需求，只有在全项目内**唯一匹配**时才提建议；
   多个器件给出不同值时视为有歧义，不猜；
6. 采纳后通过工作台自己的 `PATCH /api/projects/{id}/requirements` 写回，
   状态是 `suggested`（界面显示为「AI 建议」），不会冒充用户原话，也不自动审核；
7. 写回成功后会通知工作台立即刷新需求说明书；已经回填过的条目不会再次出现在建议里。

因此这部分**不需要修改陈鑫的后端逻辑**，只使用他已有的需求编辑接口。
像「外设接口数量与统计口径」这种不是单颗器件参数的条目，界面会明确列出
“暂时不能自动回填”，不会用猜测值覆盖。

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

打开工作台后，在右侧画布顶部切换到「器件选型」标签页即可使用。
也可以直接访问 <http://127.0.0.1:8899/?project=<项目编号>> 单独查看。

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
- 选型页目前以 iframe 嵌入工作台，两个服务仍分别运行在 8899 和工作台端口；
  端口 `8899` 写在 `App.tsx` 与启动命令里，换端口需要同步修改。
- 回填建议只覆盖“能从器件参数直接确定”的需求；数量统计、接口分配等需要
  人工判断的内容会保留待确认，不会自动生成。
