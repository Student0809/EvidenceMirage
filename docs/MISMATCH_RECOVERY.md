# Mismatch错误分支恢复

**2026-11-10 更新：当前代码仅恢复 `tool_evidence_piminer_library_v3` 运行，并校验组别和证据视图。下面“本次运行”部分是 v2 历史记录，其中旧 G00 已按研究条件归为 G10；旧命令不能在 v3 下续跑。新 G00 请按 FEEDBACK_ABLATION.md 从初始输入启动。**

入口：python -m src.mismatch.recovery。仅接受已经结束且有完整冻结校验文件的当前经验库消融协议运行。

恢复写入全新目录；原始运行不变。正常结束分支原样复制，错误分支复用逐字匹配的已完成Assessment输出、成功生成的候选成品及成功Target执行，随后继续失败位置。恢复协议为controller_boundaries_v2，不再重放Planner/generator内部调用；旧生成记录作为成品来源保留，并标记replayed_artifact。Target错误重试同一候选的失败执行槽位；GenerationError重新运行最后一次Assessment之后的生成操作。保持G组别、模型、初始P0和逻辑五轮预算。当前分支历史由原输出重建，提示或候选不同会终止。

默认仅选择RateLimitError；--include-generation-errors额外选择GenerationError。后者可能是格式/规划/候选校验失败，不应全部称为网络故障；允许重新采样只针对该失败操作，不重选正常结束的失败攻击或budget_exhausted分支。

## 本次运行

源：runs/mismatch18_G00_20261109_191756。
RateLimitError：b001、b004、b010、b014。
GenerationError（内部ValueError）：b005、b006、b011、b016。

在项目根目录：
```bash
export ABLATION_PY=/home/zhengqingliu/.cache/minipim/library-ablation-venv/bin/python
export PYTHONPATH=/tmp/piminer-ablation-test-deps-20261109${PYTHONPATH:+:$PYTHONPATH}
export PIMINER_RATE_LIMIT_RETRIES=6
export PIMINER_RATE_LIMIT_BASE=2
export PIMINER_RATE_LIMIT_CAP=60
export RECOVERY_OUTPUT="runs/mismatch18_G00_recovered_$(date +%Y%m%d_%H%M%S)"

"$ABLATION_PY" -m src.mismatch.recovery \
  --source runs/mismatch18_G00_20261109_191756 \
  --output "$RECOVERY_OUTPUT" --workers 2 \
  --include-generation-errors --dry-run
```

确认8条后，用同一命令去掉--dry-run执行。dry-run不创建目录也不调用模型。不要使用online run覆盖原目录。

Target HTTP 429最多额外重试6次；延迟从[1,2]秒、[2,4]秒开始，指数增长，上限区间[30,60]秒；数字Retry-After较大时遵循服务端值。每次HTTP调用仍独立记账。非429异常不会在此自动重试。SDK内部重试仍关闭，避免重复叠加。

恢复完成后，新目录含status.json、metrics.json、RESULTS.md、recovery.json。原目录保留失败调用及费用；新报告标注恢复政策。五轮是恢复后的逻辑执行槽位，源运行失败执行和重新生成导致的额外实际调用必须另计，恢复结果与原结果并列报告。指数退避不能保证解除服务端限额。

本入口面向已冻结错误结果，不是中断进程的任意checkpoint恢复；缺少冻结文件或校验失败会拒绝执行。

修订完成但Target限流时，复用该候选重试Target；只有生成操作未完成时才重新生成。旧版本准备的job_public.json恢复缓存不能直接运行，请从冻结源运行重新创建新输出目录。

在线自主迭代已取消额外semantic_review调用；静态候选格式/硬约束及注入位置检查仍保留。Planner的额外输出字段忽略，不再因此拒绝计划。模型传输失败仍记录错误，不伪造生成成功。此行为变更应与原运行一起记录。

## 本机运行后复制到NFS

CLI默认--local-root ~/temp_run；--output仍指定新的最终目录。运行时日志、检查点和评分结果写入本机目录，全部完成后复制到最终目录旁的.copying临时目录，逐文件SHA256核验后重命名。中断、复制失败和成功后均保留本机目录，不覆盖源运行。

监控运行进度时查看 ~/temp_run/<输出目录名>/status.json。最终目录在复制成功后才出现。本机版本是路径引用的原始位置，复制件可能仍引用本机路径；保留本机目录即可继续追溯。若中断，先保留本机记录，不要手工伪造completed状态。

finish日志保存发生OSError时，不再覆盖模型成功结果；记录persistence_errors（errno、原因、操作），尝试写入 ~/temp_run/accounting_fallback/<call_id>.jsonl，并向stderr输出警告。首次派发前日志错误仍会阻止调用。
