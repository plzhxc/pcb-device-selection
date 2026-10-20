# PCBAgent 需求工作台

2026-10-20 器件联动更新：在器件卡片修改型号、意图、数量或用途后，AI同步核对需求说明书、模块、接口、流程和本版器件表；未完成的关联复核阻止重新审核。原参考BOM单独保留。收敛路径新增“方案关系、变更影响、收敛检查”三张可筛选、拖动、缩放和全屏的图谱。

- [实施方案](../docs/需求分析工作台_器件联动与收敛图谱实施方案_2026-10-20.md)和[实际验收与交付报告](../docs/需求分析工作台_器件联动与收敛图谱验收报告_2026-10-20.md)。
- 正式入口仍是8878。追加的[玩具换主控副本](http://localhost:8878/?project=1eef1f3be7aa4601b560d7587e3c5792)与[语音板换稳压器副本](http://localhost:8878/?project=a679c79631b54c55ae6976aae5bfa76d)保留真实修改、短句讨论、AI整理、审核和导出过程。原127个项目不被覆盖，现共129个。
- 最终301项后端测试通过；两个副本的三图操作、布局、刷新保留、跳转、手机和交接核对通过。未运行完整网表生成。
- 本轮隔离服务为 `scripts/serve_canvas_sync.py`，使用 `.runtime/canvas-sync-20261020/live` 和8896端口；与下方旧隔离脚本不要同时启动。发布脚本为 `scripts/publish_canvas_sync.py`，已经追加发布，勿重复执行`--apply`。
- 只读核对：`../.venv/bin/python -X utf8 scripts/audit_canvas_sync_cases.py --base 'http://[::1]:8878' --phase manual`。浏览器检查见 `tests/canvas_sync_browser.mjs`，需先启动 `tests/shared_browser.mjs` 临时浏览器。

以下为此前渐进输入版本的交付记录。

2026-10-20 更新：保留 **需求说明书、收敛路径、模块方案** 三视图，完善少量输入的持续引导、局部关联复核、参考 ENET/BOM 上传、核心方案采用、接口单行编辑、四项交接检查及最终参考复审。默认阅读本项目条款，完整 V3 规范仍可展开。工程与资料任务单独承接，不作为客户问卷。

本轮实施与实际验收：

- [少量输入引导与参考 BOM 优化方案](../docs/需求分析工作台_少量输入引导与参考BOM优化方案_2026-10-19.md)
- [两案例真实过程、核心 BOM 与发布报告](../docs/需求分析工作台_渐进需求与参考BOM优化验收报告_2026-10-20.md)
- [按键发声玩具](http://localhost:8878/?project=dcb11c66b32d456cb53a031c7c9501a4)与[小智语音板](http://localhost:8878/?project=ff7d435768234150b10beb1997d014f9)：均从短句开始，完整过程已保存在正式入口。原 125 个项目保持原样，本轮追加 2 个。
- 隔离运行：先在 frontend 执行 `npm run build -- --outDir ../.runtime/ux-optimization-20261019/dist`，再运行 `../.venv/bin/python -X utf8 scripts/serve_ux_optimization.py`，端口 `8896`。旧隔离脚本也占用此端口，不要同时启动。
- 只读界面复核：`TMPDIR=/share/home/chenxin/.pcba-tmp node tests/incremental_ui_browser.mjs 'http://[::1]:8878'`。需先启动同环境的 `tests/shared_browser.mjs` 临时浏览器；不自动调用模型。
- 原文件与 BOM 核对：`../.venv/bin/python -X utf8 scripts/audit_reference_cases.py --base 'http://[::1]:8878'`。
- 首次追加式发布脚本为 `scripts/publish_ux_optimization.py`，本轮已执行。它会备份并核对旧记录、拒绝覆盖同 ID 项目；不要重复执行 `--apply`。
- 主入口仍用下方的 `./start.sh`，端口 `8878`；最终需求验收由用户完成。

此前[确定约束与决策树方案](../docs/需求分析工作台_确定约束与决策树重构方案_2026-10-19.md)、[开发记录](../docs/需求分析工作台_确定约束与决策树开发验收记录_2026-10-19.md)和[五案例报告](../需求分析工作台_五案例真实用户压测与优化报告_2026-10-19.md)保留历史阶段说明。当前行为以本轮方案和验收报告为准，旧发布脚本不用于本轮。

独立运行的硬件需求文档工作台。中心对话，右侧同步呈现规范说明书、当前决策树、功能模块和可拖动的系统框图及工作流程。固定使用真实 Codex CLI：默认 `gpt-6-luna` + `xhigh`，请求 fast 服务档；实际配置与每次调用收据一起保存。

第二阶段增加“收敛路径”和“模块方案”。默认只呈现产品决定；相关工程核对、V3的50小节/247字段完整参考分别展开。模块按指定规范的10大类、60父类、197子类挑选，说明书只列本项目使用的模块。用户按类别、功能与边界选择，内部编号用于追溯。

第三阶段补齐器件意图、真实只读资料、证据与条件核对、变更影响及版本化交接。工作台保持独立运行，只有显式检查后续输入时才调用现有流程的输入构造与读取入口。文档和会话保存在本工作台的 SQLite 中。

## 立即使用

当前环境已安装前端依赖并构建，Python 复用上级 `.venv`。在本目录执行：

```bash
./start.sh
./start.sh status
```

默认访问 **http://[::1]:8878/**。本机浏览器也可使用 http://localhost:8878/。

服务后台运行，日志为 `.runtime/server.log`。停止：

```bash
./start.sh stop
```

前台启动及其他本地地址：

```bash
./start.sh --foreground
./start.sh --host 127.0.0.1 --port 8890
```

当前机器 IPv4 loopback 的请求会超时，IPv6 loopback 已实际验证，因此默认绑定 `::1`。

## 从 SSH 客户端查看

在你自己的电脑建立转发（把服务器地址替换为实际地址）：

```bash
ssh -N -L '8878:[::1]:8878' chenxin@服务器地址
```

然后在本机打开 http://localhost:8878/。VS Code Remote SSH 也可在“端口”面板转发远端 `[::1]:8878`。服务仅监听本机地址。

## 基本操作

1. 新建需求，在中间输入想法，按 Enter 或发送按钮提交；Shift+Enter 换行，中文输入法选词不会误发送。
2. 等待真实 Codex 分析；状态和耗时会显示。需要中止时点击输入框右下角停止按钮。
3. 在问题卡片选择答案或自由填写，再提交回答；选项立即收起为回执，刷新后仍保留。其他问题和对话输入框可以继续使用，重复提交同一待处理回答会被拦截。暂缓后可明确选择“继续回答”。
4. 右侧“需求说明书”按规范阅读本版内容；“收敛路径”按五个主题查看当前决定与真正待决项；“模块方案”查看实际功能承接和系统关系。
5. 点击需求条目直接编辑；点击图中节点引用需求继续讨论；“查看依据”定位原始消息。
6. 使用“变更记录”查看每个版本的修改说明，用“导出”下载 Markdown 或 JSON。
7. 项目、已保存需求和运行状态由服务端保存；未发送输入保存在当前浏览器。刷新后可继续。
8. 分析期间可发送新想法或回答问题；新输入优先替换尚未开始的内部整理，已有运行完成后先处理可见队列中的客户补充。可移除排队内容；停止后队列保留，点击“继续处理”恢复。
9. 未答问题按分支持久保留，卡片默认展示少量问题，可筛选或展开；未提交的选项和文字回答会在刷新后恢复。
10. 查看“需求审阅准备”。产品目标、实际需求、必要模块、流程及待答问题处理后，整体审阅正文并点击“确认本版需求”；后续修改会取消旧版审核状态。未知工程参数保持未知，不要求用户填完247字段。
11. “收敛路径”按主题、关键词和状态筛选。每项显示当前结论、资料状态和关联影响；展开原话与历史值，可跳到说明书、模块或器件资料，或针对该决定继续讨论。
12. “模块方案”提供类别目录、功能契约、端口角色、参数模板及工程卡。选择适用父类或子类，设置用途、功能电路数量、关联需求和设计阶段边界；条件候选保留实施条件，功能电路数量不等于芯片数量。具体器件/引脚尚未确定时保持边界，不凭空填值。
13. 完成后点击“查看本版说明书”，整体检查目标、模块和交接内容，再从正文确认本版需求。需求审核与完整生成准备分别显示；工程与资料缺口随交接保留。
14. 从“器件与证据”或模块卡打开资料，查询真实型号、供应商编号或按规范类别浏览。搜索与预览不会自动选用；必须、优先和参考分别保存，厂商歧义需明确选择身份。
15. 可在输入框下上传参考 ENET 或 BOM CSV；AI 依据真实参考整理适用方案，在模块页采用。参考观测数量、本版数量和仅参考部分分别呈现，原文件及完整 BOM 可下载。外部接口表支持单行修改，提交后收起。
16. “导出 → 选型交接包”检查当前版本，下载说明书、RequirementSpec、证据、待办和公开对话。草稿包明确标记草稿；后续输入适配只验证入口可读，不执行网表生成。

正式入口保留原有项目及真实案例的完整验收过程，侧栏可搜索 `CASE-09` 等编号；也可从配套案例索引直达项目。“查看完整示例”可创建新的独立示例。示例初始内容是固定数据，继续发送会调用真实 Codex。服务启动和打开页面不会自动调用模型。

## 重新安装和构建

```bash
cd frontend
npm ci
npm run build
```

若本机 npm 代理不可达，可只对本次命令覆盖：

```bash
npm ci --proxy=null --https-proxy=null --fetch-retries=0 --fetch-timeout=20000
```

Python 需要 3.11 或更新版本及 `requirements.txt` 中的包。需要完全独立的 Python 环境时，可用已验证的上级 Python 在本目录创建虚拟环境并设置 `STUDIO_PYTHON`：

```bash
../.venv/bin/python -m venv .venv
.venv/bin/pip install -r requirements.txt
STUDIO_PYTHON="$PWD/.venv/bin/python" ./start.sh
```

Codex CLI 需在服务继承的 PATH 中可见，且已完成登录。无需在网页填写 API Key。服务仅接受需求文本，不接受模型、shell 命令或 CLI 工作目录覆盖。

实际模型输出使用服务端生成的原文锚点：Schema限定可选来源，服务端核对消息归属、连续片段与SHA256后回填用户原话。长输入无需模型重复抄写；页面的依据仍显示真实原文。旧会话来源保持兼容。

## 开发

```bash
./start.sh
cd frontend
npm run dev
```

Vite 使用 IPv6 本地 5178 端口，代理 `/api` 到 `[::1]:8878`。生产服务直接提供 `frontend/dist`，用户使用时无需同时运行 Vite。

## 验证

第三、四阶段的当前入口：

```bash
./scripts/phase3.sh doctor
./scripts/phase3.sh build
./scripts/phase3.sh start-real
./scripts/phase3.sh test
./scripts/phase3.sh accept
./scripts/phase3.sh accept-real --cases CASE-03,CASE-04 --rounds 1 --suite my-acceptance
./scripts/phase3.sh report
../.venv/bin/python -X utf8 tests/summarize_phase34.py --require-all
```

开发服务为8894；受控故障服务为8895，由脚本管理。`accept`运行两轮浏览器交互、双标签页竞态、混合负载与真实数据源只读核查，受控provider不会计入真实AI通过数。`accept-real`保留较早的画像驱动方式：调用真实Codex，从登记TXT原文开始，通过UI提交画像中的后续回答；遇到画像未包含的新取舍会保留问题，不伪造批准。这种预设画像测试不替代普通客户逐轮验收。`verify`还要求27案例、固定复测及交接证据全部通过。完整过程与当前进度见[第三四阶段实现与验收](../docs/需求分析工作台_第三四阶段实现与验收记录.md)和[案例索引](../docs/需求分析工作台_第三四阶段案例验收索引.md)。

`phase34_matrix.py`保留较早的更严格批次检查：要求27个案例全部始于同一冻结版本。它不代替本轮客户逐轮验收，也不能把跨版本开发记录算作同版本复测。当前客户验收入口为`tests/customer_journey.mjs`：每次只执行一个真实界面操作，人工先阅读AI实际问题再作答，最终明确审阅、确认、导出；`tests/verify_customer_delivery.py`复核同版页面、原始输入和交接包。`phase3.sh report`分别报告本轮27案例覆盖、当前版本固定案例双轮复测及历史批次，旧版记录继续可见。

单次操作也可通过开发脚本运行，脚本自动管理临时测试浏览器，或复用本轮已有浏览器。把一个明确操作写到JSON文件后执行：

```bash
./scripts/phase3.sh journey --port 8878 --suite my-customer-check --request ./my-request.json
```

例如读取已有过程可写`{"action":"read","key":"my-case"}`；创建过程使用`init`并提供`key`、`title`和真实`text`；逐条补充使用`send`。`answer`按当前问题ID填写`answers`或选择真实`choices`索引。原始TXT验收额外加`--original`，创建时提供`case_id`、`round`和`use_case_txt:true`。每次先读实际返回的问题，再准备下一条请求。`approve`需要人工填写`review_note`且页面已就绪；`export`仅接受已确认版本。此入口不启动模型服务、不批量生成答案，也不绕过真实页面操作与审核条件。

真实验收保留原始输入、每轮用户回答、公开分析进度、需求版本、审核和交接。开发阶段发现问题的旧记录也保留；最终固定复测使用当前后端指纹、前端资源、相同原始案例和独立规则，不把不同版本的结果合并成当前版本重复成功。后续答案来自实际AI问题，允许不同的合理产品选择，不要求相同案例输出同一方案；画像指纹仅保留来源，不用作答案模板。器件意图若与旧画像不同，必须有真实原话及人工审阅依据，原功能和数量义务仍逐项核对。

同一案例的重复测试不要求文档相同：允许合理的追问顺序、表达和模块组织差异，检查引导过程是否合理、补充决定是否被吸收，以及最终说明书是否完整可信。页面与导出的一致性仅核对同一项目的同一版本。

`test`和`verify`流式输出实际测试过程，并自动生成`.runtime/phase3/unit-result.json`，记录真实测试数、结果、后端指纹和日志SHA256；`report`核对同一版本的验收证据。案例首轮分析失败会保留失败状态，不再自动发送后续假定回答或进入旧版本审核。

同一 `suite` 中已经批准的轮次会跳过，方便断点继续；要从原TXT重新进行独立复测，请使用新的 `--suite` 名称。`tests/phase4_real.mjs --mode export`仅从UI重新核对并导出已审核案例，不产生新的模型分析，报告不会将其算作新复测。

每轮分析结束后会自动将上下文与响应Schema无损保存为该任务的 `inputs.zip`，按原路径记录并逐文件校验SHA256；成功、失败和中止后的输入都可保留，归档失败不会否定已保存文档。`answer.json`、`receipt.json`及校验结果保持直接可读。旧任务可运行 `../.venv/bin/python scripts/compact_traces.py`补充归档。停止的受控测试数据与发布前数据库备份另存 `.runtime/backups/*.zip`，均校验压缩内容后再回收展开文件；数据库备份包含 `studio.sqlite3`，含前端的发布备份还包含 `dist/`。恢复前须停止对应服务，先解压到独立目录检查，避免覆盖当前项目。

请在案例驱动停止后运行归档脚本。大型案例快照会另存为逐字一致的 `.json.gz`，验收报告自动读取；内容完全相同的截图保留全部原路径并共享存储，驱动通过原子替换保存新截图。页面中的完整案例、文档版本和公开对话继续从正式数据库读取。

加上 `--archive-screenshots` 可将历史PNG另外保存为 `.runtime/backups/case-screenshots-*.tar.xz`，包含原目录结构和逐图SHA256清单，逐图验证后才删除展开图片。本次历史267张截图已按此方式保留；后续复测新截图仍写入各案例目录。该选项不修改工作台中的案例。

本次体验修复的回归入口（只写隔离的 `.runtime/ux`，不调用真实模型、不修改正式项目）：

```bash
../.venv/bin/python -X utf8 scripts/ux_dev.py check
```

它完成后端回归、独立前端构建、两轮各35项浏览器体验验收，并自动关闭自己启动的8893服务。`unit`、`build`、`accept`可单独运行。最新产品行为与正式UI完整体验案例见[体验修复与真实使用记录](../docs/需求分析工作台_体验修复与真实使用记录.md)。正式UI现保留“体验实测A｜桌面温度显示器”（已审核v10）和“体验实测B｜桌面番茄钟”（已审核v8），可从侧栏查看完整问答、失败重试和整理过程。`tests/ux_verify_live.py`为该次历史发布的只读核验脚本，含旧资产指纹断言；第三四阶段使用`phase3.sh report`和当前案例索引核验，原有文档另与发布前指纹比较。以下旧版验收脚本保留历史用途，其界面选择器及“247字段全部闭合”预期不代表当前体验版。


第二阶段开发与两轮复测入口：

```bash
./scripts/phase2.sh build
./scripts/phase2.sh test
./scripts/phase2.sh accept
./scripts/phase2.sh accept-real
./scripts/phase2.sh report
```

`accept`使用明确标记的受控CLI执行两轮随机UI/故障/负载测试；`accept-real`执行两个真实Codex产品会话，会产生真实调用。二者都自动管理隔离服务，正式8878数据不变。开发服务可用 `serve-real`（8892）或 `serve`（8891）。详见[第二阶段开发与验收](../docs/需求分析工作台_第二阶段开发与验收.md)。

```bash
REQUIREMENTS_STUDIO_KNOWLEDGE=0 REQUIREMENTS_STUDIO_DATA="$PWD/.runtime/unit-app" ../.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
cd frontend
npm run build
```

真实模型和浏览器检查在独立验收服务上执行，避免混入正式文档：

```bash
REQUIREMENTS_STUDIO_DATA="$PWD/.runtime/acceptance" ../.venv/bin/python -m uvicorn backend.app:app --host ::1 --port 8880
```

另一个终端：

```bash
../.venv/bin/python -u tests/real_codex.py
node tests/browser.mjs
```

`real_codex.py` 会产生两轮真实模型调用；完整 `browser.mjs` 会通过 UI 发起一次真实调用和一次取消任务。`node tests/browser.mjs --capture-only` 检查界面与基础操作，不发起模型调用。测试默认使用已安装的 `/usr/bin/chromium-browser`；可用 `STUDIO_CHROME` 指定可执行文件，`STUDIO_TEST_URL` 指定验收服务。

## 文件说明

| 文件 | 职责 |
| --- | --- |
| `backend/app.py` | 独立 HTTP API、SSE、导出、静态页面与本地来源检查 |
| `backend/store.py` | SQLite 项目/消息/版本/任务/事件、并发与恢复 |
| `backend/domain.py` | 需求数据、模型输出契约、来源核对、增量修改、Markdown 渲染 |
| `backend/coverage.py` / `backend/resources/v3_checklist.json` | V3 第 1～5 章 50 小节目录、缺口统计、终稿审核条件 |
| `backend/convergence.py` | 247字段账本、规范模块与功能覆盖、数值一致性、影响复核和上下文无损分组 |
| `backend/module_catalog.py` / `backend/resources/module_catalog.json` | 原Word的10类/60父类/197子类、1260专用参数、82公共参数及完整契约 |
| `backend/experience.py` / `backend/document_export.py` | 产品决定与工程责任分层、实际相关模块范围、项目专属说明书 |
| `backend/codex.py` | 固定模型、需求提示、子进程运行、阶段事件、停止与超时 |
| `backend/model_context.py` | 完整规范的紧凑上下文、逐字引文锚点展开、端口待设计处理与版本指纹 |
| `backend/knowledge.py` / `backend/domain_service.py` | dataS与Milvus受限只读适配、有限异步任务、证据快照与检索进度 |
| `backend/device_domain.py` / `backend/domain_store.py` | 器件意图、关联、规范类别映射、幂等决定、依赖与迟到结果处理 |
| `backend/engineering_checks.py` / `backend/handoff.py` | 原值与量纲核对、证据条件、交接契约、ZIP完整性与真实输入入口适配 |
| `frontend/src/App.tsx` | 项目、对话、问题、文档、编辑、版本和响应布局 |
| `frontend/src/Diagrams.tsx` | SVG 思维导图、功能框架与流程草案 |
| `frontend/src/QuestionCards.tsx` / `frontend/src/RunTimeline.tsx` | 问题提交回执、分支与草稿、实际进度及保存的整理过程 |
| `frontend/src/CoveragePanel.tsx` | 产品审阅准备、按需展开的专业参考和用户审核入口 |
| `frontend/src/ConvergenceBoard.tsx` / `frontend/src/ModuleBoard.tsx` | 收敛路径、字段依据、类别选择、模块编辑、规范详情和关系画布 |
| `frontend/src/types.ts` | 前端契约、API、显示标签与本地草稿 |
| `frontend/src/DeviceEvidencePanel.tsx` / `frontend/src/HandoffDialog.tsx` | 分层资料、身份选择、引用操作、版本联动的交接与输入适配 |
| `frontend/src/styles.css` | 视觉设计、独立滚动、图形样式、窄屏和键盘焦点 |
| `scripts/manage.py` / `start.sh` | 启动、状态、停止本工作台服务 |
| `scripts/probe_codex.py` | 指定模型的最小连通性检查 |
| `tests/test_core.py` | 来源、编号、并发、取消、版本、恢复、导出和接口边界 |
| `tests/test_analysis_ids.py` | 已回答问题编号误入删除列表的回归、真实删除、冲突及逐项目输出约束 |
| `tests/real_codex.py` | 两轮真实 Codex 需求补充验收 |
| `tests/browser.mjs` | 浏览器操作、多尺寸、截图、真实 UI 问答和取消 |
| `tests/test_convergence.py` / `tests/test_runner.py` | 问题待办、队列、竞态、审核、超时与子进程取消 |
| `tests/interaction_audit.mjs` / `tests/connectivity_audit.mjs` | 两轮实际浏览器分支/队列/长文档及断网/丢响应/过期读取验收 |
| `tests/restart_audit.mjs` / `tests/keyboard_audit.mjs` | 两轮实际服务停启、键盘、抽屉和来源定位验收 |
| `tests/load_audit.py` / `tests/provider_fixture.py` | 独立受控 CLI 故障与负载测试；不调用真实模型 |
| `tests/real_convergence.mjs` / `tests/real_audit_profile.py` | 两个虚构产品案例的真实 Codex 多轮收敛、审核和修改复核 |
| `tests/serve_audit.py` / `tests/production_smoke.mjs` | 隔离真实/受控验收服务、正式入口只读检查 |
| `tests/summarize_audit.py` | 检查两轮证据、真实模型收敛和发布资源哈希，生成最终汇总 |
| `scripts/phase2.sh` / `scripts/phase2_dev.py` | 第二阶段构建、开发、两轮核心/随机/真实验收及报告入口 |
| `scripts/build_phase2_catalog.py` | 从原Word可重复提取权威目录，核对数量与来源哈希 |
| `tests/test_phase2.py` / `tests/test_phase2_runner.py` | 字段/类别/来源/数值/影响规则与一次有界修复、停止和反馈 |
| `tests/phase2_browser.mjs` / `tests/phase2_load.py` / `tests/phase2_real.mjs` | 两轮随机交互和负载、两个真实模型产品会话 |
| `scripts/phase3.sh` / `scripts/phase3_dev.py` | 第三四阶段开发、服务、浏览器生命周期、复测与验收入口 |
| `tests/phase3_browser.mjs` / `phase3_races.mjs` / `phase3_load.py` / `phase3_sources.py` | 两轮交互、版本竞态、负载与真实数据源核查 |
| `tests/phase4_real.mjs` / `phase4_profiles.json` / `phase4_oracles.json` / `check_case.py` | 原TXT输入、可审查的模拟用户决定、独立语义检查与真实UI验收 |
| `tests/summarize_phase34.py` / `phase34_deploy.py` | 重新核对保留证据、生成索引，以及停机备份后追加真实测试项目 |
| `scripts/compact_traces.py` / `phase34_backup.py` / `publish_phase34.py` | 已完成输入的无损归档、经完整性核对的压缩备份与保留原项目的发布 |
| `backend/history_codec.py` / `scripts/compact_history.py` | 新历史版本无损压缩，兼容旧JSON；停机备份后迁移，逐版本内容哈希与库完整性核对 |
| `backend/trace_archive.py` | 结束任务的输入自动归档，原子写入与SHA校验，失败保留原文件；不改变回答、调用收据和需求版本 |
| `backend/storage_budget.py` | 在真实推理前预留保存空间，返回后释放；预留失败不启动模型 |

## 当前边界

新协议使用字段级核对和规范模块承接，不能仅用50个章节总结自报完成。产品用途、场景和核心功能在需求阶段明确。明确委托才标为设计阶段决定，未知实现参数保留待设计核对，不要求用户逐项授权。规范未覆盖的类别保留映射缺口，不随意新增或放入“其他”兜底。

结构或引用不合格的模型输出最多自动复核一次。明确的暂时网络中断、服务超时或限流最多自动重连一次；重连与结构复核共享本轮600秒预算，首个失败收据另存留证。登录、配额、上下文、调用格式、本地存储和越界工具错误不自动重试。失败保留原版本与用户输入，错误收据脱敏；手动重试带上具体校验反馈。

每次真实CLI启动前，会在任务所在文件系统预留8MiB保存空间；模型返回后释放，用于保存回答收据和文档。预留失败时不启动新的模型调用。预留文件立即取消目录链接，进程退出会自动释放；逐次收据记录实际分配字节。该保护降低推理期间配额变化导致的保存失败风险，不能替代文件系统可用性。已返回且校验可恢复的回答可从界面重试恢复，不重复调用该轮模型；后续独立分析仍单独留证。

历史版本在库内无损压缩，当前文档和公开接口仍为原JSON。需要整理旧历史时先停止主、开发服务，再运行 `../.venv/bin/python scripts/compact_history.py --target both --apply`；脚本先备份，再核对每版完整文本指纹、外键与库完整性。内部备份ZIP使用LZMA，Python标准库 `zipfile` 可读取；面向用户的六文件交接ZIP保持常规格式。

文档从草稿开始，参考V3第1～5章，但按当前项目编写。核心用途、场景、功能与可验收指标，以及有实际依据的其他产品要求，必须明确；实际模块承接、流程、冲突、未决需求与待答问题也必须处理。实现模板中的未知数值可以继续保持待设计，不单独阻止需求审阅。明确不适用及已委托设计阶段的结论仍需用户原话依据。每轮最多新增 3 个问题，不限制总轮次或累计问题数。第 6 章实际器件选型仍在需求审核后由后续流程补充。

当前提供真实器件资料核查和有条件的必要比较；资料未知仍是未知，需求审核不等同电路、热设计或认证验证。完整工作台JSON为 `requirements-studio/2`，旧版 `/1` 保留兼容投影；正式交接采用 `pcbagent.requirement-spec/1.0`。适配器已使用现有流程输入入口，偏好、参考、系列与开放选型项不会被自动选成最终BOM。

Codex exec 返回结构化最终结果，页面实时展示读取资料、核对重点、实际CLI阶段、经过时间和公开简短说明；整理过程会保存，可在对应用户消息下展开。模型完整回复与文档在本轮校验完成后出现，不显示私有推理或虚构思考。来源核对证明引用来自历史用户消息，尚不能证明每一句需求都完成语义验证；建议与未知项会保留状态。

问题使用增量待办：本轮新增/细化问题与未答旧问题合并，`resolved_question_ids` 归档真正已获答案的问题；遗留的纯命名/分类或不相关工程问题可说明转入内部整理，未知参数仍未知。点击提交只表示回答与问题关联，不能把“不知道”等内容自动当作已解决；明确暂缓的问题保留。`remove_ids` 只用于取消已有需求，每轮 CLI 输出约束仅允许当前项目需求编号。旧返回误把已回答问题放入删除列表时按兼容规则处理，未知编号及同时修改/删除继续拦截。

会话没有等待答案的自动终止时间。若模型识别出可独立推进的任务，服务在用户补充队列为空时继续分析；后续仅在仍有内部复核或一致性事项时继续，同一条用户输入最多3次，重复任务不再循环。真正待用户决定的问题保留，资料未知与普通工程待设计项不触发反复生成。新补充优先于下一步内部分析；暂停会停止自动续分析，并保留未答事项。

文档保存后，任务保持处理中，直到资料排程和本轮收尾完成；完成状态与下一条用户输入或内部任务在同一事务中交接。画布可先显示已保存的新版本，避免收尾期间短暂显示空闲、引发重复催促或额外分析。

说明书中的日期按浏览器本地时区显示到分钟，可查看原始时间提示；结构化数据和导出原文保留完整时间，正文内容不因显示格式而改写。

关联需求修改后，系统会安排必要的章节一致性复核；不需要用户重复已提供的资料。仅来源刷新不重新打开内容相同的章节。复核尚未完成时保留待核对状态，确实缺少新决定时才继续提问。

版本记录支持查看修改摘要；完整历史版本恢复、多人协作、公网认证与完整文档格式导出留待后续阶段。

## 配套文档

- [调研与目标总览](../docs/需求分析工作台_调研与目标总览.md)
- [体验修复与真实使用记录](../docs/需求分析工作台_体验修复与真实使用记录.md)
- [第一阶段实现方案](../docs/需求分析工作台_第一阶段实现方案.md)
- [使用与验收](../docs/需求分析工作台_使用与验收.md)
- [压力测试与完整收敛验收](../docs/需求分析工作台_压力测试与收敛验收.md)
- [第二阶段调研与产品方案](../docs/需求分析工作台_第二阶段调研与产品方案.md)
- [第二阶段开发与验收](../docs/需求分析工作台_第二阶段开发与验收.md)
- [第三阶段现状与数据源调研（开发前核查）](../docs/需求分析工作台_第三阶段现状与数据源调研.md)
- [第三阶段领域能力补齐实施方案](../docs/需求分析工作台_第三阶段领域能力补齐实施方案.md)
- [第三四阶段实现与验收记录](../docs/需求分析工作台_第三四阶段实现与验收记录.md)
- [第三四阶段案例验收索引](../docs/需求分析工作台_第三四阶段案例验收索引.md)
- [第二阶段样例1：室内桌面温度显示器](../docs/需求分析第二阶段_样例1_已审核.md)
- [第二阶段样例2：工作台照度显示器](../docs/需求分析第二阶段_样例2_已审核.md)
- [已审核的完整需求样例 1](../docs/需求分析验收样例1_已审核.md)
- [已审核的完整需求样例 2](../docs/需求分析验收样例2_已审核.md)

## 增量合并与保留记录核验

`tests/phase34_deploy.py --apply`仅将开发入口的真实项目增量并入正式入口，不覆盖已有ID。合并前须确认两服务没有活动分析或器件作业并停止服务。`scripts/audit_phase34_merge.py prepare`会检查11张表的主键冲突、完整性和运行目录冲突，保存逐行指纹；合并后运行`check`核对原记录、导入记录、保留队列及运行凭据。两个命令都不恢复历史队列，审计脚本不修改数据库。审计只接受一份明确的合并前快照，拒绝覆盖既有审计记录。

本轮交付状态见[客户视角验收与交付报告](../docs/需求分析工作台_客户视角验收与交付报告_2026-10-17.md)。27份最终文档与6份固定复测交接均已完成；固定电机第二轮保留一次存储失败和真实重试，因此原严格无故障门槛仍为false，`summarize_phase34.py --require-all`实际返回1。报告区分最终文档通过和全程无故障，不删除失败或放宽门槛。
