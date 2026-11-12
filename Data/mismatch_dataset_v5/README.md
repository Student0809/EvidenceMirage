# V5：54 条受控失配修订

[M3 中文改写速览](/mnt/data/users/zhengqingliu/PIMiner_evidence/Data/mismatch_dataset_v5/M3_REVISION_ZH.md) · [完整审阅](/mnt/data/users/zhengqingliu/PIMiner_evidence/Data/mismatch_dataset_v5/REVIEW.md) · [JSONL](/mnt/data/users/zhengqingliu/PIMiner_evidence/Data/mismatch_dataset_v5/dataset.jsonl) · [标注规则](/mnt/data/users/zhengqingliu/PIMiner_evidence/Data/mismatch_dataset_v5/ANNOTATION_GUIDE.md) · [评测与防泄漏](/mnt/data/users/zhengqingliu/PIMiner_evidence/Data/mismatch_dataset_v5/EVALUATION_PROTOCOL.md)

18 个种子，54 条顶层记录，126 个逐回复执行配对标注。46 个冻结执行文件与旧版 M1 未改变。全部是 Controlled Mismatch Set；Naturalistic 暂为空。

复现：`python scripts/revise_mismatch_all18_v5_20261104.py`。脚本仅读取旧数据，输出本目录；不执行 AgentDojo 工具或 Target。
