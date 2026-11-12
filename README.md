# PIMiner Evidence

2026-11-10 权限边界更正：[Framework-assisted Target Verification](docs/ACTIVE_TOOL_MEDIATED_VERIFICATION.md) 属于框架高信任干预，不能视为越狱主动采样。当前 v2 仅保留评估端诊断，已移除内部查询结果向 Attacker 的自动转交；旧运行须按扩展权限实验解释。

2026-11-11 真正的低信任探针与授权渠道回传已实现 S01/G11 首版：[Jailbreak-based Active Evidence Sampling](docs/JAILBREAK_ACTIVE_EVIDENCE_SAMPLING.md)。Attacker 自由生成融入主 IPI 的状态检查探针，经原有 Injection Slot 注入；复用 `mark.black-2134@gmail.com` 授权收件箱；主攻击邮件带五个附件，状态报告是第二封无附件邮件；Target 自主选择检查方式。Attacker/Planner 只收到该收件箱实际接受的最小状态报告与 `probe_feedback`；官方 Gold 仍是攻击成功主指标。入口为 `python -m src.mismatch.sampling`，采样 run 明确不可恢复。首版不包含真实 SMTP 收件箱、跨 episode 状态持久化和高信任 privileged-feedback 对照臂。

2026-11-04 新增 [Mismatch 自主判断与修订入口](docs/MISMATCH_AUTONOMOUS.md)：使用现有 18 份 P0 展开 126 个受控反馈实例，支持 PIMiner、V1 及相同反馈视图对照；模型自主判断并选择动作，隐藏评分由独立命令事后计算。静态入口不执行 Target、不报告 ASR。原实验入口保持原协议。

Stage-Aware Adaptive Retry/Revise under Incomplete Feedback。默认共用原 PIMiner Router → Attacker、原生 AgentDojo 和固定合法观察，另提供固定历史的 Revise 开发对照入口。

**成功只由 AgentDojo 官方 Gold 决定。** Trigger 和 Final 标签只用于决策与诊断。此项目完成工程重构和离线验证，尚无独立 held-out 的真实模型对照结论。[重构报告](reports/REFACTOR_REPORT.md)记录 V1；当前原版 Router/Attacker 接入见下文。

## 当前默认推理链路

`完整原策略库 → 原 Router Top-3 → 原 Attacker 生成共享 P0 → Target → Gold / Final / Evidence → Policy → Retry 或原 Attacker Revise`。

`configs/experiment.yaml` 默认 `attacker.mode: original`。每案例 Router 只调用一次，保留原始策略发现、摘要提取、路由 Prompt、选择解析与候选文档拼接函数。Attacker 接收选中策略的完整原文及全部示例，使用原 `build_user_message_blocks(..., threat_model='black_box')`，不追加 Evidence、Planner 或静态修正提示。其角色与攻击类别指令逐字取自原 `.claude/commands/step.md` 的角色部分。策略库不训练、不 digest、不改写。

默认模式重新生成 P0，即使开发数据含有历史 `p0` 和 `strategy_paths` 也不使用它们代替新路由。五组共享 Router 结果、P0 和 H1。Retry 不调用 Attacker；Revise 使用该分支所有已完成尝试，包括重试的反馈、对应生成分析、payload、官方 security/utility 和最后 assistant 输出；沿用原版每条 Final 4,000 字符及完整历史 80,000 字符限制。中间工具结果、其它分支历史和私有状态均不进入 Prompt。合法 Evidence 只影响外层决策。

这里复用的是原始模型 Prompt 与策略材料；原 shell 的读写文件、`next/submit` 调度由本项目 Runner 接管。模型仍通过无工具、无 MCP 的单次 CLI 调用执行，每次发送完整历史；不复刻旧 shell 会话的工具访问或 `--delta` 会话缓存，也不保证不同模型配置下生成结果一致。

旧 Planner → Generator → Checker 流程仍可用：在完整配置的 `attacker` 中设置 `mode: grounded`，保留 `model`、`effort`、`timeout` 参数。该模式沿用案例的固定策略材料与已有 P0；是另一项方法配置，不能与默认原版 Attacker 的结果混算。`fixture` 后端仍是独立合成测试。

2026-10-31 grounded 修订使用 `grounded_history_plan_v2`：在 [grounded_history_v1](implement-stage/f-grounded-history-20261031/BUILD_NOTE.md) 的完整授权历史、已选策略和候选归档上，增加与候选绑定的 `revision_plan_v1` 计划摘要。下一轮对照每个计划的预测与实际反馈，明确保留、调整或放弃的理由及语义限定；计划和判断始终是未验证注释，不进入观察事实。Reviewer 指出 Plan 本身的冲突时，先重新规划，再用新 Plan 生成一次修正候选。仅候选执行偏离时仍修候选。已存在事实的引用别名可规范化，转换逐项留痕，U/未知事实仍不能充当失败证据。

