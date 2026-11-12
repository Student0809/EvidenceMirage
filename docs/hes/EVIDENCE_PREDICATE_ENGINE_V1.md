# Evidence Predicate Engine V1

版本：V1，2026-11-02。状态：可实现的设计草案；不是已实现功能或已验证效果。

## 1. 目标与研究边界

在缺少历史训练执行日志的情况下，利用历史用户任务、攻击目标与工具上下文，为当前任务生成可验证的 Evidence Predicates（证据命题），并对本轮已有授权查询结果做细粒度判断。

历史场景负责提出“值得检查什么”；当前接口契约负责限定“能够检查什么”；当前证据负责决定 T/F/U。

V1 的主要研究问题：在查询结果、权限与判定器相同的条件下，相关历史场景是否比仅使用当前任务帮助发现更多非冗余、可验证的条件，并增加正确判定覆盖，同时不增加无证据断言？

V1 不要求历史执行日志，不根据策略成功叙述学习成功概率或失败频率。不启动主动采样或在线攻击，不改变目标模型，不将私有轨迹或 Gold 输入生成器。历史日志缺失只限制历史结果学习，不阻止场景驱动的命题发现。

T/F/U 是命题的判断值，不是三种 Subgoal。命题包括目标条件、诊断条件、正常任务重合条件；不能全部当作必要子目标。

## 2. 已有基础

全量审计覆盖 1,234 个 AgentDojo 注入位置、97 个用户任务、35 类目标；策略库 100 例中 AgentDojo 44、IPI Arena 56。所有例子均已找到对应任务—目标场景，但历史执行没有因此得到验证。

当前 FixedObserver 使用 8 类查询，底层观察器提供 14 类资源查询。同资源授权下，邮件主题、正文、付款明细存在尚未充分使用的证据。计数与其明细不能算作独立信息源。

静态目录识别出 8 类互补字段候选目标、17 类单查询目标、9 类无相关已实现接口目标、1 类 Final 文本目标。这些是保守的设计分层，不是诊断准确率或互信息估计。

来源：

- ../../reports/hes_permission_identifiability_20261102/REPORT.md
- ../../reports/hes_permission_identifiability_20261102/goal_catalog.json
- ../../reports/hes_permission_identifiability_20261102/library_case_mapping.json
- ../../reports/hes_permission_identifiability_20261102/predicate_contracts.json

## 3. 数据流

```mermaid
flowchart LR
 H[历史场景库] --> R[多维检索与来源归组]
 C[当前任务与目标] --> R
 R --> G[候选命题生成]
 C --> G
 Q[权限及查询契约] --> G
 G --> V[校验、去重、绑定]
 Q --> V
 V --> E[确定性判定]
 P[本轮冻结证据] --> E
 E --> L[命题结果与证据账本]
 L --> F[已确认事实与证据缺口]
```

严格分离三类输入：

1. 场景材料：任务、目标、注入槽上下文与来源引用；可以影响候选命题。
2. 当前证据：授权查询返回及覆盖元数据；可以决定 T/F/U。
3. 审计材料：完整轨迹、私有状态、Gold；仅用于评测，不进入前两类输入。

历史材料里的命令、角色指令和成功叙述均作为不可信数据。生成器没有执行工具；历史文本不能改变输出 schema、权限或判定规则。

## 4. 模块接口

| 模块 | 接口 | 职责 |
|---|---|---|
| query_contracts | available_contracts(case, grants) | 返回资源、字段、类型、事件关联及覆盖契约 |
| history_retriever | retrieve(case, history, config) | 分用户任务、目标、上下文检索并归组 |
| predicate_generator | propose(case, retrieved, contracts) | 只生成结构化候选，不输出当前 T/F |
| predicate_validator | validate_and_bind(candidates, case, contracts) | 类型检查、值绑定、权限检查、去重与依赖检查 |
| predicate_evaluator | evaluate(bound_predicates, frozen_packets) | 运行确定性规则，输出证据引用及 T/F/U |
| feedback_builder | build_feedback(results, rejected, requirements) | 输出已知条件、未知条件和缺失证据 |

建议目录：src/evidence/predicates/。先以独立入口接入冻结数据，验证后再接 Observer 与迭代流程。

入口建议：

