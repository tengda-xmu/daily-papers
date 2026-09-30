import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from pathlib import Path
import pytest
import yaml
from connectors.codex_bridge.daily_update import DailyUpdater, remote_json
from src.update_cycle import cycle_start
from tools.daily_schedule import dispatch_if_needed, update_needed, workflow_gate
from tools.public_updates import FILES, pending_columns, refresh

NOW = datetime.fromisoformat('2026-09-30T11:47:00+08:00')


def edition(timestamp='2026-09-29T21:03:00+08:00'):
    return {'generated_at':timestamp, 'core':[{'id':'111111111111'}], 'extended':[]}


@pytest.mark.parametrize('moment,expected', [
    ('2026-09-30T20:59:59+08:00','2026-09-29T21:00:00+08:00'),
    ('2026-09-30T21:00:00+08:00','2026-09-30T21:00:00+08:00'),
    ('2026-10-01T00:00:00+08:00','2026-09-30T21:00:00+08:00'),
    ('2026-10-01T03:59:00Z','2026-09-30T21:00:00+08:00')])
def test_latest_due_beijing_cycle(moment,expected):
    assert cycle_start(datetime.fromisoformat(moment)).isoformat()==expected


def test_delayed_checks_and_manual_update():
    for moment in ('2026-09-29T04:00:07+08:00','2026-09-30T02:28:07+08:00','2026-09-30T11:47:32+08:00'):
        assert update_needed(edition('2026-09-27T21:03:00+08:00'),datetime.fromisoformat(moment))[0]
    assert not update_needed(edition(),NOW)[0]
    assert update_needed(edition(),NOW.replace(hour=21))[0]
    assert workflow_gate(edition(),'workflow_dispatch',now=NOW)==(True,'manual_update')


def test_no_new_complete_errors_and_invalid_data_not_complete():
    value=edition('2026-09-27T21:03:00+08:00')
    value['latest_update']={'outcome':'no_new','checked_at':'2026-09-30T02:30:00+08:00'}
    assert not update_needed(value,NOW)[0]
    for check in ({},{'outcome':'error','checked_at':NOW.isoformat()},
                  {'outcome':'no_new','checked_at':(NOW+timedelta(hours=1)).isoformat()}):
        assert update_needed({**value,'latest_update':check},NOW)[0]
    for invalid in ({},{'generated_at':'bad'},{**edition(),'core':[]},{**edition(),'extended':None},
                    edition('2026-09-30T21:02:00'),edition('2026-10-01T21:00:00Z')):
        assert update_needed(invalid,NOW)[0]


class Remote:
    def __init__(self,papers=None,due=(),runs=()):
        self.calls=[];self.runs=runs;self.lost=False
        self.status='in_progress';self.conclusion=None;self.request_id=''
        self.files={'daily.json':papers or edition(),'update-status.json':{},
                    'public-updates.json':{'columns':{name:{'checked_at':NOW.isoformat(),'outcome':'ok'}
                                              for name in FILES if name not in due}}}
        self.files.update({file:{} for name,file in FILES.items() if name in due})
    def __call__(self,method,path,body=None):
        self.calls.append((method,path,body))
        if method=='POST':
            self.request_id=body.get('inputs',{}).get('request_id',self.request_id)
            if self.lost:raise TimeoutError('response lost')
            return {'workflow_run_id':123}
        if '/contents/data/' in path:
            name=path.split('/contents/data/')[1].split('?')[0]
            return {'content':base64.b64encode(json.dumps(self.files[name]).encode()).decode()}
        if '/runs?' in path:
            return {'workflow_runs':self.runs or ([{'id':123,'display_title':'Evening check '+self.request_id,'status':self.status}] if self.request_id else [])}
        if '/jobs?' in path:return {'jobs':[]}
        if '/runs/' in path:return {'status':self.status,'conclusion':self.conclusion}
        return {}


def test_shared_instances_and_restart_dispatch_once(tmp_path):
    remote=Remote(edition('2026-09-27T21:00:00Z'))
    updaters=[DailyUpdater(tmp_path,remote) for _ in range(3)]
    with ThreadPoolExecutor(3) as pool:
        jobs=list(pool.map(lambda u:u.catch_up(now=NOW),updaters))
    assert {j['run_id'] for j in jobs}=={'123'}
    posts=[c for c in remote.calls if c[0]=='POST']
    assert len(posts)==1 and posts[0][2]['inputs']['scheduled_check'] is True
    assert DailyUpdater(tmp_path,remote).catch_up(now=NOW)['run_id']=='123'


def test_current_and_active_runs_never_dispatch(tmp_path):
    remote=Remote()
    assert dispatch_if_needed(remote,NOW,runtime=tmp_path)['state']=='current'
    assert not any(c[0]=='POST' for c in remote.calls)
    remote=Remote(edition('2026-09-27T21:00:00Z'),runs=[{'id':999,'status':'queued'}])
    data=dispatch_if_needed(remote,NOW,runtime=tmp_path/'other')
    assert data['run_id']=='999' and not any(c[0]=='POST' for c in remote.calls)