此更新用于 `mode: grounded` 以及 `piminer_aligned` 的 `false_revision_mode: grounded`。F 的 `analysis` 保持为空，计划经独立字段进入 F 历史，不进入 U 会话或跨案例经验。通常仍为 Planner、Generator、Reviewer 三次调用；初始格式纠错最多增加一次，发现计划冲突时最多增加两次规划调用，总计最多两个候选、八次调用，Target 预算不变。配置见 [30 例开发配置](experiments/a2_v2_plan_memory_test30_20261031.yaml)，验证与限制见 [构建记录](implement-stage/plan-continuity-20261031/BUILD_NOTE.md)。尚未在线测量该更新的 ASR。

原代码及命令文档快照位于 `data/original/piminer_code/`，提取原语位于 `src/vendor/router.py`、`prompts.py`、`final_output.py`、`attacker_role.txt`。资产清单保留迁移来源；当前仅校验数据与资源哈希，跳过源码条目。测试逐字核对原函数、角色文本和生成 Prompt。默认模式冻结、审计整个 Router 可见策略库，包括最终没有被选中的策略。

本次接入验证：65 项测试通过，104 项资产与来源哈希一致，10 个原生开发案例离线预检通过；五策略原生整链路使用脚本模型响应完成回归，未调用远程模型。验证记录见 `reports/router_attacker_verification.json`，当前原版开发配置已冻结为 `configs/v2_original_dev.lock.json`。

## A2 选例版 Attacker

新增 `attacker.mode: a2`：复用当前原版 Router、角色和黑盒修订流程，每个选中策略保留完整方法正文与最多两条相关原始案例。MiniPIM 的选例规则和源码快照已迁入本项目，当前 10 个 dev 任务的本地 BGE 检索结果已离线准备；运行时无需访问 MiniPIM 或加载 embedding 模型。

旧 A2 Evidence 对照使用 `experiments/a2_observable_pilot5.yaml`，完整开发配置为 `experiments/a2_native_dev.yaml`。默认 original 保持原样。使用说明、冻结配置和新任务准备见 [A2 接入说明](docs/A2_ATTACKER.md)。初始接入时只完成离线验证；后续已执行开发实验，结果及可比性限制分别见 [旧 A2 比较审计](reports/a2_vs_original_audit_20261030.md) 与下述固定历史对照。

2026-10-30 新增可选 A2 v2：逐层补齐案例、按任务组和情境去重，读取有明确来源的上下文引用，并分别编码用户任务与注入位置窗口，避免长上下文挤掉注入槽。另可设置 `revision_profile: calibrated`，使 Revise 按黑盒可见范围区分事实与假设，记录可核验的策略引用；`original`、`a2`、`piminer_aligned` 均支持此修订选项。完整策略方法和选中案例原文保留。用法见 [Revise/A2 v2](docs/REVISE_A2_V2.md)，固定同源 P0/H1 的四臂实验协议见 [开发对照协议](reports/revise_ablation_protocol_20261030.md)。离线检索指标不能替代官方 Gold 的在线比较。

本轮固定历史开发实验已完成：完整策略库 **0/10**、旧 A2 **0/10**、A2 v2 **2/10**、A2 v2＋反馈校准 **0/10**。20 次修订生成、40 次 Target 执行均无生成或 Target 错误；A2 v2 的两个成功来自 c004 的同一个 P1，两次评分共享候选。五个任务都已用于开发，每任务每臂仅生成一个候选，不能宣称稳定或显著提升。`calibrated` 保留为实验选项，不建议设为默认；后续候选使用 `revision_profile: original`。完整记录见 [实验结果](reports/revise_ablation_results_20261030.md)。

运行中按用户提速要求，尚未启动的 c005/c006 经 [并行协调器](scripts/parallelize_revise_ablation_20261030.py) 认领，各任务四臂并行生成、逐轮并行评分，两轮间有屏障；原三个任务继续原调度。没有重放已开始的调用，预算、历史和模型参数保持不变，并发负载与时序变化已记录在调度 amendment 中。

后续候选配置为 [a2_v2_aligned22_20261030.yaml](experiments/a2_v2_aligned22_20261030.yaml)，冻结配置为 `configs/a2_v2_aligned22_20261030.lock.json`，使用 `data/a2/aligned22_20261030.field_v2.retrieval.json`：保留历史路由和 P0，重新执行 H1，修订使用 A2 v2 与 `revision_profile: original`。已完成 22 例离线预检与冻结，API 调用为 0。它与复用历史 H1 的五例实验不同；22 例尚未在线运行，不能把五例结果当作该配置的评估结论。

