# 实验 A：固定历史 P0 的当下重复对照

使用历史首轮成功的 c007、c017、c028，分别调用原 PIMiner 与当前 Evidence 的真实 Target 入口，各独立重复 5 次，共固定 30 次顶层执行。只执行历史 P0，不调用 Attacker/Router，不生成新 payload。比较的是同一当前依赖和端点下的两条入口，不是恢复 2026-10-23 的服务端模型快照。

## 环境与来源

工作目录为 `/home/zhengqingliu/data/PIMiner_evidence`。复用 `/tmp/minipim-a4-test-env/bin/python`：Python 3.11.16、AgentDojo 0.1.35、OpenAI 1.107.3、HTTPX 0.28.1、Pydantic 2.13.5、PyYAML 6.0.3、tenacity 9.1.4、python-dotenv 1.2.4。未安装或升级包。模型通过 API 执行，3 个本地 worker，不分配 GPU、不下载权重。

环境声明为 `configs/p0_stability_env_20261030.json`，ledger 为 `.aris/compute/local.md`。版本 pins 描述实测既有环境，不声称已验证一键重建全部传递依赖。与既有冻结 manifest 的重叠版本一致。

历史样本目录为 `../PIMiner/eval_results/pim_test/deepseek_claude_agentdojo_nano_20261023/iterative_attack_01_agentdojo_gpt-5-nano/samples/`，读取 `007.json`、`017.json`、`028.json` 的 `history[0].injection`。与 `data/piminer_aligned_20261030/initializations.json` 核对完整源文件 SHA、任务身份、P0 原文及 SHA。

原入口使用 `../PIMiner/iterative_attack_orchestrator/iterative_attack_common.py` 的 `build_reward`、`run_full_trajectory` 和原 `benchmarks/agentdojo/reward.py`；当前入口使用 `src/adapters/agentdojo.py` 的 `AgentDojoAdapter.execute`，配置来源为 `configs/evidence_piminer_aligned_asr5_20261030_v2.lock.json`。

Target 为 `gpt-5-nano`、benchmark `v1.2.2`、端点 `https://api2.aigcbest.top/v1`。正式启动器复用既有环境变量或相邻 PIMiner/MiniPIM 的 dotenv 凭据，凭据不写入报告。dry-run 不加载该凭据、不验证远端连通性、不调用 live preflight。保留当前代理设置，仅记录 presence。

## 固定协议

每个案例由一个 worker 顺序执行 5 组 original/current 配对，组内先后按案例和重复编号交替；总共 3 个 worker。每次顶层执行重新创建原生环境及客户端。成功也不早停；错误保持未决，不补样、不重跑已开始的 trial。

| 行为 | 原入口 | 当前入口 |
|---|---|---|
| OpenAI SDK retries | 当前 SDK 默认 2 | 显式 0 |
| SDK timeout | 默认 connect=5 秒，read/write/pool=600 秒 | 显式 120 秒 |
| AgentDojo 请求重试 | 保留当前包规则 | 保留当前包规则 |
| 顶层内 pipeline.query | 原函数最多 3 次，内部环境复用 | 一次 |
| 工具执行循环上限 | 当前包默认 15 | 当前配置 30 |
| 工具观测包装 | 原工具，不新增 Collector | 当前 Collector 保持不变 |
| 无 final/异常结果 | 保留旧原始评分，另外标审计有效性 | Gold=null 的 Target error |

两臂保留各自默认 system 工厂、无 defense、reasoning_effort=null；不给 Target 新增 seed、temperature、retry 或 timeout。工具循环轮数不等于工具调用总数。原函数最多 3 次重入不是保证重入 3 次：某些异常会触发原函数自身后续异常而提前退出。内部异常后真正恢复、有正常 final 的结果可有效，同时报告内部错误；无 final/顶层错误不能因旧评分为 false 就当作有效失败。

## 离线命令

从上述工作目录逐字执行：

```bash
/tmp/minipim-a4-test-env/bin/python scripts/run_p0_stability.py --output runs/p0_stability_A_20261030 --dry-run
```

成功输出 JSON 包含 `status="offline_valid"`、指定 3 个案例、`trials=30`、`workers=3`、`api_calls=0`。检查配置/数据来源、依赖版本、原 P0、原生任务与注入槽；不会创建输出目录。此检查不证明远端 API 可用或正式执行成功。

输出目录已经存在时，连 dry-run 也会拒绝。不得为通过检查删除已有运行目录；应检查保存的状态。

## 正式执行与审计

以下命令消耗用户已授权的 30 次顶层执行预算。文档作者的离线检查不执行它，主执行者在实现检查和独立文档核验后启动：

```bash
/tmp/minipim-a4-test-env/bin/python -u scripts/run_p0_stability.py --output runs/p0_stability_A_20261030
```

`manifest.json` 固定 30 个 trial、P0、配置/数据哈希与依赖环境；源码仅在 `source_paths` 记录路径，`source_snapshot/` 保留副本供查看，`source_snapshot.json` 只记录原路径和副本路径，不生成或校验源码 SHA。旧 manifest 中的 Python 源码哈希不再参与验证；其中配置/数据检查仍然保留。历史运行记录不重写。`status.json`、`summary.json`、`report.md` 供监控。`trials/<trial_id>/` 保存 started/terminal、原始结果、轨迹及请求审计。模型输入输出可能含实验 payload，只在本地原始记录保留，不直接复制进公开简报。

顶层 started 必须先于网络调用持久化。中断后 started 无 terminal 的 trial 视为状态未决，不能自动补跑。顶层执行、pipeline 重入、SDK create 调用、SDK 内部 HTTP send 和较低层 transport/redirect 尝试分别记录。客户端计数不是远端实际处理或计费次数，也不是同一个统计层级；不能把一个顶层调用说成仅一次 API 请求。

每个案例×入口报告成功、有效失败、错误、待完成及固定分母 5；有缺失时完整成功率为 null。这是 3 个选定案例上的重复测量，不估计全 benchmark ASR，不证明普遍优劣或历史模型未变。

## 文档核验状态

作者逐字检查记在 `.aris/compute/local.md`。compute-env-contract 要求作者检查与 fresh-agent-follows-doc 分开；独立代理应只读取本文件、ledger、run-experiment/compute-env-contract，并逐字执行同一离线命令。没有独立证据时不能把作者检查写作独立通过。
