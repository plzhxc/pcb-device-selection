# 合并说明

本目录是**器件选型阶段**的完整交付。分两部分：

| 部分 | 内容 | 交付方式 |
| --- | --- | --- |
| 一 | 器件选型阶段的全部实现与文档 | 本仓库，放到 `01_PCBAgent/device_selection/` |
| 二 | 需求分析工作台的跳转按钮（改了 2 个文件） | 见 `merge/`，覆盖或打补丁都行 |

两部分都在本仓库里，**一次 pull 就齐了**。

---

## 1. 仓库内容

```
device_selection/
├── README.md              总说明（含第 4 节「判定器件选型条件的规则」）
├── MERGE.md               本文件
├── cli.py                 命令行入口
├── run_web.sh             启动网页端
├── src/                   实现（16 个模块）
├── web/                   网页端：server.py + static/index.html
├── assets/                预置元件库（成本参考与预设直达）
├── specs/                 手写示例需求清单
├── cases/                 回归案例库：31 个案例、527 个模块
├── testcases/             手工测试用例：4 个，含两阶段预期输出
├── tools/                 案例构建、接口回归、桥接、批量测试
├── docs/                  设计文档
└── merge/                 给工作台用的补丁（见第 3 节）
    ├── README.md          工作台部分怎么并
    ├── chenxin-workbench-jump-button.patch
    ├── crypto-shim.cjs
    └── files/             可直接覆盖的完整文件（按仓库原始路径摆放）
```

**不入库的内容**（见 `.gitignore`）：

| 路径 | 原因 |
| --- | --- |
| `output/` | 运行产物：选型结果、项目缓存、回归报告，可随时重跑生成 |
| `legacy/` | 从 `Worklib/backup/retired-front-stages-20261014/` 还原的历史代码，只作本地参考；其中预置元件库已复制到 `assets/`，代码不再依赖它 |

---

## 2. 依赖与环境

| 项 | 要求 | 说明 |
| --- | --- | --- |
| Python | 3.12（实测） | 需要 `mysql-connector-python`、`openai`（可选）、`pydantic`（仅接口校验用） |
| MySQL | `device_parameters_dev` | 只读连接。默认走本机 socket：`/share/home/chenxin/my-database/run/mysql.sock`，账号 `pcb_dev`，空密码 |
| Milvus | `http://127.0.0.1:19530` | 可选。连不上不影响选型，只是补不到缺失参数 |
| 需求分析工作台 | `http://[::1]:8878` | 可选。不连也能用 `specs/` 里的示例清单 |

环境变量（都有默认值，通常不用设）：

```bash
export DEVICE_SELECTION_DB_MODE=socket        # 或 tcp
export DEVICE_SELECTION_DB_HOST=10.131.254.102
export DEVICE_SELECTION_DB_USER=datas_rw
export DEVICE_SELECTION_DB_PASSWORD='...'     # TCP 模式才需要
export DEVICE_SELECTION_LLM=off               # 关掉大模型辅助
export DEVICE_SELECTION_STUDIO=http://[::1]:8878
```

启动：

```bash
python3 cli.py health    # 先确认数据库通
./run_web.sh             # 打开 http://127.0.0.1:8899/
```

---

## 3. 工作台补丁（需要 chenxin 应用）

### 补丁做了什么

在需求分析工作台**顶栏「导出」右边**加一个「器件选型 ↗」按钮，点击打开
`http://<主机名>:8899/?project=<项目编号>`，我的界面认这个参数会自动载入该项目。

改动只有两处：一行图标 import、顶栏加一个 `<a>`，约 334 个字符。

### 怎么应用

```bash
cd <PCBAgent 仓库根>
git apply /path/to/merge/chenxin-workbench-jump-button.patch
cd requirements_studio/frontend && npm run build     # 重新构建 dist（dist 不入库）
```

### 关于 `crypto-shim.cjs`

`merge/crypto-shim.cjs` 是我这边为了在 **Node 16** 上构建 Vite 6 加的垫片
（Node 16 的 `crypto` 模块没有 `getRandomValues`，Vite 6 会直接调用它）。

**如果构建机的 Node ≥ 18，不需要这个文件。** 需要时这样构建：

```bash
node --require ./crypto-shim.cjs ./node_modules/vite/bin/vite.js build
```

### 待确认

1. 这个按钮要不要保留、放在哪个位置；
2. 端口 `8899` 目前是硬编码的，是否要改成可配置；
3. 按钮文案与图标是否合适。

---

## 4. 怎么验收

### 4.1 不依赖工作台（最快）

```bash
python3 cli.py health
python3 cli.py run --spec specs/example_power.json --out output/demo_power.json
python3 tools/run_cases.py            # 31 个案例回归，看 output/case_runs/summary.md
```

### 4.2 接口回归（46 份真实交接件）

```bash
python3 tools/run_chenxin_specs.py --writeback
```

预期：`经过参数核对并有推荐 276（52.3%）`，写回后通过他们契约校验 `46 / 46`。

### 4.3 端到端（需要工作台在跑）

```bash
python3 tools/bridge_studio.py list
python3 tools/bridge_studio.py run --project <project_id>
```

预期：输出 `state=input_validated`、`can_start_generation=True`，
`generation-input.json` 是合法的 `pcbagent.netlist_input.v1`。

### 4.4 网页端手工走一遍

用 `testcases/README.md` 里那 4 个用例：左栏点项目 → 按「开始选型」→
展开每行「证据」对照预期。

---

## 5. 当前状态与已知缺口

| 项 | 状态 |
| --- | --- |
| 候选召回、参数核对、三态判定、下游交接 | 已实现并实测 |
| 数据手册补数（Milvus） | 已接入，缺失参数补回率约 35% |
| 用户决定持久化 + 只导出「已确认」 | 已实现 |
| 工作台接口适配（读 spec / 写回） | 已实现，46/46 通过契约校验 |
| **候选排序** | 偏弱：只按「是否推荐 + 证据完整度 + device_id」，偏好条件还没进入排序 |
| **成本** | 器件库没有价格字段，只有预设元件库能给参考价 |
| **大模型** | 已接入但只做意图翻译；单次 24~46 秒，仅作兜底 |
| 远端数据库账号 | `datas_rw` 密码在 chenxin 的 0600 配置文件里，本账号读不到 |

详细的规则说明见 [README.md](README.md) 第 4 节；
设计文档见 [docs/器件选型阶段设计.md](docs/器件选型阶段设计.md)。
