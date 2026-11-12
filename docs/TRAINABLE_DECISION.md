# 可训练 Retry/Revise 决策模块

本模块实现训练路线中的**决策学习**：根据合法执行证据，分别估计保留当前 Payload 和采用一个固定修订候选的下一次执行成功概率。它不训练新的 Attacker、不训练语义证据提取器，也不实现主动查询采样。

入口：`main.py decision`。现有可用解释器是 `/tmp/minipim-a4-test-env/bin/python`，无需新增 ML 依赖。

## 当前已完成什么

- 原生 AgentDojo 任务准备：`data/decision_20261029/`，52 train / 2 dev / 2 test；56 个不同基础任务，全部通过原生任务/注入位置离线校验。
- 训练、评估、JSON 模型保存/加载、`learned` policy 接入和工程烟测。
- `reports/decision_module_smoke_20261029/` 是**合成工程验证**，包括合成权重；不能用于真实 AgentDojo 实验或论文效果结论。
- **尚未采集新真实配对标签，也没有完成真实数据模型训练。** 原始任务 JSONL 不是带监督标签的训练集；没有用旧 Retry-only 或回顾性筛选结果伪造配对样本。

## 模型与观测语义

使用两个带 L2 正则的 logistic head，最小化按来源历史等权的二项交叉熵。每个 head 的监督目标是同一候选 K 次独立执行的官方 Gold 均值。其估计对象是单次成功率，不是 K 次至少成功一次，也不是完整剩余预算的最优价值。

特征包含回应类别、聚合 Trigger、逐必要条件的 T/F/U、同 Payload 的历史计数和预算。任务 ID、Payload 文本、Gold、utility、完整轨迹和未来结果均不作为预测特征。目标语义仍由现有公开目标绑定的 predicate 定义提供。模型概率尚未做经验校准，保存文件明确标记 `calibrated=false`。

不可观察的隐藏步骤不参与 Trigger；没有可观察必要 Trigger 的任务在执行前排除。**运行中证据不足不会被删除或改成 F。** 旧观察包没有完整事件覆盖承诺，空回执仍是缺失状态。后续可观察 Trigger 修复增加了原生在线覆盖认证；认证通过的邮件/到账通道零事件可判局部 F，未认证或异常仍为 U。至少一个必要条件有 T/F 时，模型可以使用该事实及其它缺失标记；没有可用证据或观察器报错时执行回退动作。

决策比较：
`delta = Q_Retry - Q_Revise + revision_penalty`。

- delta 大于 tie_margin：Retry。
- delta 小于负 tie_margin：Revise。
- 差距不超过阈值，或证据不可用：fallback_action，默认 Retry。
- revision_penalty 的单位是成功概率，不是美元；默认 0。
- tie_margin 只是决策阈值，不是统计置信区间。
- 原有成功、Target 错误和预算耗尽的停止规则仍由共同 Runner 控制。

初版 collector 只采集首次有效失败 H1。模型在后续历史上的复用属于尚未验证的分布外推；不能把工程上的多轮运行解释为已经学到了多轮最优策略。

## 1. 数据准备

已经准备好的目录可以直接使用。若改变划分或扩充目标定义，请换新输出目录：

```bash
/tmp/minipim-a4-test-env/bin/python main.py decision prepare \
  --output data/decision_next --seed 20261029
```

从 `native_pool.jsonl` 转移 goal spec 时，要求 suite、injection task 与公开目标文字完全一致。当前覆盖 5 个目标族、228 条合格任务记录，每基础任务预选一例后剩 56 个。其余任务的排除原因在 `excluded.jsonl`。

按基础任务以及相同规范化用户任务文本分组，已知历史暴露固定到训练侧。已纳入旧暴露审计、greeting36 预选记录和当前原生运行记录。开发/测试比例仅对未发现暴露的组计算，故默认持出规模很小；当前 test 两例均为 banking。目录内 manifest 保存分布和来源。这里是**项目内用户任务持出**，并非官方预制训练集、跨目标泛化或经过完整认证的独立测试集。正式 E1 的已有审计要求没有被绕过。

## 2. 配对采集

先查看执行上限，不调用任何模型：

