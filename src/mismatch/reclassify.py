"""Add a checked research-condition label without rewriting frozen run evidence."""
import argparse
import json
from pathlib import Path
from .data import digest, write_json
from ..feedback.ablation import ABLATION_PROTOCOL


CLASSIFICATION_FILE = 'EXPERIMENT_CLASSIFICATION.json'


def classify_legacy_g00(run):
    run = Path(run).resolve()
    manifest = json.loads((run / 'manifest.json').read_text())
    config = manifest['ablation']
    if (config.get('protocol') != 'assessment_piminer_library_v2'
            or config.get('group') != 'G00'
            or config.get('historical_experience') is not False
            or config.get('structured_assessment') is not False
            or manifest.get('feedback_view') != 'authorized_evidence'):
        raise ValueError('Expected legacy ordinary-assessment G00 with tool evidence and no library')
    status = json.loads((run / 'status.json').read_text())
    metrics = json.loads((run / 'metrics.json').read_text())
    if status['status'] not in ('completed', 'completed_with_errors'):
        raise ValueError('Run is not complete')
    if metrics['ablation'] != config or metrics['missing_instances']:
        raise ValueError('Incomplete or inconsistent metrics')
    receipt = json.loads((run / 'scoring_receipt.json').read_text())
    if digest(run / 'metrics.json') != receipt['metrics_sha256']:
        raise ValueError('Metrics checksum mismatch')
    branches = set()
    for row in status['results']:
        folder = run / 'branches' / row['branch']
        branches.add(row['branch'])
        frozen_path = folder / 'frozen.json'
        if digest(frozen_path) != receipt['inputs'][str(frozen_path.relative_to(run))]:
            raise ValueError('Frozen manifest checksum mismatch')
        frozen = json.loads(frozen_path.read_text())
        for name, checksum in frozen['files'].items():
            if digest(folder / name) != checksum:
                raise ValueError('Frozen evidence checksum mismatch: ' + str(folder / name))
        job = json.loads((folder / 'job_public.json').read_text())
        if job['ablation'] != 'G00' or job['view'] != 'authorized_evidence' or row['ablation'] != config:
            raise ValueError('Branch intervention differs from run')
        for path in sorted((folder / 'calls').glob('*.json')):
            call = json.loads(path.read_text())
            if call['stage'] == 'assessment':
                data = json.loads(call['prompt'].split('PUBLIC INPUT:\n', 1)[1])
                if data.get('assessment_mode') != 'ordinary':
                    raise ValueError('Assessment format differs')
    if len(branches) != metrics['planned_instances'] or branches != {r['branch'] for r in metrics['rows']}:
        raise ValueError('Branch coverage differs')
    record = dict(schema_version=1, kind='research_condition_reclassification',
        original_protocol=config['protocol'], original_group='G00',
        classification_protocol=ABLATION_PROTOCOL, group='G10',
        tool_evidence=True, historical_experience=False, assessment_mode='ordinary',
        reason='A denotes extra tool evidence visible to the attacker, not assessment format.',
        frozen_artifacts_unchanged=True, recovery_compatible=False,
        metrics_sha256=digest(run / 'metrics.json'),
        manifest_sha256=digest(run / 'manifest.json'),
        scoring_receipt_sha256=digest(run / 'scoring_receipt.json'),
        instances=metrics['planned_instances'], summary=metrics['all'])
    path = run / CLASSIFICATION_FILE
    if path.exists():
        if json.loads(path.read_text()) != record:
            raise ValueError('Existing classification differs')
    else:
        write_json(path, record)
    report = run / 'RESULTS_G10.md'
    counts = metrics['all']['online_success_count']
    n = metrics['planned_instances']
    report_text = (
        '# G10（A1P0）18 例运行重分类\n\n'
        'A=攻击侧额外工具证据可见，P=PIMiner 历史策略库。'
        '本运行使用 ordinary 判断，工具证据开启、历史策略库关闭。\n\n'
        '| 研究组别 | 样本数 | ASR@1 | ASR@3 | ASR@5 | 保留轨迹 Target 次数 |\n'
        '|---|---:|---:|---:|---:|---:|\n'
        f"| G10 | {n} | {counts['1']}/{n} | {counts['3']}/{n} | {counts['5']}/{n} "
        f"| {metrics['all']['target_dispatches']} |\n\n"
        '原目录名及冻结文件中的 G00 属于旧 assessment_piminer_library_v2 定义，'
        '不能作为新协议的 G00。此次仅修正研究条件标签，没有重跑、重评分或修改冻结数据。'
        '保留轨迹之外的历史失败与恢复开销须另计。\n\n'
        '[重分类及校验记录](EXPERIMENT_CLASSIFICATION.json) · '
        '[原始逐条报告（旧组名）](RESULTS.md) · [原始评分](metrics.json)\n'
    )
    if report.exists() and report.read_text() != report_text:
        raise ValueError('Existing classification report differs')
    if not report.exists():
        report.write_text(report_text)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    args = parser.parse_args()
    record = classify_legacy_g00(args.run)
    print(json.dumps(dict(group=record['group'], instances=record['instances'],
                          classification=str(args.run / CLASSIFICATION_FILE))))


if __name__ == '__main__':
    main()
