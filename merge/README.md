# 需要并入需求分析工作台的部分

工作台（`requirements_studio`）是 chenxin 的代码。我们**只改了两个文件**，
都在前端：

| 文件 | 性质 | 说明 |
| --- | --- | --- |
| `requirements_studio/frontend/src/App.tsx` | 修改 | 顶栏「导出」右边加一个「器件选型 ▾」**下拉菜单** |
| `requirements_studio/frontend/crypto-shim.cjs` | 新增 | 仅 Node < 18 构建时需要，见下 |

**下拉里两个选项：**

| 选项 | 行为 |
| --- | --- |
| 打开选型界面 | 新开标签到 `http://<主机名>:8899/?project=<项目编号>`，自动载入该项目（不自动跑选型） |
| 自动选型并导出下游文件 | **不跳转**，直接在后台把该项目跑完选型、全部确认、导出 `pcbagent.netlist_input.v1` 并下载 |

「自动选型」会把**每个有候选的选型项都选上**，不留空；结论不完美的项会标
`needs_review`（如"硬性条件未知""未核对"），在选型界面上显示为「⚠ 需复核」。
没有候选的项无法凭空生成，会列在返回的 `not_exported` 里。

---

## 两种合并方式，任选一种

### 方式一：直接覆盖文件（最省事）

`files/` 目录已经按仓库里的原始路径摆好，直接拷进去覆盖即可：

```bash
cp -r merge/files/requirements_studio <PCBAgent仓库根>/
cd <PCBAgent仓库根>/requirements_studio/frontend
npm run build
```

### 方式二：打补丁（便于 review）

```bash
cd <PCBAgent仓库根>
git apply /path/to/merge/chenxin-workbench-jump-button.patch
cd requirements_studio/frontend && npm run build
```

补丁是拿**仓库里当前的 `App.tsx`** 做基准生成的，不掺杂我副本里的其他差异。

---

## 关于 `crypto-shim.cjs`

Node 16 的 `crypto` 模块没有 `getRandomValues`，而 Vite 6 会直接调用它，
导致构建报 `crypto$2.getRandomValues is not a function`。

**如果构建机 Node ≥ 18，不需要这个文件，`npm run build` 直接能过。**
确实需要时这样构建：

```bash
node --require ./crypto-shim.cjs ./node_modules/vite/bin/vite.js build
```

---

## 注意

- 他们的 `.gitignore` 里排除了 `frontend/dist/`，所以**构建产物不入库**，
  部署机上要跑一次 `npm run build`。
- 器件选型阶段的网页端默认跑在 `:8899`，目前是**硬编码**在 `App.tsx` 里的。
  如果要换端口或多机部署，需要改成配置项——这一点待确认。
