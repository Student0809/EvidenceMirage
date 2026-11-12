# Evidence 开发集并行重复实验

`scripts/run_evidence_parallel.py` 将不同样本放入独立进程，接受 `--workers`（默认 5，实际上限为样本数）。每个进程独立创建模型适配器、原生环境、Router 缓存、决策历史和结果目录；同一样本内的执行仍按原 Controller 顺序进行。父进程独占写入汇总状态。单例调用错误保留缺失，不会自动重采样；一个进程失败不丢弃其他样本的结果。

本入口限定为 development split、原 Attacker、单 `stage_aware` 策略，且任务必须至少有一个攻击者可观察的必要 Trigger。它不替代独立测试集的注册与去重流程。

当前准备的重复实验使用 c000、c001、c004、c005、c006，从全新 P0 和首轮执行开始，每例总预算为 3（初始 1 + 后续最多 2）。训练型 Controller 未启用。比较对象是旧 Evidence 运行的前三次前缀；旧运行允许首轮后再执行三次。此次不是固定成功 Payload 的重放，也不是独立保留集结果。

旧实验后项目新增了 A2 模式；本次显式保持 `attacker.mode=original`。关键运行代码与数据的哈希检查及原提示词的逐字重建见 `reports/evidence_repeat_protocol_audit_20261030.json`。该报告保留为历史记录；当前已移除源码哈希生成与校验，配置锁只校验数据、策略资源和运行环境。

离线检查：

```bash
/tmp/minipim-a4-test-env/bin/python scripts/run_evidence_parallel.py \
  --frozen configs/evidence_repeat_asr3_pilot5_20261030.lock.json \
  --reference-run runs/observable_pilot5_20261029 \
  --output runs/evidence_repeat_asr3_pilot5_20261030 \
  --workers 5 --dry-run
```

真实运行使用同一命令去掉 `--dry-run`。真实运行会发送所需 benchmark 提示与历史至既有 Target/Attacker 服务，具体端点、预算及启动授权状态记录在 `reports/evidence_repeat_launch_20261030.json`。启动曾被自动审批系统拦截；用户随后明确确认了上述两个数据目的地，现已启动本轮。实际完成状态以运行目录的 status.json 为准。

完成后分析与核验：

```bash
/tmp/minipim-a4-test-env/bin/python scripts/summarize_evidence_repeat_20261030.py \
  --run runs/evidence_repeat_asr3_pilot5_20261030 \
  --reference-run runs/observable_pilot5_20261029 \
  --outputprefix reports/evidence_repeat_asr3_pilot5_20261030
/tmp/minipim-a4-test-env/bin/python scripts/verify_evidence_repeat_20261030.py \
  --run runs/evidence_repeat_asr3_pilot5_20261030 \
  --output reports/evidence_repeat_verification_20261030.json
```

主结果使用全体五例作为分母，并同时报告确认成功、完整失败与错误缺失。存在缺失时完整 ASR@3 为 null。另报告 c004 是否再次成功、成功轮次，以及有效失败中 `Gold=False, Trigger=T` 的频率和这些节点的实际动作。一次完整重复仍不能估计稳定成功概率或证明因果收益。
