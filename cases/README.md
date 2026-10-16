# 器件选型测试案例库

本目录由 `tools/build_cases.py` 从历史选型产物自动生成，用来固定回归口径：
每次改动选型逻辑后跑同一批案例，对比结论有没有变。

## 来源

`workspace/web/output/case*/console/stages/03_device_selection__*.json`
——真实跑出来的选型结果，每个模块带参数（含重要度）和当时选中的器件。

## 转换口径

| 历史内容 | 转成什么 |
| --- | --- |
| `importance = critical` 的参数 | 硬性条件（违反即淘汰） |
| `importance = important` 的参数 | 偏好条件 |
| `importance = optional` 的参数 | 不进条件，记进 `meta.json` |
| `confirmed_device` | 选型项的指定器件（意图记为「仅作参考」） |
| 当时选中的器件 | `expected.json` 的对照基线 |
| 参数键对不上数据库字段 | 不硬塞，记进 `meta.json` 的 `unmapped_parameters` |

字段名先归一化再对照数据库真实字段；类别 `leaf_code` 优先取当时确认器件在库里的真实归属。

## 规模

- 案例 **31** 个，模块 **527** 个
- 硬性条件 **1347** 条，偏好条件 **613** 条
- 未能映射的参数 **2368** 条（保留在 meta 里待人工确认）

## 被跳过的素材

历史产物里状态为失败的运行没有可转换的模块，不能灌进案例库污染基线，
原因记录在 `index.json` 的 `skipped_sources`：

- `case10/console/stages/03_device_selection__7c07c31c.json`：历史产物状态 'error'，错误 "AgentStepError: agent role 'parameter_estimator' failed at step 'parameter_estimation.module_8' with status 'output_inva"，无模块
- `case24/console/stages/03_device_selection__dc578995.json`：历史产物状态 'error'，错误 "AgentStepError: agent role 'parameter_estimator' failed at step 'parameter_estimation.module_16' with status 'output_inv"，无模块
- `case4_re/console/stages/03_device_selection__c2944e43.json`：历史产物状态 'error'，错误 "AgentStepError: agent role 'parameter_estimator' failed at step 'parameter_estimation.module_13' with status 'output_inv"，无模块

## 怎么用

```bash
# 跑单个案例
python3 cli.py run --spec cases/case17/requirement_spec.json --out output/case17.json

# 批量回归（对比结论与历史基线的差异）
python3 tools/run_cases.py
```

## 逐案例

| 案例 | 模块 | 硬性条件 | 偏好条件 | 未映射参数 | 类别未解析 | 有历史选择 |
| --- | --- | --- | --- | --- | --- | --- |
| [case11](case11/requirement_spec.json) | 28 | 66 | 27 | 139 | 1 | 17 |
| [case4](case4/requirement_spec.json) | 26 | 56 | 28 | 141 | 2 | 7 |
| [case17](case17/requirement_spec.json) | 25 | 66 | 31 | 103 | 2 | 14 |
| [case2](case2/requirement_spec.json) | 25 | 65 | 25 | 123 | 3 | 4 |
| [case24_re](case24-re/requirement_spec.json) | 23 | 60 | 28 | 95 | 1 | 13 |
| [case9](case9/requirement_spec.json) | 23 | 58 | 25 | 106 | 2 | 9 |
| [case2_re](case2-re/requirement_spec.json) | 21 | 49 | 30 | 90 | 2 | 4 |
| [case1](case1/requirement_spec.json) | 20 | 50 | 27 | 98 | 6 | 4 |
| [case22](case22/requirement_spec.json) | 20 | 42 | 23 | 86 | 3 | 6 |
| [case34](case34/requirement_spec.json) | 20 | 58 | 23 | 83 | 3 | 10 |
| [case18](case18/requirement_spec.json) | 18 | 46 | 21 | 98 | 2 | 8 |
| [case26](case26/requirement_spec.json) | 18 | 33 | 15 | 84 | 2 | 8 |
| [case33_re](case33-re/requirement_spec.json) | 18 | 40 | 17 | 87 | 0 | 7 |
| [case16](case16/requirement_spec.json) | 17 | 44 | 22 | 69 | 5 | 9 |
| [case21](case21/requirement_spec.json) | 17 | 48 | 25 | 65 | 0 | 8 |
| [case8](case8/requirement_spec.json) | 17 | 49 | 22 | 70 | 1 | 11 |
| [case29](case29/requirement_spec.json) | 16 | 41 | 26 | 66 | 2 | 8 |
| [case14](case14/requirement_spec.json) | 15 | 33 | 12 | 78 | 0 | 10 |
| [case19](case19/requirement_spec.json) | 15 | 52 | 21 | 55 | 0 | 11 |
| [case23](case23/requirement_spec.json) | 15 | 38 | 15 | 64 | 1 | 3 |
| [case12](case12/requirement_spec.json) | 14 | 34 | 22 | 54 | 0 | 3 |
| [case7](case7/requirement_spec.json) | 14 | 36 | 9 | 68 | 2 | 5 |
| [case10_re](case10-re/requirement_spec.json) | 13 | 39 | 11 | 54 | 0 | 6 |
| [case13](case13/requirement_spec.json) | 13 | 34 | 16 | 55 | 1 | 8 |
| [case15](case15/requirement_spec.json) | 13 | 34 | 18 | 61 | 2 | 6 |
| [case3](case3/requirement_spec.json) | 13 | 36 | 18 | 59 | 0 | 5 |
| [case27](case27/requirement_spec.json) | 12 | 31 | 11 | 51 | 0 | 3 |
| [case30](case30/requirement_spec.json) | 11 | 27 | 12 | 53 | 1 | 3 |
| [case6](case6/requirement_spec.json) | 10 | 31 | 12 | 45 | 0 | 2 |
| [case31](case31/requirement_spec.json) | 9 | 29 | 12 | 36 | 1 | 3 |
| [case20](case20/requirement_spec.json) | 8 | 22 | 9 | 32 | 1 | 5 |

## 说明

- `expected.json` 只是历史运行的对照基线，**不是唯一正确答案**；
  当时有相当一部分模块没有选出器件，那些空结果本身就是需要改进的证据。
- `meta.json` 里的 `unmapped_parameters` 是下一步可以补映射的地方，
  补一条映射，案例覆盖就多一块。
