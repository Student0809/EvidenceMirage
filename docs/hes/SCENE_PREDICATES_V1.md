# 历史场景驱动的证据命题 V1：使用说明

实现状态：历史条件抽取、三通道检索、候选命题生成、接口编译和冻结证据判定已接通。支持 rules 与 model 两个后端；rules 可独立自动运行，model 使用项目现有的无工具 CLI transport。模型后端已通过受控 JSON 接口测试；补齐 DeepSeek 环境初始化后，c013 的真实模型生成已成功。尚未验证语义准确率或历史增益。

## 两种后端的区别

- rules：透明的确定性基线，按当前目标中的规则提取条件，检索历史并提供对应引用。它不是开放式发现，也不能用来证明历史增量；移除历史后，其命题集合通常不变。
- model：历史抽取阶段允许开放式 concept；在线阶段将检索场景与条件输入模型，生成语义命题和建议绑定。模型可能提出规则表之外的新命题。生成结果必须有当前目标引文、有效历史引用和符合白名单的绑定。

两者都不需要人工逐例填写候选命题。真正检验“历史让模型发现了更多有效条件”需要 model 组与 without-history 对照，并独立评估语义质量；规则回放不回答这个问题。

## 入口

构建全部历史场景的规则记忆：

```bash
python scripts/run_scene_predicates.py build-memory \
  --output /tmp/hes-memory-rules
```

默认输入 strategy_library_compact/experiences.jsonl，处理全部100例。仅提取 user_task、injection_goal、placeholder_context 与场景标识；不读取 winning payload、成功解释或 Gold。不会把后来补建的上下文当成已验证历史执行；此版本使用 compact 内的文本视图，不自动补写缺失历史上下文。

使用模型抽取历史知识（先配置有效 CLI 登录及模型路由）：

```bash
python scripts/run_scene_predicates.py build-memory --backend model \
  --model '<configured-model>' --effort medium \
  --cache /tmp/hes-json-cache --output /tmp/hes-memory-model
```

每例最多一次生成调用，成功 JSON 按模型身份、系统提示、阶段提示和输入内容缓存；再次运行需新输出目录，但可复用 cache。抽取结果必须逐项带 exact source_quote；该检查只证明引文存在，不证明抽取语义正确。

运行端到端生成与判定：

```bash
python scripts/run_scene_predicates.py run \
  --memory /tmp/hes-memory-rules/memory.jsonl \
  --input reports/hes_predicate_v1_20261102/inputs/c013.json \
  --output /tmp/hes-c013-rules
```

模型生成可搭配任意已审阅的记忆版本，包括规则抽取记忆：

```bash
python scripts/run_scene_predicates.py run --backend model \
  --model '<configured-model>' --effort medium --cache /tmp/hes-json-cache \
  --memory /tmp/hes-memory-model/memory.jsonl \
  --input reports/hes_predicate_v1_20261102/inputs/c013.json \
  --output /tmp/hes-c013-model
```

`--without-history` 使用同一当前任务生成流程但不提供历史；`--exclude-same-context` 在基础任务排除之外，再排除完全相同归一化上下文。B3 打乱历史的正式实验调度尚未实现。

## 输入与结果

输入 bundle：case、episode_id、policy、packets；可选 excluded_groups。case 的公开字段由 allowlist 提取，不传递 messages、Gold、原攻击 payload 或当前证据给生成器。生成前先验证所有 packet，避免无效证据触发不必要的模型调用。

检索按 user_task、goal、context 分别做 BM25，每通道最多2例；默认排除当前基础任务及输入声明的全部留出组。多策略／同任务副本、相同上下文不重复占位。得分只表示词项相关性，不是成功概率。跨 benchmark 的场景可以作为候选知识，权限仍只能来自当前 policy。

模型语义输出包含：id、concept、statement、current_quote、history_refs、evidence_need、binding。history_refs 逐项绑定 example_id 和 condition_id，必须存在于本次检索结果。binding=null 的命题保留为 U 的证据需求。

编译器只允许当前目标中的字面值产生 expected_ref。生成模型不能提供 current_goal 引用的解析值，也不能将历史验证码复制到当前命题。source_authenticity、set_coverage、persisted_state、event_order 这几类已知高风险概念禁止直接弱化为现有计数或字面存在查询。此检查不是任意语义等价的自动证明；改名或复杂语言仍需要质量评测。

