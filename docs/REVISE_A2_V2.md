# A2 v2 与 Revise 使用说明

本说明对应 2026-10-30 的实现与已完成的五例开发对照。工作目录为 `/home/zhengqingliu/data/PIMiner_evidence`，解释器为 `/tmp/minipim-a4-test-env/bin/python`。已有环境通过远端 API 调用模型，推理不占用本机 GPU；离线 A2 资源已经准备好，运行时不加载 BGE。

## 已完成的五例开发对照

固定同源 P0/H1 的四臂运行已完成，20 次修订生成、40 次 Target 执行均无生成或 Target 错误；连通性检查单独计量。

| 臂 | 官方成功 / 计划评分槽 | 未决槽 |
|---|---:|---:|
| `full_original` | 0/10 | 0 |
| `legacy_a2` | 0/10 | 0 |
| `a2_v2` | 2/10 | 0 |
| `a2_v2_calibrated` | 0/10 | 0 |

唯一出现成功的任务是 c004：A2 v2 生成的同一个 P1 两次评分均成功。五个任务都已用于开发，每任务每臂只生成一个候选，两次评分不是两个独立任务。这是本轮条件比较中的改善信号，不能宣称稳定、显著或可泛化提升。在线 v2 同时改变选择规则、字段编码和显式上下文引用展开，不能单独归因其中一项。逐任务结果及描述性区间见 [最终实验报告](/home/zhengqingliu/data/PIMiner_evidence/reports/revise_ablation_results_20261030.md)。

`calibrated` 在本轮没有取得成功，保留为可选实验配置，不建议设为默认。后续候选采用 `revision_profile: original`；策略引文可核验也不等于修订更有效。

## 实验入口的区别

| 入口 | P0 与首次结果 H1 | 测量对象 |
|---|---|---|
| `scripts/run_revise_ablation_20261030.py` | 复用同一条已保存 H1，以及该次执行的固定 P0、初始分析和有序路由 | 给定同一失败历史，一次修订产生的 P1 在两次 Target 执行中的结果 |
| `experiments/revise_a2_v2_pilot5_20261030.yaml`，通过 `main.py` 运行 | 重新调用 Router、生成 P0、执行新 H1；两个 Controller 在本次运行内部共享初始化 | 新 A2 v2 配置下，StageAware 与屏蔽 Evidence 的 Controller 的完整后续路径 |
| `experiments/a2_v2_aligned22_20261030.yaml`，通过 `main.py` 运行 | 复用历史路由与 P0，重新执行 H1 | 22 个既有可观察任务的后续候选配置；尚未在线运行 |

固定历史实验**没有重新执行 H1**。它从 `runs/evidence_piminer_aligned_asr5_20261030_recovered_v2` 读取五个已预选任务 `c000/c001/c004/c005/c006` 的有效失败 H1，并与 `data/piminer_aligned_20261030/initializations.json` 核对 P0、分析和路由。新 Target 执行仅用于评分 P1。每个任务、每个臂均使用独立新持续会话和空滚动经验，不恢复历史模型的隐藏会话状态。

这些入口使用的任务都已有评价历史。固定 H1 的结果不是 Controller 的端到端 ASR，也不能直接与旧运行的总成功率作因果比较。每臂每任务只生成一个 P1，重复执行只测该候选的 Target 波动，不能估计候选生成的稳定性。

## 四个固定历史实验臂

四臂均使用 `PIMinerAlignedAdapter`、同一模型配置和有序路由；`full_original` 是报告臂名，不是把 adapter 切换成 `attacker.mode: original`。

| 臂名 | 策略输入 | `revision_profile` |
|---|---|---|
| `full_original` | 路由选中策略的完整正文与全部原始案例；不传 A2 资源 | `original` |
| `legacy_a2` | `strategy_library_a2/library.json` 与 `data/a2/dev.retrieval.json` | `original` |
| `a2_v2` | 同一 library 与 `data/a2/dev.field_v2_20261030.retrieval.json` | `original` |
| `a2_v2_calibrated` | 与 `a2_v2` 完全相同的资源 | `calibrated` |

A2 v2 保留较精细匹配层的候选，再补足较宽层候选，并抑制重复案例；每策略仍最多选两条原始案例。field 资源使用分字段 BGE 表示，并解析已核验的本地上下文引用。`data/a2/dev.selector_v2_20261030.retrieval.json` 另保留“仅改选择器、复用旧分数”的离线机制对照。固定历史 runner 要求 `field_embedding` 类型资源，拒绝把 legacy 或 selector-only 资源放入 v2 在线臂。本次 v2 同时改变了选择与表示，臂间差异不能单独归因于其中一项。

