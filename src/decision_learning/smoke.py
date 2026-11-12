"""Small synthetic engineering fixture: fitting, serialization, and real Runner integration."""
from copy import deepcopy
import json
from pathlib import Path

from ..adapters.fixture import FixtureAttacker, FixtureTarget
from ..engine.config import ROOT, load_config, load_cases
from ..engine.history import EvidenceSummary, History, ObservationSummary
from ..engine.runner import Runner
from ..engine.storage import RunStore, write_json
from ..evidence.observer import FixedObserver
from ..policies.registry import build_policies
from ..policies.learned import validate_runtime
from .features import context_from_history
from .model import fit_model, evaluate_model


def synthetic_rows(config=None):
    config = config or load_config(ROOT / 'experiments/e0_smoke.yaml')
    protocol = dict(target=deepcopy(config['target']), attacker=deepcopy(config['attacker']),
                    observation=deepcopy(config['observation']), repetitions=4,
                    revision_mode='fixed_candidate')
    rows = []
    for split, count in (('train', 12), ('dev', 4), ('test', 4)):
        for i in range(count):
            state = 'T' if i % 2 == 0 else 'F'
            history = History((ObservationSummary('source', 'a' * 64, 'TRIGGERED_FAIL',
                                  state, 'Reject',
                                  (EvidenceSummary('receipt', 'email_received', state, True),)),), 3, 0)
            high, low = [True, True, True, False], [True, False, False, False]
            base = f'fixture/{split}_task_{i}'
            rows.append(dict(schema_version=1, kind='SYNTHETIC_FIXTURE',
                             pair_id=base + '/h1', base_task_id=base, group_id=base,
                             split=split, status='paired', protocol=deepcopy(protocol),
                             context=context_from_history(history),
                             outcomes=dict(Retry=high if state == 'T' else low,
                                           Revise=low if state == 'T' else high),
                             payloads=dict(Retry='a' * 64, Revise='b' * 64)))
    return rows


def run_smoke(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = synthetic_rows()
    with (output / 'paired_synthetic.jsonl').open('x') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
    model = fit_model(rows, steps=500)
    write_json(output / 'model_synthetic.json', model)
    for split in ('dev', 'test'):
        write_json(output / (split + '_synthetic_metrics.json'),
                   evaluate_model(model, rows, split=split))
    config = load_config(ROOT / 'experiments/e0_smoke.yaml')
    cases = [load_cases(config)[0]]
    config['policies'] = dict(always_retry={}, always_revise={}, learned=dict(
        model_path=str(output / 'model_synthetic.json'), allow_synthetic=True))
    validate_runtime(config, cases)
    runner = Runner(FixtureTarget(), FixtureAttacker(), FixedObserver(**config['observation']),
                    build_policies(config['policies']),
                    RunStore(output / 'runner', dict(result_kind='SYNTHETIC_FIXTURE', config=config)),
                    post_failure_budget=config['post_failure_target_budget'], seed=config['seed'])
    runner.run(cases)
    if not any(d['policy_id'] == 'learned' and d['reason'].startswith('learned_')
               for d in runner.decisions):
        raise AssertionError('Learned policy was not exercised by Runner')
    summary = dict(kind='SYNTHETIC_FIXTURE', status='passed', api_calls=0,
                   data_rows=len(rows), model=str(output / 'model_synthetic.json'),
                   learned_decisions=sum(d['policy_id'] == 'learned' for d in runner.decisions),
                   claim='Engineering validation only; no measured AgentDojo efficacy.')
    write_json(output / 'summary.json', summary)
    return summary
