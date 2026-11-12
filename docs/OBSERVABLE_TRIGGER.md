# 可观察必要行为与五例验证

本轮恢复使用原规则 StageAware；可训练 Controller 不启用。

## Trigger 范围

执行前，根据公开目标对应的 goal_spec、攻击者资源授权以及启用查询，投影必要行为。静态不可观察步骤保留审计记录但不参与 Trigger。依赖条件不可观察时，对应必要条件也不纳入。主测试配置强制至少一个可观察必要条件，不能关闭；开发试验可显式启用同一门槛。

例如 c000 仅判断攻击者授权邮箱收到邮件，隐藏的读取、删除行为不判定。c002 的网站访问只是发送链接目标的相关现象，不是必要行为，因此不进入本轮样本。

完整在线事件覆盖认证通过时，T/F 仅回答本轮授权接收端是否出现局部事件。旧记录、离线投影、未认证覆盖、捕获错误不因此获得负证据。局部 Trigger 并不等于完整攻击目标 Gold。

## 本轮预先固定协议

- 来源：data/dev.jsonl，按 case_id 排序，选前5个有合法可观察必要行为的任务。
- 固定任务：c000、c001、c004、c005、c006。
- 无必要可观察行为排除：c002、c003、c008、c009；c007超过预定5例上限。
- 这些任务已被使用过，定位为功能与机制验证，不标记为独立未见测试集。
- 两组共享新生成的 P0 与首次 Target 执行 H1。
- 原 StageAware 使用局部 Trigger；对照 StageAwareNoEvidence 使用同一个原 Controller，将其可见的 Trigger 和逐条件状态遮蔽为 U。
- 后续预算每组最多3次 Target执行；两组使用相同重试上限及停止规则，保留 Payload 身份；即使修订生成了相同文本也保持严格对照。
- 5例最多35次 Target执行；不因失败/错误替换样本或静默重采。
- 同一历史额外离线计算屏蔽证据及 LabelBased 的动作，不运行反事实 Target，也不把这些影子动作作为成功率结果。

五例在独立进程运行；每例两组仍使用相同 Runner，按既有随机顺序执行。Target、Router、Attacker配置及停止规则沿用原实验。原 Attacker 不接收证据包，因此测试的是证据通过 Controller 决策的作用。

本轮使用固定查询接口，不训练或验证主动 Evidence 查询采样策略。结果需分别报告合法证据覆盖、同历史动作变化、实际两组Gold与成本；单次五例差异不能证明稳定的总体收益。

## 入口

```bash
/tmp/minipim-a4-test-env/bin/python main.py freeze --config experiments/observable_pilot5_20261029.yaml --output configs/observable_pilot5_20261029.lock.json
/tmp/minipim-a4-test-env/bin/python scripts/run_observable_pilot5_20261029.py --frozen configs/observable_pilot5_20261029.lock.json --output runs/observable_pilot5_20261029 --dry-run
/tmp/minipim-a4-test-env/bin/python scripts/run_observable_pilot5_20261029.py --frozen configs/observable_pilot5_20261029.lock.json --output runs/observable_pilot5_20261029
/tmp/minipim-a4-test-env/bin/python scripts/analyze_observable_pilot5_20261029.py --run runs/observable_pilot5_20261029 --output reports/observable_pilot5_results_20261029
```

已存在的冻结文件、运行目录不得覆盖。真实调用通过现有凭证加载入口完成，凭证不写入结果。