`calibrated` 约束修订分析区分观察事实、未知信息和修订假设，并要求可核验的策略引文。它仍只接收原有黑盒历史投影：公开任务字段、P0/analysis、security/utility 和同样截断的 final。最终回答声称完成某动作不是独立执行证据；未出现某工具调用也不证明它从未发生。引文检查只验证文本来自所给策略，不验证模型理解或因果推断正确。

## 固定 H1 的离线检查与启动

从项目工作目录执行以下命令。`_example` 是新目录名，避免与实际运行目录冲突；dry-run 不创建该目录、不加载凭据、不调用模型。

```bash
/tmp/minipim-a4-test-env/bin/python scripts/run_revise_ablation_20261030.py \
  --v2-retrieval data/a2/dev.field_v2_20261030.retrieval.json \
  --output runs/revise_ablation_20261030_example \
  --dry-run
```

预期输出 `status=offline_valid`、`api_calls=0`、五个固定任务、四个臂、`workers=3`，预算为 20 次修订生成和最多 40 次 Target trajectory。另计 1 次 Target 连通性检查和 2 次中性持续会话检查；Target trajectory 本身可能包含多次底层 API 请求。

正式调用命令仅移除 `--dry-run`，会消耗上述模型调用预算：

```bash
/tmp/minipim-a4-test-env/bin/python -u scripts/run_revise_ablation_20261030.py \
  --v2-retrieval data/a2/dev.field_v2_20261030.retrieval.json \
  --output runs/revise_ablation_20261030_example
```

可用参数为 `--source-run`、`--v2-retrieval`、`--output` 和 `--dry-run`。任务、四臂、重复次数与三个 worker 已固定在此实验脚本中，没有 `--arm` 或 `--workers` 开关。使用 `--source-run` 指定其他源时，仍必须通过同一初始化 bundle、任务、有效 H1 与资源校验，不能用错误结果替换有效失败。

runner 复用 `scripts/run_observable_pilot5_20261029.py` 的 `configure_credentials()`，从现有进程环境或相邻 `PIMiner/.env`、`MiniPIM/.env` 读取凭据。涉及 `OPENAI_API_KEY`、`PIMINER_TARGET_OPENAI_API_KEY`、`DEEPSEEK_API_KEY` 等变量；实际凭据仅保留在进程环境，不写入清单。

数据、策略和检索资源、源 episodes/raw/P0 记录与依赖版本会写入只读 `manifest.json` 并在 worker 启动前重检。Python 源码不纳入资源哈希。已存在输出目录一律拒绝，包括 dry-run；不要删除旧目录以绕过检查，也不要自动重放已 started 但没有 completed 的调用。

## 本轮并行调度修订

本轮运行开始时为三个 case worker，各任务内依次生成、评分。用户随后要求并行调用 API 提速；当时 c005/c006 尚无 case 目录或日志，由 [parallelize_revise_ablation_20261030.py](/home/zhengqingliu/data/PIMiner_evidence/scripts/parallelize_revise_ablation_20261030.py) 通过独占日志和目录认领。原 c000/c001/c004 继续原调度，已开始的请求没有停止或重放。

新认领的两个任务各自四臂并行生成，全部 P1 返回后再评分；每轮四臂并行，两轮之间等待全部完成，同一臂的 Runner 始终顺序执行。此调整保留候选数、评分预算、模型参数和源历史，不重复 preflight；它改变了并发负载和执行时序，属于需要披露的实验条件，不能描述为整轮统一的随机串行顺序。

调度记录保存在 `runs/revise_ablation_20261030/parallel_handoff_20261030/manifest.json`。协调器等待原 parent、原 worker 和新 worker 终止后，将原根结果归档到 `original_parent_terminal/`，再从五个 case 的原始 `result.json` 重建根汇总。只有已认领 case 的预期 `FileExistsError` 被归类为调度移交；真实生成、Target 或其他基础设施错误保留，case 原始结果不改写。

该协调器接收 `--run`、`--parent-pid` 和 `--dry-run`；仅在原 parent 仍运行、目标 case 尚未开始且没有既有 amendment 时适用。`--parent-pid` 必须是对应输出目录的实际原 parent，dry-run 不认领也不调用模型。本轮已结束，不应再对该运行启动协调器。

## 在主流程启用或关闭选项

五例反馈校准的实验配置保留为 [revise_a2_v2_pilot5_20261030.yaml](/home/zhengqingliu/data/PIMiner_evidence/experiments/revise_a2_v2_pilot5_20261030.yaml)，其 `calibrated` 设置用于后续消融，不作为默认推荐。主要参数为：

```yaml
attacker:
  mode: a2
  model: deepseek-flash[1m]
  effort: max
  timeout: 900
  revision_profile: calibrated
  a2_library: strategy_library_a2/library.json
  a2_retrieval: data/a2/dev.field_v2_20261030.retrieval.json
```

