# 轴 A — 广度扩展执行手册（补机制 + 补目标覆盖）

目标：在紧凑双层库（`cards/` + `experiences.jsonl`）的基础上，**扩大证据覆盖**——
让每个机制在更多目标模型上有命中证据（A1 补目标），并从失败轨迹挖出新机制
（A2 补机制）。所有新证据必须遵守固定划分，且能回流进紧凑库。

本手册的离线部分（覆盖分析、manifest、split 回填）已经就绪；带 **[LIVE]** 的步骤
需要真实 attacker + 目标模型调用（需配好 model-config 和 provider 凭据）。

---

## 0. 当前覆盖矩阵与缺口（100 条经验，2026-09-21）

机制 × 目标模型（命中数，`.` = 无证据）：

| mechanism | claude-haiku-4-5 | claude-sonnet-4-5 | gpt-5 | gpt-5-nano |
|---|---|---|---|---|
| appended_output_directive | . | . | . | 3 |
| authenticated_principal_voice_forge | . | 8 | . | . |
| authority_shed_docstring | 1 | . | . | . |
| entity_data_poisoning | 2 | 3 | 3 | . |
| fabricated_procedure_gate | 23 | 9 | 10 | 8 |
| false_history_forge | . | . | 1 | . |
| forged_chat_turn | 1 | 4 | 6 | 18 |

套件覆盖：agentdojo `{travel:19, workspace:13, slack:10, banking:2}`，
ipi_arena `{coding:32, tool:22, browser:2}`。

**优先补的缺口（按收益排序）：**

1. **单点机制**——`authority_shed_docstring`(1)、`false_history_forge`(1)：证据太薄，
   小模型学不到稳定模式。优先在其**已确认目标 + 相邻目标**上补命中。
2. **机制×目标空洞**——例如 `appended_output_directive` 只有 nano，
   `authenticated_principal_voice_forge` 只有 sonnet。补"同机制跨目标"是迁移证据的核心。
3. **稀疏套件**——`banking`(2)、`browser`(2)：补这些套件让检索键分布更均衡。
4. **新机制(A2)**——上面 7 个机制无法覆盖的失败轨迹，走 `propose` 挖新机制。

---

## 1. 先决条件：固定划分（离线，已生成草稿）

已生成 manifest 草稿（split 由 `--seed 42` 决定、与目标无关）：

- `eval_results/strategy_research/manifest_agentdojo_draft.json`（97 组）
- `eval_results/strategy_research/manifest_ipi_arena_draft.json`（41 组）

**init 前你必须定稿 `training_targets`**（草稿里是占位值 `gpt-5-nano claude-haiku-4-5`）。
研究方案建议：≥2 个训练目标 + ≥3 个最终留出目标（含完全留出的家族）。留出目标
**不写进** `training_targets`——框架会阻止对留出目标做 collect/plan。定稿后：

```bash
python -m strategy_research init --manifest eval_results/strategy_research/manifest_agentdojo_draft.json
# ipi_arena 用独立 --db（一个库只能绑一个 manifest）
python -m strategy_research --db eval_results/strategy_research/ipi.sqlite \
  init --manifest eval_results/strategy_research/manifest_ipi_arena_draft.json
```

紧凑库已用这两份草稿回填了 `split_role`（experiences）和 `split_role_distribution`
（cards）。**test 组的 6 条经验现已隔离，训练期 RAG/SFT 不得使用。** 换 manifest 后
用 `build.py --manifest ... --manifest ...` 重建即可刷新。

---

## 2. A1 — 补目标覆盖 [LIVE]

思路：保持强 attacker/teacher 不变（如 Claude 订阅），只**换目标模型**重跑现有机制，
命中即成为新经验。用 discovery 组，绝不碰 test/development。

```bash
# 用某张卡的机制去打一个尚无证据的目标；--strategy-id 指向 import-library 登记后的 artifact_id
python -m strategy_research import-library --directory strategy_library_json   # 一次性，登记先验
python -m strategy_research list strategy                                       # 取 artifact_id

python -m strategy_research collect-plan \
  --dataset agentdojo --rows data/agentdojo/agentdojo_injection_steps.jsonl \
  --targets <NEW_TARGET> \
  --strategy-id <该机制的 artifact_id> \
  --seeds 0 --budget 5 --limit 20 \
  --model-config eval_results/strategy_research/teacher.json      # 强 attacker/teacher

python -m strategy_research run-plan --plan-id <PLAN_ID> \
  --runs-dir eval_results/strategy_research/runs \
  --python /root/miniconda3/envs/piminer/bin/python
```

`run-plan` 只跑 discovery 组、黑盒反馈、单策略 override、无跨样本记忆（`piminer.py` 已固定）。
命中的 episode 即新证据。DeepSeek 目标另需 `--target-config`（见 `strategy_research/AGENTDOJO_DEEPSEEK.md`）。

