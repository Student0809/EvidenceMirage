# Unknown 使用 PIMiner 反馈修订，仅明确 T 时原 payload Retry

后续更正：用户要求 F 保留此前可读取 Evidence 的黑盒修订。本文所述纯黑盒计划未在线启动；实际重跑采用 [mixed revision 协议](/home/zhengqingliu/data/PIMiner_evidence/reports/a2_v2_mixed_evidence_test30_protocol_20261030.md)，即 U 原 PIMiner、F Evidence-aware、T 原样 Retry。

2026-10-30。此版本已经实现并完成离线验证；尚未用它重新运行 30 条在线测试。此前 4/30 的结果属于旧 Unknown 固定日程配置，不能沿用为新版本的得分。

## PIMiner 的 ASR@5

原实验 [deepseek_claude_agentdojo_nano.yaml](/home/zhengqingliu/data/PIMiner/experiments/deepseek_claude_agentdojo_nano.yaml:4) 配置 `max_iters: 5`。每任务先生成和评测 P0，失败后结合已有 payload、analysis、官方成功判定和黑盒 final output 生成下一候选，最多再评测 P1–P4。任一次官方成功即停止。ASR@5 是在最多五次此类尝试中至少成功一次的任务比例。

原版不是固定 payload 连跑五次，也不是五个互不相关的全新采样。Router 每任务先选一次策略，后续固定这组候选策略，持续会话根据失败历史修订；跨任务可共享当前运行已经结束案例的滚动经验。参见 [原 parallel driver](/home/zhengqingliu/data/PIMiner/piminer_test_parallel.sh:125) 和 [增量提示](/home/zhengqingliu/data/PIMiner/iterative_attack_orchestrator/iterative_attack_common.py:356)。

这里“五次”计完整 Target 尝试槽，包含 P0；每个槽可能产生多个底层 API 请求，并保留原生内部错误恢复，不能说总共最多调用五次 API。

## 新的 Controller 规则

| 已完成结果 | 下一步 |
|---|---|
| 官方 Gold 成功 | Stop |
| Target 错误或预算耗尽 | Stop |
| Trigger=F | Revise：根据已有失败反馈生成下一候选 |
| Trigger=U | Revise：调用同一 PIMiner-aligned 反馈修订路径 |
| Trigger=T，当前 payload 尚未达到重试上限 | Retry：原 payload 字节不变，在新环境重跑 |
| Trigger=T，达到重试上限 | Revise |

同一 payload 最多重试两次，上限按 SHA-256 字节身份累计，改候选名称不重置。总预算仍是 P0 加最多四次后续 Target 执行；Retry 和 Revise 都使用同一目标执行预算。

例如连续 U 且没有错误或成功时，新规则为 `P0 → P1 → P2 → P3 → P4`，每个箭头都调用修订生成器。旧日程在同一预算下为 `P0 → P0 → P1 → P1 → P1`，其中三次原样重跑来自预设日程，而非正向执行证据。

Revise 表示确实调用生成器并提供已完成的失败历史；模型偶然返回相同字节时仍保留真实调用及其成本，不事后改称免费 Retry。后续 Target 仍从新环境执行，不接着上次的私有状态继续。

## 实现与兼容

`StageAware` 新增 `unknown_action` 参数，可选 `schedule` 或 `revise`。新配置显式设置：

```yaml
policies:
  stage_aware:
    max_retries_per_payload: 2
    unknown_action: revise
```

新的 U 分支原因记录为 `unknown_piminer_revise`。Registry 和 Runner 直接使用该决定，进入已经实现的 `aligned.generate_with_history`：首修订包含完整公开失败历史，之后在同一任务持续会话中追加 delta 和新增的已完成反馈；A2 v2、原 revision profile、既有路由和原 P0 保持此配置的定义。

历史兼容默认仍为 `unknown_action: schedule`，其动作及 reason 完全保持原样；旧配置、冻结文件和运行数据不改写。全局 legacy 配置不会悄悄改变历史解释，新行为通过下面的新配置启用。

此处复用的是 PIMiner 的反馈修订逻辑，不宣称逐字复刻全部原驱动：本项目对真实 Target/生成错误终止更严格；aligned 不能恢复生成原始 P0 时的隐藏会话；A2 v2 本身也改变了策略案例视图。没有把原版遇到错误继续消耗预算的行为引入新版本。

## 生成器的反馈边界：沿用原版 black_box，不加入 Evidence

用户补充要求是：U 时保持 PIMiner 的原黑盒生成方式，不借助新增的工具执行证据生成 P0/P1/P2。本次配置使用 `piminer_aligned` 与 `revision_profile: original`，没有启用使用 Evidence 规划修订的 grounded 生成路径。