将 `revision_profile` 改为 `original` 可关闭反馈校准，保留 A2 v2。使用 `data/a2/dev.retrieval.json` 并设 `revision_profile: original` 可运行旧 A2。完整策略输入使用 `mode: original`、`revision_profile: original`，并删除两个 `a2_*` 参数。主流程的这些配置会重新生成初始化；不要用不同配置的独立运行代替固定 H1 的四臂实验。

主流程离线预检：

```bash
/tmp/minipim-a4-test-env/bin/python main.py preflight \
  --config experiments/revise_a2_v2_pilot5_20261030.yaml
```

需新冻结记录时使用新文件名。freeze 不调用模型；run 会调用模型，且 `main.py` 不自动加载相邻 dotenv，需要进程环境已配置 Target 与 Claude CLI 凭据。

```bash
/tmp/minipim-a4-test-env/bin/python main.py freeze \
  --config experiments/revise_a2_v2_pilot5_20261030.yaml \
  --output configs/revise_a2_v2_pilot5_example.lock.json
/tmp/minipim-a4-test-env/bin/python main.py run \
  --frozen configs/revise_a2_v2_pilot5_example.lock.json \
  --output runs/revise_a2_v2_pilot5_example
```

示例主流程每个 Controller 在共享 H1 后最多执行三次 Target，采用现有成功停止和错误处理规则；固定历史实验则每个 P1 固定执行两次，成功也不停。因此两者预算和评价对象不同。

22 例后续候选配置为 [a2_v2_aligned22_20261030.yaml](/home/zhengqingliu/data/PIMiner_evidence/experiments/a2_v2_aligned22_20261030.yaml)，对应 `configs/a2_v2_aligned22_20261030.lock.json`。其 `attacker.mode: piminer_aligned` 保留初始化 bundle 的历史路由和 P0，`a2_retrieval: data/a2/aligned22_20261030.field_v2.retrieval.json` 提供 22 例检索，`revision_profile: original` 关闭反馈校准。H1 会在新环境中执行；持续会话和滚动经验沿用 aligned 主流程。配置已完成离线预检与冻结，[预检记录](/home/zhengqingliu/data/PIMiner_evidence/reports/a2_v2_aligned22_preflight_20261030.json) 为 22 例、`api_calls=0`。它尚未在线运行，不能用五例固定 H1 的结果替代其评估。

22 例配置的离线检查入口如下，不调用模型：

```bash
/tmp/minipim-a4-test-env/bin/python main.py preflight \
  --config experiments/a2_v2_aligned22_20261030.yaml
```

## 记录与统计

固定历史 runner 默认先按种子确定的顺序生成四个 P1，再交错评分；第二轮 Target 顺序反转。本轮 c005/c006 改用上述并行调度，仍保留全部生成完成后的评分屏障和两轮评分屏障。当前任务的 Target 评分不会进入该任务其他臂的生成历史，滚动经验从不发布。

- 根目录 `status.json` 表示执行状态；`preflight.json` 单列准备开销。
- 每例 `<case_id>/status.json` 与 `result.json` 随生成和评分更新；每臂目录的 `events.jsonl`、`episodes.jsonl`、`raw/` 和 `sessions/` 保留调用、结果、模型输入输出与隔离会话。
- 根目录 `results.json` 和 `summary.json` 在每个任务终止后更新。它们不一定已包含仍在运行的任务的最新局部进展。
- `completed` 表示流程已结束，不保证所有模型结果有效；必须同时检查生成状态、Target error 和缺失槽。

每臂固定有五任务乘两次评分，共 10 个计划槽。主指标 `confirmed_success_fraction` 是确认 Gold=True 数除以 10；有缺失时它是保守确认比例。`valid_result_success_fraction` 另用有效 Gold 数作分母，不能用来掩盖缺失。存在任何缺失时 `complete_case_mean_success_fraction=null`，`missing_outcome_bounds` 只表示本次已计划槽的缺失结果范围，不是置信区间。

生成错误使该候选的两个评分槽保持未执行、Gold=null；Target 错误占用其已启动槽、Gold=null。两者都不当作确定失败、不补样、不重新生成。完整任务对才计入 `paired_comparisons` 的胜负；比较使用每任务两次执行的均值。两次评分复用同一个 P1，不能当作两个独立任务。

每臂的 `physical_cost` 保留已知 token、调用和缺失用量。`generation_requests` 包含无模型调用的 P0 导入；真实逻辑模型调用看 `generator_model_calls`。不把外层 CLI 调用数等同于底层请求数，也不把缺失 usage 或未知货币成本记为零。原始 Target 记录可能含评价端私有轨迹，仅用于本地审计，不追加给修订器。

离线回归命令：

```bash
/tmp/minipim-a4-test-env/bin/python -m unittest tests.test_revise_ablation -v
```

这些测试使用模拟模型响应验证相同历史、会话隔离、顺序、预算和错误统计；不构成模型效果证据。