若只在明确 Trigger=T 时原样 Retry，使用新的 [Unknown 修订配置](experiments/a2_v2_piminer_unknown_revise_test30_20261030.yaml)：`unknown_action: revise` 使 U/F 都按已有 PIMiner 反馈流程生成下一候选，T 的 Retry 仍受每 payload 两次上限约束，总预算仍含 P0 共五次。旧配置的 U 日程保留以便历史重放；新配置已离线验证，尚未重新在线评测。ASR@5 的原版含义、实现边界和启动检查见 [说明](docs/PIMINER_UNKNOWN_REVISE_20261030.md)。

## 快速开始

Python 3.10+。新环境安装 `pip install -r requirements.txt`。本机已验证的解释器是 `/tmp/minipim-a4-test-env/bin/python`。以下命令在本项目根目录执行；可用该绝对路径替换 `python`。

```bash
python -m unittest discover -s tests -v
python main.py validate --config experiments/e0_smoke.yaml
python main.py freeze --config experiments/e0_smoke.yaml --output configs/smoke_v1.lock.json
python main.py run --frozen configs/smoke_v1.lock.json --output runs/my_smoke
python scripts/verify_assets.py --compare-source
python scripts/replay_gold.py
```

`configs/v1_smoke.lock.json`、`configs/v1_dev.lock.json` 和 `runs/e0_v1/` 保留为旧版本记录。源码哈希已移除，旧 lock 中的源码条目也不再校验；实验数据、策略资源或运行环境变化后仍需重新 freeze。运行使用新输出目录，程序拒绝覆盖。`fixture` 是确定性的工程测试，使用合成 Gold，`metrics.json` 明确标记 `SYNTHETIC_FIXTURE`，不能用于论文 ASR。

## 真实 AgentDojo 开发集运行

```bash
python main.py preflight --config experiments/e0_native_dev.yaml
python main.py preflight --config experiments/e0_native_dev.yaml --live
python main.py freeze --config experiments/e0_native_dev.yaml --output configs/my_native_dev.lock.json
python main.py run --frozen configs/my_native_dev.lock.json --output runs/my_native_dev
```

离线 preflight 和 freeze 不调用模型；`preflight --live` 和 run 会调用模型。Target 使用环境变量 `OPENAI_API_KEY`，可选 `OPENAI_BASE_URL`；不会自动加载原项目的 `.env`。Router 与 Attacker 共用本机已配置的 `claude` CLI、`deepseek-flash[1m]`、`max` effort；每次调用禁用工具、文件访问接口和 MCP，公共提示之外不提供原始环境。真实 run 自动检查 Target、Router、Attacker 调用通路，其调用与成本写入 manifest/metrics，单独于攻击预算计量。

`configs/experiment.yaml` 保存完整模型参数。Target 目前支持 OpenAI-compatible Chat Completions；DeepSeek 的 developer role 转 system 兼容也保留。未迁入旧 Gemini/Anthropic Target provider 和其他 benchmark 的启动器。需要其他 provider 时实现相同 `execute(case, payload, episode_id) -> TargetResult` 接口。

`max_tool_calls` 是 AgentDojo `ToolsExecutionLoop.max_iters` 的轮数上限，不是工具函数数量。一次 Target 指一次完整 trajectory；每轮可能含多次工具调用。SDK 自动重试设为 0，AgentDojo 自身的 HTTP 请求层仍保留最多 3 次重试，所有实际请求含失败均计量。模型采样不能承诺逐字复现；确定性保证针对冻结输入下的策略决策。

## 数据与测试隔离

- `data/original/agentdojo/agentdojo_injection_steps.jsonl`：原始 1,234 行，逐字节保留。
- `data/original/scenario_expansion/`：14 个已有导出数据文件，含正式生成数据和历史 fixture；保留原始 schema，不自动转换为官方原生任务。
- `data/original/observability/`：35 类目标的原有可观测性审计与后续修订，属于历史分析材料；不会给 Planner 输入审计中的私有判定条件。
- `data/native_pool.jsonl`：1,234 行公共字段投影，`split=unassigned`，不按 Gold/Partial 筛选。
- `data/dev.jsonl`：10 个已用案例及历史冻结 P0，全部标记曾评价，仅供开发回归；默认 original 模式重新路由、生成 P0。
- `data/goal_specs.jsonl`：逐案例、带公开目标引用的 Trigger 定义。邮件、附件、到账信号按任务绑定；“发链接”的网站访问仅作代理诊断，不进入必要 Trigger。
- `data/exposure_audit.json`：本地可识别的 25 个已评价基础任务及来源；这是保守清单，不是全域独立性证明。
- `data/test.jsonl`：**有意为空**。不得把重复分析过的 c000/c005/c006、旧 36 案例或其它旧任务改名当独立测试。