- P0：原始 PIMiner 在执行前根据公开任务、注入上下文和策略生成，没有本轮执行反馈；此配对配置复用经核验的原 P0，也不会注入本轮 Evidence。
- P1/P2 等：生成器只能接收 `iter / analysis / injection / security / utility / final_output` 六个历史字段及原本允许的公开任务和策略信息。`Trigger`、query packets、Evidence 条件状态、运行后中间工具调用/返回、私有状态均不渲染到生成提示。
- Evidence 只用于外层 Controller 选择动作。U 调用上述原黑盒修订；明确 T 可保留当前 payload Retry；F 调用同样的黑盒修订，生成器本身不读取 F/T/U 的原因。
- 滚动经验发布也使用同一六字段白名单，不把 Evidence 或原始工具轨迹通过经验间接传给生成器。

“和 PIMiner 一样”需要保留一个原版细节：`final_output` 是最后一条 assistant 消息的文本及其自身携带的最终 tool call（若有），不是全部工具调用轨迹；中间 tool results 始终隐藏。因此它不等于删掉最终输出中所有工具名称。任务原本公开的注入位置上下文也保留，那是攻击输入，不是本轮额外执行证据。原版定义见 [extract_final_output](/home/zhengqingliu/data/PIMiner/iterative_attack_orchestrator/iterative_attack_common.py:258)。

边界检查见 [aligned 白名单与隐私标记测试](/home/zhengqingliu/data/PIMiner_evidence/tests/test_piminer_aligned.py:101) 和 [最终输出投影测试](/home/zhengqingliu/data/PIMiner_evidence/tests/test_revision_profile.py:192)：额外传入的 `PRIVATE_EVIDENCE`、中间 trajectory、工具返回及私有状态标记不会进入 full/delta 提示。此前 30 条运行的 79 次实际生成提示也通过原黑盒模板逐字重建核验；新规则改变的是动作选择，而非扩充生成器可见反馈。

## 30 条配置与零 API 检查

配置：[a2_v2_piminer_unknown_revise_test30_20261030.yaml](/home/zhengqingliu/data/PIMiner_evidence/experiments/a2_v2_piminer_unknown_revise_test30_20261030.yaml)。冻结文件：[lock](/home/zhengqingliu/data/PIMiner_evidence/configs/a2_v2_piminer_unknown_revise_test30_20261030.lock.json)。同一批原始 30 条任务，22 条可观察、8 条 U；同一固定路由/P0、A2 v2 field 资源和策略内共享本轮经验，单条预算五次，16 路并行。

从 `/home/zhengqingliu/data/PIMiner_evidence` 执行：

```bash
/tmp/minipim-a4-test-env/bin/python scripts/run_evidence_parallel.py --frozen configs/a2_v2_piminer_unknown_revise_test30_20261030.lock.json --output runs/a2_v2_piminer_unknown_revise_test30_20261030 --workers 16 --dry-run
```

预期 `offline_valid`、全部 30 个 ID、workers=16、api_calls=0、最多 150 次 Target trajectory、Router=0、最多 120 次修订生成，且不创建 run 目录。该命令不调用模型；本次没有启动新的在线评测。

## 验证与解释边界

新增 12 项测试验证 U、T→U、F→U、观察错误下 U、T 的 SHA 上限、F 原规则、停止条件、非法参数和真实 Runner 的完整历史传递。全 U 且预算四次时，真实 Runner 生成四次、执行五次，候选为 P0–P4；混合 T→U 时，先原样 Retry，随后调用一次修订且传入前两次完整反馈。测试不调用模型。

全量回归 **510 项通过**（104.685 秒），包含原有生成反馈隔离测试，日志为 [full regression](/home/zhengqingliu/data/PIMiner_evidence/reports/unknown_piminer_revise_full_regression_20261030.log)。文档 dry-run 和冻结一致性经 [独立核验](/home/zhengqingliu/data/PIMiner_evidence/reports/unknown_piminer_revise_independent_review_20261030.md) 通过；API 调用为零。

还对旧 30 条运行的 133 个正常 Controller 决策逐条重放，旧模式的 action 和 reason 均精确一致；额外的生成错误 Stop 是 Runner 的停止记录，不当作新的策略预测。将新策略输入同一批**旧历史**时，原 23 次 U Retry 全部变为 Revise，2 次明确 T Retry 保留。证据：[saved-history check](/home/zhengqingliu/data/PIMiner_evidence/reports/unknown_piminer_revise_saved_history_check_20261030.json)。

这只验证了新决策规则。新修订会改变 payload 和后续历史，因此不能拿旧历史重放推算新 ASR，不能直接给原结果增加成功或删除失败。