所有生成命题作为 diagnostic 进入引擎，使用 rollout_events 范围；不将动态命题合取成整体成功。M1 暂不支持字段前缀判断、跨事件合并或当前密码结果验证，不能在编译时偷偷换成 contains、数量或调用日志。

输出包括：

- retrieval.json：三个检索通道、分数、选中来源与排除原因。
- semantic_predicates.json：自动生成的语义命题，包括暂不可执行者。
- compilation.json：编译后的命题、当前引用值、拒绝记录、缺失证据与来源链。
- feedback.json：T/F/U、证据引用、缺失接口／覆盖等原因。
- manifest.json：输入哈希与生成器未读取证据的约束。
- result.json：完整结果；模型模式另存 cache 调用及 provider 记账。

无需接口的格式错误进入 rejected_candidates；语义合理但缺少证据或接口的候选保留 U。程序不能仅凭一个有原文引用的模型输出，就证明该命题相关或绑定正确。

## 代码位置

- src/evidence/predicates/history.py：公开场景拆解、原文引用校验、三通道检索。
- src/evidence/predicates/model.py：模型提示、JSON 调用与版本绑定缓存。
- src/evidence/predicates/generation.py：自动命题发现和保守编译。
- src/evidence/predicates/pipeline.py：端到端编排。
- scripts/run_scene_predicates.py：记忆构建与单案例运行。
- scripts/replay_scene_predicates.py：当前30例的首轮冻结证据回放。

## 当前验证结果与限制

38 项测试通过，包括开放式模型输出的受控模拟、引用验证、历史值污染、留出组过滤、来源去重、缓存隔离、模型输入不含证据、编译及原判定器回归。

已用100例规则记忆，对 test30 每例第一条保存执行回放：30/30 有可用观察；生成106条语义命题，25条绑定候选得到 T=3、F=16、U=6；另81条未绑定需求保留 U。没有使用真实标签评估正确性，不能把这些数量当作准确率、成功率或历史增益。

c013 原目标的主题引号不闭合，规则解析保留证据需求而不猜测修正；c023 的密码生效仍需结果验证接口；c005 的附件引用不被冒充成附件内容与源集合覆盖。

最初 smoke 因未加载 DeepSeek 环境而误走过期 OAuth。修复后使用 deepseek-flash[1m] / max 成功生成 c013 的11条候选（1条绑定、10条需求）；结果见 reports/hes_predicate_v1_20261102/c013_deepseek_configured。不是恢复 OAuth，而是正确配置 DeepSeek 端点与原有凭据。

## DeepSeek 启动配置（修复新增入口的环境遗漏）

原有实验启动器会从上游 PIMiner/.env 读取 DEEPSEEK_API_KEY，新入口现在支持相同配置。只在当前进程设置环境，不写入密钥、不加载该文件中的其他凭据：

```bash
/tmp/minipim-a4-test-env/bin/python scripts/run_scene_predicates.py run \
  --backend model --deepseek \
  --deepseek-env /mnt/data/users/zhengqingliu/PIMiner/.env \
  --cache /tmp/hes-deepseek-cache \
  --memory reports/hes_predicate_v1_20261102/memory_rules/memory.jsonl \
  --input reports/hes_predicate_v1_20261102/inputs/c013.json \
  --output /tmp/hes-c013-deepseek
```

`--deepseek` 默认模型 deepseek-flash[1m]、effort=max，并设置 DeepSeek Anthropic 端点、Opus/Sonnet/Haiku 映射、subagent model 和 compact window。优先使用进程 DEEPSEEK_API_KEY，其次仅接受明确指向 DeepSeek 端点的 ANTHROPIC_AUTH_TOKEN，最后才读取指定 env 文件中的 DEEPSEEK_API_KEY。清除本进程冲突的 Anthropic API Key/OAuth Token。缓存身份现在包含端点，避免跨供应商复用。

此前 OAuth 错误来自新入口未初始化 DeepSeek 环境，不能据此认定 DeepSeek API Key 或模型失效。
