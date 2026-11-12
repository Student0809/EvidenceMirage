# 额外工具证据 × PIMiner 历史经验库消融

当前协议：`tool_evidence_piminer_library_v3`。A 表示攻击侧能否看到额外工具证据，P 表示能否使用 PIMiner 历史策略库。

| 组别 | A：额外工具证据 | P：历史策略库 | 判断格式 | 修订后端 |
|---|---|---|---|---|
| G00 | 不可见（final_only） | 关闭 | ordinary | V1 Planner / Generator |
| G10 | 可见（authorized_evidence） | 关闭 | ordinary | V1 Planner / Generator |
| G01 | 不可见（final_only） | 开启 | ordinary | V1 Planner / Generator |
| G11 | 可见（authorized_evidence） | 开启 | ordinary | V1 Planner / Generator |

四组统一输出 goal_status/action/action_reason；Stop 仍只表示判断目标完成。A 不再切换结构化判断。A1 提供预先定义的工具返回证据，不额外增加 Planner 在线查询；此处“主动采样”按研究定义指额外证据可见性。

A0 在评估和修订入口对当前及全部历史 PublicAttempt 清空 packets，因此评估 available_refs、Planner 的 query_results/predicates/facts、Generator 和后续历史均不接收工具证据。原始证据仍保留在输入和执行产物中，供独立评分；它不会因为被保存就进入模型提示。Target 最终回答中的工具描述仍作为模型自报保留。

P=1 使用准备数据中的 strategy_document，不重新路由、不增加判断经验库、不在线跨任务学习。P=0 屏蔽该文档并禁止生成器读取策略文件。

四组保留各样本共同的固定初始攻击 P0、已有分析、Target 最终回答及本分支的修改历史；关闭 P 不抹除 P0 已有策略内容。初始反馈是受控静态反馈，不是本轮执行 P0 的结果。独立 Gold 和私有状态始终不提供给攻击侧。

## 已有 18 例运行的重新归类

`runs/mismatch18_G00_continue_20261109_234253` 实际为 **G10 / A1P0**：ordinary 判断、工具证据可见、历史策略库关闭。结果仍为 8/18、ASR@5=44.4%、保留轨迹 49 次 Target 执行。

权威重分类记录位于该目录的 `EXPERIMENT_CLASSIFICATION.json`。原 manifest、metrics、分支日志和冻结校验保留旧协议及旧组名，避免改写历史。比较时使用 sidecar 中的 group=G10；不要依据旧目录名将它归入新 G00。

新旧协议不可混用恢复缓存。新 recovery 拒绝 v2 源运行，即使存在重分类 sidecar；v3 只允许同组、同证据视图的恢复。真正的 G00 必须从共同初始 P0/静态反馈新建运行，不能继承旧 G10 的评估、计划或候选。

## 启动真正的 G00

在项目根目录使用已经配置好的运行环境：

```bash
export ABLATION_PY=/home/zhengqingliu/.cache/minipim/library-ablation-venv/bin/python
export PYTHONPATH=/tmp/piminer-ablation-test-deps-20261109${PYTHONPATH:+:$PYTHONPATH}
export G00_OUTPUT="runs/mismatch18_G00_v3_$(date +%Y%m%d_%H%M%S)"

"$ABLATION_PY" -m src.mismatch.online run \
  --selection configs/mismatch/all18_m2.json \
  --ablation G00 --workers 2 --output "$G00_OUTPUT" --dry-run

"$ABLATION_PY" -m src.mismatch.online run \
  --selection configs/mismatch/all18_m2.json \
  --ablation G00 --workers 2 --output "$G00_OUTPUT"
```

默认组仍为 G11。`--dry-run` 不创建输出目录、不调用模型，返回的 ablation 应为 tool_evidence=false、historical_experience=false、feedback_view=final_only、assessment_mode=ordinary。静态命令的 `--view` 如与组别冲突会在调用模型前报错。

## 离线验证

测试覆盖四组元数据、初始与后续两轮历史的证据隔离、评估/规划/生成各阶段的哨兵检查、改变隐藏证据不改变 G00 提示、原始证据不变、旧缓存拒绝及 v3 同组恢复。fixture 是接口测试，不是经验成功率。