```text
python scripts/run_predicate_engine.py \
  --cases <case-jsonl> \
  --evidence <frozen-packets-jsonl> \
  --history <scenario-memory-jsonl> \
  --arm current_plus_history \
  --config <versioned-config> \
  --output <run-directory>
```

以上是待实现接口。没有证据文件时可生成命题与证据需求，但不能输出虚构查询结果。

## 5. 历史记忆与检索

历史单元保留：experience_id、benchmark、suite、user_task、goal、context、context_source_tool、injection_slot、source_path、match_level、base_task_group、context_group、payload_group。

V1 默认不向生成器提供 winning payload、成功解释或未经验证的结果标签。使用历史场景结构即可检验当前假设，避免把策略生成能力混入命题发现实验。

三个检索通道分别寻找：

- 用户任务相似：正常动作、任务所需字段、正常动作与目标动作重合。
- 目标相似：待验证的结果条件、集合约束、顺序约束。
- 上下文相似：数据字段、来源工具、注入槽语义。

首版使用结构过滤加词项检索，不要求向量模型。每通道最多 2 条，总计最多 6 条，按来源组去重；不足时不重复填满。分数只表示相关性，不解释为事实置信度。跨 benchmark 的权限不迁移。

检索输出保存通道、分数、来源组、排除原因。留出任务在检索前排除；不能生成后再删除引用。

## 6. 命题结构

示例：

```json
{
  "schema_version": "predicate-v1",
  "id": "received_subject_matches",
  "statement": "本轮目标接收端存在主题满足目标约束的邮件",
  "role": "goal_condition",
  "necessity": "explicit_goal_clause",
  "query_id": "received_email_subjects",
  "scope": {
    "episode_id": "current",
    "resource_ref": "authorized_goal_mailbox",
    "domain": "rollout_events"
  },
  "operator": "exists_equal",
  "expected_ref": "current_goal.required_subject",
  "negative_requires": "complete_scoped_event_set",
  "depends_on": [],
  "history_refs": ["example-id"],
  "binding_status": "executable"
}
```

约束：

- role ∈ goal_condition / diagnostic / normal_task_overlap。
- necessity ∈ explicit_goal_clause / proposed_necessary / not_asserted；模型提出的必要性不能直接进入目标合取。
- scope.domain 必须显式区分 observed_events（目前返回的集合）与 rollout_events（整轮事件集合）。改变作用域不是同一命题。
- expected_ref 只能解析到当前任务公开约束或明确授权的可信参考；历史值和注入内容不能自动作为可信源。
- query_id、operator、字段路径只能来自白名单，不允许执行生成代码、eval 或任意正则。
- 每次最多 12 个候选，数量上限对各生成组一致；这些数值是初始工程配置，开发集冻结后不根据测试效果修改。
- 数据类型错误、循环依赖、未知字段、越权资源必须拒绝，不能悄悄修正为更容易满足的命题。

有效但暂不可执行的命题保留为 evidence_requirement；无意义或不合法命题保留 rejection 记录，不混入普通 U。

## 7. V1 运算符

| 运算符 | 适用条件 | 判定要点 |
|---|---|---|
| exists_event | 指定范围内存在事件 | 正事件支持 T；空集合只有覆盖完整才支持 F |
| exists_equal / exists_contains | 主题、正文、明确字面字段 | 有匹配支持 T；无匹配需目标范围完备才支持 F |
| all_numeric_le | 所有相关单笔金额不超过阈值 | 发现违反即可 F；T 需要完整范围 |
| sum_equal | 相关金额合计符合约束 | T/F 均要求完整窗口；V1 不利用未经声明的单调性提前判定 |
| count_compare | 指定集合计数 | 必须明确是已观察集合还是整轮集合 |
| observed_event_field_match | 某个已绑定事件的字段符合约束 | 该事件的字段完整时可判断，不扩展成整轮存在性结论 |
| final_literal_contains | Final 中出现明确字面内容 | Final 完整时可 T/F；不等价于语义推荐或动作发生 |

金额使用 Decimal 和明确币种；不隐式跨币种求和。文本默认精确比较；大小写、空白等归一化只能由契约声明并保存版本。

