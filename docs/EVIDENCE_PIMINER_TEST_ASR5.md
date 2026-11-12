# PIMiner 测试清单上的 Evidence ASR@5

2026-10-30 恢复上次未启动的实验。清单来自相邻 PIMiner 的 AgentDojo 测试选择（seed=42，n=30，offset=20），按已有可观察必要 Trigger 规则纳入 22 例、排除 8 例，不补样。完整清单见 reports/full_test_inventory_20261030.md。该清单曾被执行，不声明 independent held-out。

固定设置：原 PIMiner Router 和原 Attacker，StageAware Retry/Revise，16 个独立样本进程；每例初始执行一次，失败后最多四次，总预算五次。每个 Payload 最多 Retry 两次。所有样本重新生成 P0 和路由，不复用旧执行。模型沿用 gpt-5-nano Target 和 deepseek-flash[1m] Attacker，现有服务地址分别为 https://api2.aigcbest.top/v1 和 https://api.deepseek.com/anthropic。凭据仅在进程环境中加载，不写入报告。

运行环境沿用 /tmp/minipim-a4-test-env/bin/python（Python 3.11.16、agentdojo 0.1.35、openai 1.107.3、pydantic 2.13.5、PyYAML 6.0.3），版本与上次完成的重复实验相同。模型通过 API 执行，不分配本机 GPU。锁文件校验数据、策略库和运行环境；源码哈希已移除，旧锁中的源码条目不再校验。

从 /home/zhengqingliu/data/PIMiner_evidence 执行离线检查（无模型调用）：

```bash
/tmp/minipim-a4-test-env/bin/python scripts/run_evidence_parallel.py --frozen configs/evidence_piminer_test_asr5_20261030.lock.json --output runs/evidence_piminer_test_asr5_20261030 --workers 16 --dry-run
```

真实运行及完成后自动汇总、离线评分/决策核验：

```bash
/tmp/minipim-a4-test-env/bin/python -u scripts/execute_full_evidence.py --frozen configs/evidence_piminer_test_asr5_20261030.lock.json --output runs/evidence_piminer_test_asr5_20261030 --report-prefix reports/evidence_piminer_test_asr5_20261030 --coverage data/agentdojo_full_observable_20261030/coverage.json --workers 16
```

运行目录存在时命令会拒绝重新执行。错误保留为缺失，不自动重采样。最大预算：110 次 Target 执行、22 次 Router 调用、110 次候选生成，另有 3 次连通性预检。

监控文件为 runs/evidence_piminer_test_asr5_20261030/status.json 和 worker_results.json；父日志为 reports/evidence_piminer_test_asr5_20261030.run.log。完成后生成同前缀 .json、.md 和 .supervisor.json。单个 worker 的 completed 仅表示流程结束，Target/生成错误仍属于缺失结果。

运行期间可使用独立前缀生成快照，不调用模型：

```bash
/tmp/minipim-a4-test-env/bin/python scripts/summarize_full_evidence.py --run runs/evidence_piminer_test_asr5_20261030 --output-prefix reports/evidence_piminer_test_asr5_20261030_progress --coverage data/agentdojo_full_observable_20261030/coverage.json --verify-completed
```

主分母固定为 22，并同时报告原清单 30 / 纳入 22 / 排除 8。证据驱动 Retry（preserve_observed_trigger）与 U 固定计划回退 Retry 分开报告；Retry 的开始、完成、Payload 不变和 Gold 结果分别核验。存在调用错误或未完成样本时，完整 ASR 保持 null。单组结果不能证明 Evidence/Retry 的因果收益。