```bash
/tmp/minipim-a4-test-env/bin/python main.py decision collect \
  --prepared data/decision_20261029 --config configs/experiment.yaml \
  --splits train dev --repetitions 5 \
  --output runs/decision_pairs_v1 --dry-run
```

去掉 `--dry-run` 后会调用配置中的 Target、Router 和 Attacker：

```bash
/tmp/minipim-a4-test-env/bin/python main.py decision collect \
  --prepared data/decision_20261029 --config configs/experiment.yaml \
  --splits train dev --repetitions 5 --output runs/decision_pairs_v1
```

默认 54 个 train/dev 案例的 Target 上限是 594 个 episodes；候选生成和 Router 请求另计。可用 `--max-cases N` 在执行前固定前 N 例做小规模试采；该参数先按指定 split 顺序合并再截断，若 N 很小可能只包含 train，因此不能据此评价 dev。

每例先生成新 P0，执行来源 H1；有效首败时，仅依据 H1 生成唯一 P1。合法上下文和两个候选在未来执行前冻结。随后交错执行 P0/P1 各 K 次；成功不提前停止，错误保留 null，禁止隐式补跑或覆盖输出。初始成功、初始错误、生成失败均留在计数中。

沿用原版 Attacker 的现有 benchmark feedback：它仍能接收等量的 security/utility。**训练模块的输入不含这些值。** 所以这不是新实现的无 Gold 黑盒攻击设置。

`pairs.jsonl` 中每条记录包括 schema_version、kind、pair_id、base_task_id、group_id、split、status、context、outcomes、payloads、protocol。Gold 仅出现在 outcomes 的标签侧，原始评价资料独立存放于 raw。全部文件可追溯到来源历史和冻结候选。

## 3. 训练与开发集评价

```bash
/tmp/minipim-a4-test-env/bin/python main.py decision train \
  --data runs/decision_pairs_v1/pairs.jsonl \
  --output models/decision/model.json

/tmp/minipim-a4-test-env/bin/python main.py decision evaluate \
  --model models/decision/model.json \
  --data runs/decision_pairs_v1/pairs.jsonl --split dev \
  --output reports/decision_dev_v1.json
```

训练只读取 train 标签；开发评价拒绝训练组重叠。多个采集文件可同时传入 `--data`，但协议、重复数必须一致，pair ID 不得重复。两臂共同完整的样本用于拟合/评分；缺失记录及比例仍报告，完整案例选择可能产生偏差。

评价包含逐执行 Brier、所选动作的观测成功率、Always Retry / Always Revise 比较、任务与组的宏平均。不能把这些离线固定候选指标当作端到端 ASR，也不报告测后选最优候选造成偏高的 oracle regret。单类标签、全部相同特征或两臂结果一致会明确告警。

模型、阈值等在开发阶段固定后，再单独采集 test 并执行 `evaluate --split test`。当前 test 太小，不能形成稳健效果结论。

## 4. 接入现有实验

`experiments/learned_controller.example.yaml` 使用上述真实模型路径，并保留五个现有基线：

```bash
/tmp/minipim-a4-test-env/bin/python main.py freeze \
  --config experiments/learned_controller.example.yaml \
  --output configs/learned_controller.v1.lock.json

/tmp/minipim-a4-test-env/bin/python main.py run \
  --frozen configs/learned_controller.v1.lock.json \
  --output runs/learned_controller_v1
```

需要先完成真实标签采集与训练，示例配置才能加载。模型文件被纳入现有 freeze 指纹，Target/Attacker/观察协议改变会被拒绝。合成模型即使设置 allow_synthetic，也不能用于 live AgentDojo。正式 test 仍需现有的来源与策略重合审计。

## 工程验证

```bash
/tmp/minipim-a4-test-env/bin/python main.py decision smoke \
  --output reports/decision_smoke_new
/tmp/minipim-a4-test-env/bin/python -m unittest discover -s tests -v
```

烟测通过合成标签执行训练、序列化、持出评分和真正 Runner 中的 learned policy；不进行网络调用。输出目录不可复用。实施假设与审查记录位于 `implement-stage/trainable-decision/`。