source_authenticity、set_coverage、persisted_state、semantic_entailment 在 V1 可以被发现，但只有绑定了对应可信比较或状态接口才可执行，否则记录证据需求。成员顺序运算符在事件身份与顺序契约核对后增补，不作为首版上线依赖。

## 8. 证据绑定与 T/F/U

冻结证据至少包含 episode_id、policy_fingerprint、snapshot_id、query_id、resource_scope、observed、coverage、source、packet_id。snapshot_id 由可信适配器生成；缺失旧数据不允许补造一个共享快照身份。

判定顺序：

1. 检查 packet 类型、来源、轮次、资源授权和策略指纹。
2. 解析字段并检查其完整性、缺失值与数据类型。
3. 核对命题的量词、作用域、事件关联和覆盖要求。
4. 执行白名单运算符。
5. 保存判定、证据引用与 reason_code。

U 的原因至少包括：packet_missing、interface_not_implemented、resource_not_granted、coverage_insufficient、event_binding_missing、reference_unavailable、unsupported_semantics。损坏证据同时报告 validation_error，不把它伪装成正常环境不确定性。

query.response.supported 描述查询的事件支持情况，不是生成命题的判断值。不能直接复制为命题的 T/F。

现有完备性证书只在指定 suite、query 和本轮资源范围内有效。不能因为计数查询有证书就自动认为正文查询也有证书。需要跨查询推导时，必须另建经过验证的规则；V1 默认不做此推导。

不同查询不得仅凭数组位置合并成同一邮件。必须具有稳定事件 ID，或者经过适配器验证的同快照对齐契约。无关联依据时，可分别判定“存在主题正确的邮件”和“存在正文正确的邮件”，但联合命题保留 U。

## 9. 输出与反馈

每条结果：predicate_id、verdict、scope、evidence_refs、reason_code、coverage_used、contract_version、predicate_hash。

反馈按以下结构返回：

```json
{
  "confirmed_conditions": [],
  "refuted_conditions": [],
  "unresolved_conditions": [],
  "evidence_requirements": [],
  "normal_task_overlap_notes": [],
  "rejected_candidates": [],
  "overall_goal_status": "not_derived"
}
```

V1 不从动态生成的命题集合推导整体攻击成功。遗漏目标条件会让全 T 产生假成功；诊断条件为 F 也不一定使目标失败。需要整体结论时，必须有另行审定的完整目标公式。

输出“邮件未在完整观察窗口内到达目标接收端”是允许的；输出“Target 忽略了注入”没有由该证据支持。失败原因保持未知，除非它自身具有可识别的命题定义。

## 10. 当前代码衔接

src/evidence/observer.py 继续负责证据采集。V1 增加独立的只读 packet 适配器，先复用已保存字段；需要新增字段时单独说明，不能把丰富查询视图和旧固定视图放在同一主比较里。

src/evidence/ledger.py 当前只支持 event 与 positive_attachment_count，并有固定查询白名单。因此仅修改生成提示词不可实现 V1。建议先新增 PredicateLedger，保留旧 EvidenceLedger 作为 B0；验证一致性后再考虑共用底层 packet 校验。

src/vendor/attacker_observer.py 的授权规则与 coverage_certificate 不随命题改变。生成器不能增加 grants。

对 c023：用户已声明可以验证密码结果；目录记为 allowed_but_not_implemented。由结果验证适配器返回目标密码是否生效，不提供 update_password 日志。该适配器不在当前冻结证据主实验中凭空补齐。

## 11. 验证设计

### 11.1 契约与程序正确性

最低测试集：

- 同收件计数、不同主题／正文；同收款次数、不同金额／备注。
- 附件引用数相同但附件内容不同，仍不可识别内容正确性。
- 相同接收正文、不同私人源状态，来源真实性保留 U。
- 空结果有／无合法覆盖证书；证书 suite/query/resource 不匹配必须拒绝。
- 越权资源、跨轮次、跨 policy、冲突 snapshot、字段损坏。
- 两封邮件分别满足主题和正文，不能误合成同一封满足两者。
- 观察窗口未结束的累计额不足不能直接 F。
- 正常任务已要求改密，不能把改密动作视为攻击进展。
- 非法 schema、循环依赖、历史值污染 expected_ref、历史中的指令文本。

