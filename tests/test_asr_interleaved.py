import io
import json
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
from scripts import launch_asr_interleaved as launcher


def test_schedule_pairs_every_case_once_per_repeat_with_balanced_first_arm():
    cases=[f'c{i:03}' for i in range(30)]
    rows=launcher.schedule(cases,['reference','candidate'],3,20261101)
    assert len(rows)==180
    for repeat in range(3):
        part=[r for r in rows if r['repeat']==repeat]
        assert {(r['case_id'],r['arm']) for r in part}=={(c,a) for c in cases for a in ('reference','candidate')}
        assert sum(r['arm']=='reference' for r in part[::2])==15
        assert all(a['case_id']==b['case_id'] and a['arm']!=b['arm'] for a,b in zip(part[::2],part[1::2]))


@pytest.mark.parametrize('stop_after',[None,1])
def test_real_parent_scheduler_uses_one_preflight_per_arm_repeat_and_stops_without_reissue(tmp_path,monkeypatch,stop_after):
    cases=['c001','c002']; names=['reference','candidate']; output=tmp_path/'run'
    arms=[]
    for name in names:
        root=tmp_path/name;root.mkdir()
        lock=root/'condition.lock.json';lock.write_text('{}')
        source=root/'source_manifest.json';source.write_text('{}')
        arms.append(dict(name=name,source_root=str(root),frozen=str(lock),
            frozen_sha256=hashlib.sha256(lock.read_bytes()).hexdigest(),
            source_manifest_sha256=hashlib.sha256(source.read_bytes()).hexdigest()))
    protocol=dict(output=str(output),python=sys.executable,arms=arms,case_ids=cases,repeats=3,
        schedule_seed=4,schedule=launcher.schedule(cases,names,3,4),paid_execution_authorized=True)
    path=tmp_path/'protocol.json';path.write_text(json.dumps(protocol))
    launches=[]; executed=[]
    class Process:
        def __init__(self,command,**kwargs):
            self.name=Path(command[-1]).name
            launches.append(command)
            self.queue=[{'status':'ready'}]
            self.stdin=self;self.stdout=self;self.code=None
        def write(self,line):
            cmd=json.loads(line)
            if cmd['action']=='finish':
                self.queue.append({'status':'completed'});self.code=0
            else:
                executed.append((self.name,cmd['case_id']))
                self.queue.append(dict(case_id=cmd['case_id'],status='completed'))
                if stop_after==len(executed): (output/'STOP.json').write_text('{}')
        def flush(self):pass
        def close(self):self.code=1
        def readline(self):return json.dumps(self.queue.pop(0))+'\n'
        def poll(self):return self.code
        def wait(self):return self.code
    monkeypatch.setattr(launcher.subprocess,'Popen',Process)
    monkeypatch.setattr(launcher.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0,stdout=json.dumps(dict(status='offline_valid',case_ids=cases))))
    monkeypatch.setattr(launcher,'configure_credentials',lambda:None)
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN','local-mock-not-a-credential')
    monkeypatch.setattr(sys,'argv',['launch','--protocol',str(path)])
    if stop_after:
        with pytest.raises(RuntimeError,match='402'):launcher.main()
        assert len(executed)==1 and len(launches)==2
        assert json.loads((output/'status.json').read_text())['status']=='stopped_402'
    else:
        launcher.main()
        assert len(launches)==6
        assert executed==[(r['arm'],r['case_id']) for r in protocol['schedule']]
        assert json.loads((output/'status.json').read_text())['status']=='awaiting_analysis'