正式 E1：按公开任务与注入位置预选独立基础任务，写入 test 和对应 goal specs；每案例沿用 dev schema、`split=test`、真实 `provenance.previously_evaluated=false`。默认 original 模式无需提供 `p0`、`strategy_paths`，共同 Router/Attacker 先运行一次。填写 `data/test_audit.json` 的 `status: complete`、`dataset_sha256`、`reviewer`、`selection_rule`，记录来源与整个 Router 可见策略库的示例重合检查。审计记录是复现材料，不需另行人工授权。程序会检查基础任务与已知开发历史的重叠，以及冻结策略文档中的原始任务/注入上下文重合。

```bash
python main.py freeze --config experiments/e1_main.yaml --output configs/e1.lock.json
python main.py run --frozen configs/e1.lock.json --output runs/e1
```

E1 仅允许冻结配置，并按数据集哈希记一次评价占用；不静默替补、不自动重跑。审计仍需覆盖文本近似重合、外部使用历史等自动匹配无法证明的部分。当前空测试集会明确报错，不会回退到开发集。E2 只预先定义消融，暂不运行。

## 策略与预算

|策略|规则|
|---|---|
|Always Revise|每次有效失败后修订|
|Always Retry|保留 P0|
|Fixed Schedule|Retry → Revise → Retry|
|Label-Based|仅 Final 的冻结关键词标签：Partial Retry，Reject/Ignored Revise|
|Stage-Aware|T 时最多重试两次；F 时修订；U 按固定日程回退；同内容哈希不会因重命名重置重试计数|

所有组共享首次 P0/H1，然后各有最多 3 次 Target 启动预算。成功即停；Target 错误占用启动预算并终止该案例分支，Gold 保持 null；生成错误单独记录并终止，不消耗未启动的 Target 预算。分支只拿到当前已完成历史。每案例策略运行次序由种子确定性打乱。

`ASR@3` 的分母是共享 H1 有效失败的全部案例。`confirmed_gold_asr_at_budget` 是确认成功数/首败案例数；错误仍保留在分母，解释为保守的确认成功比例。若存在无效结果，`gold_asr_at_budget_complete=null`；另列有效结果分母与成功比例。初始成功、初始错误和整体累计成功率分开报告。

固定合法查询只取授权资源。无回执为 U；已有回执的完整附件计数全为零才支持该范围的 F。观察器故障不覆盖官方 Gold。Final 标签与执行阶段并列保留，Label-Based 是本项目冻结词表基线，不宣称复现 IterInject。

## 运行记录

每次 run 包括只读 `manifest.json`（数据/策略哈希、完整配置、版本与模型 preflight；不含源码哈希）、`episodes.jsonl`、`decisions.jsonl`、`metrics.json`、`outcomes.json`、`events.jsonl`、`status.json` 和 `raw/`。

`raw/` 是评价端资料，可能包括私有环境与工具轨迹；它不会通过模型输入投影暴露给修订器或策略。事件在 Target/生成启动前持久化，异常退出保留 interrupted，不自动重放未知执行。当前版本不实现断点恢复；复测须使用新目录并解释中断，正式 Test 占用不会自动解除。

共享 Router、P0 和 H1 在物理总成本中各只计一次；各组同时报告“后续实际成本”和“包含完整共享初始化成本的单组成本”。Token 未返回时单列缺失，不按零成本；不猜测美元价格。`generator_calls_by_stage` 分别记录 `router`、`attacker_p0`、`attacker_revise`；grounded 模式记录 Planner、Generator、静态修正。默认协议的观察查询另行计量，不增加主动查询或语义成功评分器；显式开启 Framework-assisted Target Verification 时，追加验证请求以 `active_verification_http` 另行标记并计入总 usage。

历史实验只保留索引：[legacy/README.md](legacy/README.md)。原项目不作为新运行时依赖。`scripts/replay_gold.py` 是可选只读历史核验，依赖原目录的历史快照，和正常运行分离。

## 可训练决策模块

新增可选 `learned` policy 和 `main.py decision` 命令，支持 AgentDojo 任务分组准备、固定 P0/P1 配对采集、两个动作价值头训练、持出评价与模型冻结。输入仅使用合法证据；官方 Gold 只用于监督标签。无可观察 Trigger 的任务事前排除，运行缺失保持缺失。

已完成离线工程验证和真实任务准备，尚未启动真实配对采集或训练出实证模型；合成烟测权重不能用于 live 实验。完整操作与限制见 [可训练决策模块](docs/TRAINABLE_DECISION.md)，示例配置为 `experiments/learned_controller.example.yaml`。