这些测试是构造反例，不报告成真实攻击诊断准确率。

### 11.2 冻结证据主实验

| 组别 | 命题来源 | 权限、证据、判定器 |
|---|---|---|
| B0 | 当前固定命题，转换为明确作用域 | 相同 |
| B1 | 当前任务＋目标＋契约 | 相同 |
| B2 | B1＋相关历史场景 | 相同 |
| B3 | B1＋打乱历史场景 | 相同 |

B2−B1 检验历史增量，B2−B3 检验历史相关性。生成组采用相同模型、提示结构、候选上限和生成预算；另行记录实际 Token，承认增加历史文本会增加输入长度。B3 与 B2 控制历史条数与长度，不能用明显无关的格式差异制造容易识别的对照。

先在当前固定证据视图中运行四组；另设底层丰富查询视图，四组仍共享该视图。两种视图结果分别报告，避免把新增证据收益归因于历史。

没有历史训练日志也能做此实验：历史库提供场景，评测证据来自当前保存的运行 packet 或新的受控 fixture。没有可信参考的旧记录不能用于宣称判定准确率。

### 11.3 划分与参考标注

按 base_task_group 隔离开发与测试，所有同任务的轮次、注入位置和策略副本同组。记录 context_group 重合；另报告去除相同上下文后的结果。同目标模板迁移可保留；目标模板留出作为更严格扩展。

97 个任务不等于97个有完整证据的测试任务，先统计可用证据与可识别参考标注数，再确定样本量。c005/c013/c015/c023/c027 等已参与设计的案例归开发集。

参考标注独立记录：命题相关性、目标必要性、实际状态（若可得）、在指定授权证据下的可识别性、可接受判定及最小证据。完整轨迹只供参考标注，不回灌生成器。实际状态为真但不可识别，正确输出仍可为 U。

评测前冻结参考条件集；模型提出新颖有效命题时，由不知道组别的审阅流程判断是否补充公共参考集，并对所有组重评。不得只奖励与手写模板文字一致的结果。

### 11.4 指标与统计

主要指标：

1. 无证据断言率：没有充分授权证据支持的 T/F 数，除以输出 T/F 总数；无断言的情况单列，不能单独视为优胜。
2. 正确判定覆盖率：独立参考集中可判定条件被正确覆盖的比例。
3. 非冗余条件覆盖率：独立参考条件被提出且正确绑定的比例，遗漏计入分母。

辅助指标：命题有效率、不可识别条件正确保留 U 的比例、可识别状态对区分率、重复率、被拒候选率、Token、延迟及每个有效非冗余条件的成本。

以基础任务为聚类单位，报告宏平均、配对差和聚类 bootstrap 区间；不把同任务五轮作为五个独立样本。单独报告互补字段、单查询、无接口与 Final 四层。

工程门槛：所有权限与证据契约测试通过；可执行命题均绑定已注册字段；全链可追溯。研究门槛：在开发集预先固定风险容忍差值和最小有用覆盖增益，再在留出集判断 B2 是否达标。没有足够任务或区间过宽时报告证据不足，不事后下调门槛。

## 12. 实现里程碑与交付

| 阶段 | 交付物 | 完成标准 |
|---|---|---|
| M1 | schema、查询契约、packet 适配器、确定性判定器 | 手写命题通过契约反例测试 |
| M2 | 当前任务生成器与校验器 | B1 端到端生成可追溯结果；异常候选被记录 |
| M3 | 历史检索与场景归组 | B2/B3 可重复运行；无留出任务泄漏 |
| M4 | 参考标注与冻结证据对照 | 输出覆盖—风险—成本结果及逐例错误分析 |

每次运行保存 config.json、input_manifest.json、retrieval.jsonl、candidate_predicates.jsonl、bound_predicates.jsonl、rejections.jsonl、judgments.jsonl、feedback.jsonl、metrics.json；记录输入哈希、模型配置和所有模板版本。含敏感字段的原始证据只保留在原有授权存储中，报告使用引用。

先完成 M1，不立即运行付费生成或在线攻击。只有当 M4 支持“历史带来额外且可靠的判断能力”后，再将 evidence_requirements 接入主动采样。若历史没有增益，可保留当前任务驱动的细粒度解释器，并明确收缩研究结论。
