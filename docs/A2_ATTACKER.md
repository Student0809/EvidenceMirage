# MiniPIM A2 接入 Evidence PIM

A2 使用与当前原版相同的 Router、Attacker 模型与角色、black-box 历史和 Evidence 决策接口。唯一的生成材料变化是：每个 Router 选中的策略保留完整方法正文，只附最多两条选定原始案例。Router 仍看完整原库的路由摘要，因此本次不压缩 Router 输入。默认 `original` 配置继续保留。

## 已准备资源与实验

- `strategy_library_a2/library.json`：从本项目现有 10 份原始 Markdown 解析的完整方法及 100 条案例；保留方法和案例原文。MiniPIM 的选择、分段和转换代码来源保存在同目录的 provenance/upstream 中；历史源码哈希仅留作旧档案，不再生成或校验。
- `data/a2/dev.retrieval.json`：10 个 dev 任务的 BGE 得分、各策略选例、回退层级和来源。五个 observable pilot 任务可直接复用对应公开任务的记录。
- `experiments/a2_native_dev.yaml`：全部 10 个 dev 任务、现有五种策略。
- `experiments/a2_observable_pilot5.yaml`：当前 5 个可观察任务，StageAware 与屏蔽证据 Controller 共用新生成的 P0/H1；首次之后每臂最多 3 次 Target 执行。
- `configs/a2_native_dev_20261029.lock.json` 和 `configs/a2_observable_pilot5_20261029.lock.json`：本次接入的冻结配置。

这些任务已有评价历史，仅用于开发实验，不是独立 held-out 测试。

在项目根目录执行离线预检：

```bash
/tmp/minipim-a4-test-env/bin/python main.py preflight --config experiments/a2_observable_pilot5.yaml
```

后续正式启动这组开发实验时执行（会调用 Router、Attacker 和 Target）：

```bash
/tmp/minipim-a4-test-env/bin/python main.py run \
  --frozen configs/a2_observable_pilot5_20261029.lock.json \
  --output runs/a2_observable_pilot5_20261029
```

数据资源、配置或运行环境变化后须重新 freeze，使用新的 lock 和输出目录。Python 源码不再生成或校验哈希；旧清单中的 `.py` 项会被忽略。不要把旧 original 的 P0/H1 当作 A2 的首轮结果；A2 每次新运行会重新路由、生成共享 P0。

## 选择与冻结

离线准备沿用 MiniPIM A2 的规则：先排除同任务组，再依次尝试同场景同用户任务类型、同类型任意场景、该策略全部案例；扩大范围后重新排名。相似度由本地 `bge-base-en-v1.5` 对“用户任务＋注入位置上下文”编码，归一化向量点积排序；编码器最多 512 tokens，案例正文不截断。原始案例的结构化结果保持 unknown，不将档案叙述当作新成功率证据。两个来源策略各只有一个案例，记录不足而不重复填充。

选例针对每个公开任务预先计算，并在各策略分支及修订轮次间保持不变。运行时记录选中案例 ID、得分、回退层级、文档和资源哈希及字符数。所有分支沿用 Runner 的历史隔离；合法 Evidence 继续只影响外层 Retry/Revise 决策。

验证阶段检查完整 Router 库、原文重建、选例规则、公开任务哈希和资源依赖。冻结清单覆盖整个原库、角色、A2 资源、审核标签及非 Python 数据快照；不包含准备代码或其他 `.py` 文件。可训练决策模块的配对采集也纳入相同 A2 资源指纹。

## 新任务的离线准备

在线实验不需要安装 BGE，也不读取相邻 MiniPIM 项目。仅准备新任务时需要本地 embedding 模型和 `requirements-a2.txt` 里的依赖。本机现有 harness 环境已可使用。

为新数据集准备与 `data/a2/dev.annotations.json` 同格式的标签文件：每条包含 case_id、任务组、用户任务类型、原文引文、reviewer、公开字段哈希和数据集来源哈希/行号。标签依据公开用户需求；缺失或不一致时拒绝准备，不静默猜测。

```bash
/home/zhengqingliu/miniconda3/envs/harness/bin/python scripts/prepare_a2.py \
  --dataset data/new.jsonl \
  --task-annotations data/a2/new.annotations.json \
  --retrieval data/a2/new.retrieval.json \
  --reuse-library
```

脚本只用本地 CPU，不下载模型、不调用外部生成模型，拒绝覆盖旧产物。`--reuse-library` 比较数据与策略内容时忽略旧源码哈希，并继续引用旧库文件的原始字节和资源哈希，不重写旧库。新配置的 attacker 填入：

```yaml
attacker:
  mode: a2
  model: deepseek-flash[1m]
  effort: max
  timeout: 900
  a2_library: strategy_library_a2/library.json
  a2_retrieval: data/a2/new.retrieval.json
```

## 本次验证

180 项离线测试通过，包括 13 项 A2 专项测试；10 例开发配置和 5 例 Evidence 配置的原生离线预检通过。记录见 `reports/a2_tests_20261029.log` 与 `reports/a2_integration_20261029.json`。

用既有五例 Router 选择离线组装首轮 Prompt，A2 正文字符数减少 44.5%–70.1%。这不是在线模型 token、延迟或 ASR 对照。本次未发起在线 Router、Attacker 或 Target 调用。

历史接入记录曾报告 `src/vendor/attacker_observer.py` 的源码哈希漂移。当前不再检查 Python 源码哈希；数据集、策略正文、角色、审核标签、模型权重、资源清单、payload 与证据的哈希校验继续保留。