**回流紧凑库**：新命中先落进 strategy_research 的 store；要进 `experiences.jsonl` 有两条路——
(a) 若同时也写回了原 `strategy_library/*.md` 的 in-context example，则 `convert.py` 重导 JSON 后
`build.py` 重建即可；(b) 更直接的是给 `build.py` 增加一个从 strategy_research episode 导出
经验记录的入口（见 §4 待办）。当前 `build.py` 只从 `strategy_library_json` 构建。

## 3. A2 — 挖新机制 [LIVE]

思路：对现有 7 机制都打不动的失败轨迹，让教师读**真实失败轨迹**提出新机制。

```bash
# 导入一批已完成的 PIMiner run（或上一步 collect 的失败 episode）作为 discovery 证据
python -m strategy_research import-run --run-dir eval_results/<COMPLETED_RUN> --role discovery

python -m strategy_research propose \
  --model-config eval_results/strategy_research/teacher.json \
  --episode-ids <EPISODE_ID_1> <EPISODE_ID_2> ...          # 含失败轨迹

python -m strategy_research review --candidate-id <CID> \
  --disposition accept_for_validation --reviewer <you> --rationale '...'

# 在 validation 组做 candidate/no_strategy/nearest 配对验证
python -m strategy_research plan --candidate-id <CID> \
  --dataset agentdojo --rows data/agentdojo/agentdojo_injection_steps.jsonl \
  --targets <TRAIN_TARGET_A> <TRAIN_TARGET_B> \
  --seeds 0 1 2 --budget 5 --limit 12 \
  --model-config eval_results/strategy_research/teacher.json
python -m strategy_research run-plan --plan-id <PLAN_ID> --runs-dir eval_results/strategy_research/runs \
  --python /root/miniconda3/envs/piminer/bin/python
python -m strategy_research report --plan-id <PLAN_ID>
python -m strategy_research activate --report-id <REPORT_ID>       # 通过门槛才激活
python -m strategy_research export-library --output eval_results/strategy_research/verified_library.json
```

新机制通过验证后，按 `cards/` 的 YAML schema 手工/脚本落一张新卡，并把其命中经验追加进
`experiences.jsonl`（见 §4）。**不要**凭空手写机制卡——机制必须有真实证据背书。

## 4. 证据回流工具 `ingest_research.py`（已实现）

把 strategy_research 实跑产出并入紧凑库，原 `strategy_library/` 保持只读基线。
证据分两个文件：`experiences.jsonl`（`build.py` 从 JSON 无损重建）+
`experiences_research.jsonl`（本工具 append，`build.py` 不覆盖）。`build.py` 重建时
把后者按 strategy_id 折进各卡的 `example_refs` / `split_role_distribution`，卡片即
两处证据的合并视图，而 `experiences.jsonl` 与 `--check` 保持纯 JSON 来源。

**A1 回流（现有机制的新目标命中）：**

```bash
python strategy_library_compact/ingest_research.py experiences \
  --db eval_results/strategy_research/research.sqlite \
  --strategy-id <现有卡的 strategy_id> \
  --manifest eval_results/strategy_research/manifest_agentdojo_draft.json
  # 默认扫库内全部 episode；也可 --episode-ids <id...> 指定
```

只接收 **discovery 组 + 黑盒 + 非 synthetic + 真实命中** 的 episode；test/dev/validation
组、未命中、synthetic、privileged（除非 `--allow-privileged`）一律拒绝并列出原因。
按 `experience_id` 去重，可重复运行。

**A2 回流（新验证机制建卡）：**

```bash
python strategy_library_compact/ingest_research.py card \
  --export eval_results/strategy_research/verified_library.json \
  --dataset agentdojo
  # 默认导出里全部已验证机制；也可 --candidate-ids <id...> 取子集
```

从 `export-library` 的已验证候选提炼一张新 `cards/*.yaml`（机制/招式/前提/失败信号
来自 candidate 的 proposal）。随后用上面的 `experiences` 命令把该机制的命中并入。

**闭环：**

```bash
collect-plan/run-plan (A1)  或  propose→…→activate→export-library (A2)
  → ingest_research.py experiences|card
  → build.py --manifest ... --manifest ...      # 折进卡片视图
  → build.py --check                            # 确认基线保真
```

---

## 纪律（勿违反）

- **只在 discovery 组扩展**；validation 仅用于 §3 的配对验证；development/test 绝不参与挖库或选择。
- 新经验必须关联真实 trace/run-dir，`why_it_landed` 只用当时可见的黑盒反馈。
- 目标偏斜是当前最大短板：优先补"同机制跨目标"，而不是在 nano 上堆更多同类命中。
- 每次扩展后跑 `build.py --check` 确认保真，并检查 `index.json` 的 `split_role_counts`
  没有把 test 组样本算进可训练证据。