def test_missing_column_dispatches_without_new_papers(tmp_path):
    remote=Remote(due=('leads',))
    data=dispatch_if_needed(remote,NOW,runtime=tmp_path)
    assert data['state']=='queued' and data['cycle']==cycle_start(NOW).isoformat()


def test_lost_post_resolves_after_restart(tmp_path):
    remote=Remote(edition('2026-09-27T21:00:00Z'));remote.lost=True
    assert DailyUpdater(tmp_path,remote).catch_up(now=NOW)['state']=='confirming'
    assert DailyUpdater(tmp_path,remote).catch_up(now=NOW)['run_id']=='123'
    assert sum(c[0]=='POST' for c in remote.calls)==1


def test_failure_backoff_and_online_recovery(tmp_path):
    calls=[]
    def offline(*args):calls.append(args);raise OSError('private credentials')
    updater=DailyUpdater(tmp_path,offline)
    data=updater.catch_up(now=NOW)
    assert data['state']=='failed' and data['retry_at']==NOW.timestamp()+300
    updater.catch_up(now=NOW+timedelta(seconds=60))
    assert len(calls)==1
    updater.remote=Remote(edition('2026-09-27T21:00:00Z'))
    assert updater.catch_up(reconnected=True,now=NOW+timedelta(seconds=5))['state']=='queued'


def test_reconnect_tracks_existing_run_immediately_after_status_read_failed(tmp_path):
    remote=Remote(edition('2026-09-27T21:00:00Z'))
    updater=DailyUpdater(tmp_path,remote)
    updater.catch_up(now=NOW)
    def offline(*args):raise OSError('offline')
    updater.remote=offline
    assert updater.catch_up(now=NOW+timedelta(minutes=1))['state']=='failed'
    updater.remote=remote
    resumed=updater.catch_up(reconnected=True,now=NOW+timedelta(seconds=65))
    assert resumed['run_id']=='123' and resumed['state']=='queued'
    assert sum(c[0]=='POST' for c in remote.calls)==1


def test_large_metadata_uses_blob():
    def remote(method,path):
        if '/contents/' in path:return {'encoding':'none','sha':'a'*40}
        assert path.endswith('/git/blobs/'+'a'*40)
        return {'content':base64.b64encode(b'{"ok":true}').decode()}
    assert remote_json(remote,'daily.json')=={'ok':True}


def test_completed_columns_skipped_and_partial_backoff(tmp_path,monkeypatch):
    from src.public_sources import write,read
    import src.ai_updates,src.opportunities,src.research_leads,src.social_content
    for file in FILES.values():write(tmp_path/'data'/file,{'checked_at':NOW.isoformat(),'outcome':'ok'})
    write(tmp_path/'data/ai-updates.json',{})
    calls=[]
    def forbidden(*args):calls.append('unexpected');raise AssertionError('Already completed')
    monkeypatch.setattr(src.opportunities,'refresh',forbidden)
    monkeypatch.setattr(src.research_leads,'refresh',forbidden)
    monkeypatch.setattr(src.social_content,'refresh',forbidden)
    monkeypatch.setattr(src.ai_updates,'refresh',lambda root:{'checked_at':NOW.isoformat(),'outcome':'partial','entries':[]})
    monkeypatch.setattr('tools.public_updates.import_readings',lambda *_:None)
    assert pending_columns(tmp_path,NOW)==['ai']
    refresh(tmp_path,due_only=True,now=NOW)
    data=read(tmp_path/'data/public-updates.json')['columns']
    assert not calls and set(data)=={'ai'} and data['ai']['failures']==1
    assert pending_columns(tmp_path,NOW+timedelta(minutes=1))==[]
    assert pending_columns(tmp_path,NOW+timedelta(minutes=5))==['ai']


def test_workflow_serialization_and_publication_artifact():
    root=Path(__file__).resolve().parents[1]
    workflow=yaml.load((root/'.github/workflows/daily.yml').read_text(encoding='utf-8'),Loader=yaml.BaseLoader)
    assert workflow['concurrency']['cancel-in-progress']=='false'
    assert workflow['jobs']['update']['needs']=='check'
    assert workflow['jobs']['publish']['needs']==['check','update']
    assert any(s.get('uses')=='actions/upload-artifact@v4' for s in workflow['jobs']['update']['steps'])
    assert any(s.get('uses')=='actions/download-artifact@v4' for s in workflow['jobs']['publish']['steps'])
    assert 'publication_ready' in workflow['jobs']['publish']['if']
    imported=next(s for s in workflow['jobs']['update']['steps'] if s.get('name')=='Import local connector metadata')
    assert 'mkdir -p data/inbox' in imported['run']
