# V1 M1 使用说明

后续已补齐历史检索与自动生成代码，最新入口见 [场景驱动 V1 使用说明](SCENE_PREDICATES_V1.md)。本文仅说明底层 M1 接口。

已实现：白名单命题结构、14 类原生资源查询的 packet 校验、冻结证据适配、6 种确定性运算符、逐命题 T/F/U、语法去重、原因代码和独立 CLI。没有模型调用；历史检索与自动命题生成属于后续 M2/M3。

## 运行

在项目根目录执行，输出目录必须尚不存在：

```bash
python scripts/run_predicate_engine.py \
  --input examples/predicate_engine/synthetic_mail.json \
  --output /tmp/predicate-engine-demo
python -m unittest tests.test_predicate_engine
```

样例是合成收件，不是真实攻击结果。预期 subject=T、body=U：没有主题匹配的正文并不能在未认证完整的整轮窗口中证明不存在匹配正文。输出包含 feedback、judgments、rejections、predicates 和 input_manifest。

## 输入契约

JSON bundle 包含 episode_id、policy、references、predicates、packets。policy 使用原生观察器资源结构；所有 packet 必须来自可信 runner。指纹与内容哈希用于绑定和追溯，不是防伪签名；不可让候选生成模型自行填充证据。

references 是调用者提供的当前任务值映射，只接受 current_goal.* 引用。命题不能携带 expected 字面值。命题必须显式声明资源范围 authorized_policy（该 policy 的全部授权资源），不声称能从缺少对象信息的字段列表筛出某个特定收件端。如果任务要求指定单个端点，请提供仅包含该端点的、由可信 runner 生成的 policy/view。

已有 FixedObserver packet 可以直接导入。要使用同权限下的丰富字段，可显式调用 packets_from_view(episode_id, policy, view, queries)；它校验原生 view，并只返回明确请求且已授权的查询。不自动查询或增加权限。

已支持 exists_event、exists_equal、exists_contains、all_numeric_le、sum_equal、count_equal。记录型查询需为字段运算符指定 field，例如 payment_records.amount。sum_equal 针对明细字段，未将聚合 payment_total 作为可求和列表使用。计数运算符检查事件数；附件数量需使用 received_attachment_counts 数值运算。

scope.domain：

- rollout_events：整轮事件；不存在、总和、计数等结论要求 query 自身的完备性证书。
- observed_events：仅当前已返回事件的集合，允许在该有限集合中给出 F；不可解读为整轮失败。

当前不执行跨查询合并、依赖、Final 语义、整体目标合取、对象身份推断或密码验证。JSON 中这些未实现字段会被拒绝，而不是静默忽略。未知接口产生 interface_not_implemented，已知但未授权接口产生 resource_not_granted。

与设计稿的阶段差异：M1 采用更小的严格 schema，暂不接受 necessity、depends_on、negative_requires、binding_status；否定规则由运算符和 scope 推导。scope 使用实际 episode_id 和 authorized_policy，不解析 current 或自定义资源引用。样例是当前可运行格式。

单一冻结查询有冲突 packet 时整次导入失败；不同查询的结果保持独立，不制造 snapshot_id 或数组对齐。模型可生成候选，但后续接入时仍必须通过此白名单校验。
