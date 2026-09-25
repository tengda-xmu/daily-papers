import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from src.public_sources import article, candidates, date, identifier, read, write
from src.ai_updates import normalize, refresh, validate_analysis
from src.opportunities import classify, enrich, retain, state

NOW = datetime(2026, 9, 25, 14, tzinfo=timezone.utc)


def item(**kwargs):
    return normalize({'title': 'New research agent release', 'url': 'https://example.org/agent-v2',
        'evidence_url': 'https://example.org/agent-v2', 'evidence_text': 'The new research agent supports document analysis. Select a document to begin. An account is required.',
        'published_at': '2026-09-24T10:00:00Z', 'verification': 'verified', 'source': 'Example', **kwargs})


def analysis(row):
    return {'id': row['id'], 'content_version': row['content_version'], 'analysis': {
        'title_zh': '科研智能体更新', 'summary': '新增文档分析功能。', 'application': '建议：用于文献整理。',
        'requirements': '需要账号。', 'steps': [{'text': '选择文档。', 'evidence': 'Select a document to begin.'}],
        'evidence': ['The new research agent supports document analysis.']}}


def test_nested_changelog_preserves_event_dates_and_versions():
    body = b'<main><div><h2 id="one">Date: 2026-09-10</h2></div><h3>Model V2 Release</h3><p>New model.</p><div><h2 id="two">Date: 2026-08-10</h2></div><h3>Model V1</h3><p>Old model.</p></main>'
    rows = candidates(body, {'format': 'changelog', 'url': 'https://example.org/updates'})
    assert len(rows) == 2
    assert rows[0]['url'].endswith('#one') and 'Old model' not in rows[0]['evidence_text']
    assert rows[0]['published_at'].startswith('2026-09-10')
    assert date('24 September 2026').startswith('2026-09-24')


def test_article_dates_are_not_inferred_from_deadlines_or_crawl_time():
    row = article('<h1>Open research call</h1><p>截止时间：2026年10月10日</p>'.encode(), 'https://example.org/call')
    assert not row['published_at']
    body = b'<script type="application/ld+json">{"@type":"Article","datePublished":"2026-09-20"}</script><h1>Agent update</h1>'
    assert article(body, 'https://example.org/news')['published_at'].startswith('2026-09-20')


def test_evidence_is_required_for_steps_and_stale_results_are_rejected():
    row = item()
    assert validate_analysis(analysis(row), row)['analysis']['steps']
    bad = analysis(row)
    bad['analysis']['steps'][0]['evidence'] = 'A made-up command that is not in the source.'
    with pytest.raises(ValueError):
        validate_analysis(bad, row)
    with pytest.raises(ValueError):
        validate_analysis(analysis(row), item(evidence_text='Changed official content.'))


def test_ai_keeps_history_merges_event_identity_and_retains_data_on_failure(tmp_path):
    first = item(event_key='research-agent-v2')
    second = item(url='https://example.org/another-announcement', event_key='research-agent-v2')
    write(tmp_path / 'config/ai-sources.json', {'entries': [first, second], 'sources': []})
    result = refresh(tmp_path, now=NOW, summarize=lambda _: None)
    assert len(result['entries']) == 1 and result['entries'][0]['related_urls']
    config = {'entries': [], 'sources': [{'id':'bad','name':'Bad','url':'https://example.org/feed','hosts':['example.org'],'format':'rss'}]}
    write(tmp_path / 'config/ai-sources.json', config)
    def fail(*_):
        raise OSError('secret-auth-must-not-be-published')
    result = refresh(tmp_path, now=NOW + timedelta(days=100), fetcher=fail, summarize=lambda _: None)
    assert len(result['entries']) == 1 and result['outcome'] == 'partial'
    assert 'secret-auth' not in json.dumps(result)


@pytest.mark.parametrize('title,kind', [('青年编委招募','academic_role'),('ASME Join a Technical Committee','academic_role'),
    ('企业航空结构科研项目招标申报','funding'),('2026年联合基金申请指南','funding'),
    ('青年编委名单',''),('基金指南征订通知',''),('科研物资采购招标',''),('基金拟资助结果公示','')])
def test_opportunity_classification_excludes_non_applications(title, kind):
    assert (classify(title) or '') == kind


def test_deadlines_staleness_long_running_and_parent_group():
    row = {'kind': 'funding', 'verification':'verified', 'verified_at':NOW.isoformat(), 'deadline':'2026-10-01', 'stage':'application'}
    assert state(row, NOW) == ('closing', '即将截止')
    assert state({**row,'deadline_at':'2026-09-25T23:59:00-12:00'}, NOW)[0] != 'ended'
    assert state({**row,'deadline':'2026-09-24'}, NOW)[0] == 'ended'
    assert state({**row,'deadline':'2026-12-01','verified_at':(NOW-timedelta(days=31)).isoformat()}, NOW)[0] == 'unknown'
    assert retain({**row,'published_at':'2025-01-01'}, NOW)
    standing = {**row,'deadline':'','stage':'standing','published_at':'2020-01-01'}
    assert retain(standing,NOW) and state(standing,NOW)[0] == 'standing'
    assert not retain({**row,'deadline':'2025-01-01'}, NOW)
    corporate = enrich({'title':'航空开放基金申报','url':'https://example.org/call','organization_group':'comac','evidence_text':'申请截止日期：2026年10月1日'})
    assert corporate['deadline'] == '2026-10-01' and corporate['organization_type'] == 'enterprise'
    unrelated = enrich({'title':'结构开放课题申请','url':'https://example.org/university','evidence_text':'申请人可来自中国航发等企业。'})
    assert not unrelated.get('organization_group')


def test_agent_skills_are_distinct_from_training_and_plain_tools():
    from src.ai_updates import classify
    assert 'skills' in classify('Agent Skills: workflows and SKILL.md')
    assert 'skills' in classify('MCP support for tool connections')
    assert 'skills' not in classify('Learn practical AI skills at a training course')
    assert 'skills' not in classify('A new coding tool')


def test_ai_page_escapes_content_and_has_public_filters():
    from tools.public_pages import render_ai
    html = render_ai({'entries':[item(title='<script>alert(1)</script> Model release')], 'sources':[]})
    assert '<script>alert(1)' not in html and 'id="lead-period"' in html
    assert 'ai.html' in html and 'aria-current="page">AI 前沿' in html
    assert 'id="lead-pagination"' in html and '中文导读待补充' in html


def test_public_freshness_is_independent_of_papers(tmp_path):
    from tools.public_updates import needed
    write(tmp_path / 'data/daily.json', {'generated_at': NOW.isoformat(), 'core': [{'id':'paper'}], 'extended':[]})
    assert needed(tmp_path,NOW)
    for name in ('ai-updates.json','opportunities.json'):
        write(tmp_path / 'data' / name, {'checked_at':NOW.isoformat(),'outcome':'ok'})
    assert not needed(tmp_path,NOW)
    write(tmp_path / 'data/ai-updates.json', {'checked_at':NOW.isoformat(),'outcome':'partial'})
    assert needed(tmp_path,NOW)


def test_ai_queue_is_separate_resumes_and_preserves_ready_results(tmp_path):
    from connectors.codex_bridge.ai_queue import AIQueue
    runtime = tmp_path / 'runtime'
    row = item()
    queue = AIQueue(tmp_path, runtime, None, asyncio.Lock(), fetch=lambda _: {'entries':[row]})
    queue.sync()
    assert queue.snapshot()['counts']['pending'] == 1
    with queue.db() as db:
        db.execute("UPDATE tasks SET state='generating',attempts=1")
    restarted = AIQueue(tmp_path,runtime,None,asyncio.Lock())
    assert restarted.snapshot()['counts']['pending'] == 1
    with restarted.db() as db:
        db.execute("UPDATE tasks SET state='ready',result=?", (json.dumps(analysis(row)),))
    restarted.enqueue(row)
    assert restarted.snapshot()['counts']['ready'] == 1
    assert not (runtime / 'reading-queue.sqlite3').exists()


def test_release_dedup_keeps_versions_and_ignores_integrations():
    from src.ai_updates import event_identity
    first = item(title='Introducing DeepSeek-V4.1-Flash')
    report = item(title='DeepSeek-V4.1-Flash Release', url='https://example.org/release')
    assert event_identity(first) == event_identity(report)
    assert event_identity(first) != event_identity(item(title='DeepSeek-V4.2-Flash Release'))
    assert event_identity(first) != event_identity(item(title='Introducing integration with DeepSeek-V4.1-Flash'))


def test_recollection_does_not_reset_publication_date(tmp_path):
    row = item()
    write(tmp_path/'config/ai-sources.json', {'entries':[row]})
    refresh(tmp_path,now=NOW,summarize=lambda _:None)
    write(tmp_path/'config/ai-sources.json', {'entries':[dict(row,published_at=NOW.isoformat())]})
    result = refresh(tmp_path,now=NOW,summarize=lambda _:None)
    assert result['entries'][0]['published_at'] == row['published_at']


def test_reviewed_details_require_same_source_content():
    import hashlib
    from src.public_sources import collect_source,clean
    text='Research funding application. Deadline unknown.'
    body=('<h1>Research call</h1><main>'+text+'</main>').encode()
    source={'id':'call','name':'Lab','url':'https://example.org/call','hosts':['example.org'],'format':'document',
            'review':{'source_version':hashlib.sha256(clean(text).encode()).hexdigest(),'fields':{'deadline':'2026-10-01'}}}
    assert collect_source(source,NOW,lambda *_:body)[0]['deadline']=='2026-10-01'
    changed=body.replace(b'Deadline unknown.',b'Deadline has changed.')
    assert 'deadline' not in collect_source(source,NOW,lambda *_:changed)[0]


def test_spaced_chinese_deadline_and_result_date_are_distinct():
    row=enrich({'title':'航空开放课题申请','url':'https://example.org/call',
                'evidence_text':'请于202 6 年 5 月 17 日前发送材料。评审结果于2026年6月30日前公布。'})
    assert row['deadline']=='2026-05-17'
    row=enrich({'title':'智能算法开放课题申请','url':'https://example.org/call',
                'evidence_text':'截止受理日期为2026年9月30日，评审结果将于2026年10月30日前发布公告。'})
    assert row['deadline']=='2026-09-30'


def test_ai_generation_retries_and_publish_requires_live_acknowledgement(tmp_path):
    from connectors.codex_bridge.ai_queue import AIQueue
    from tests.test_reading_queue import FakeClient,task
    row=item();q=AIQueue(tmp_path,tmp_path/'runtime',FakeClient(analysis(row)),asyncio.Lock())
    q.enqueue(row);asyncio.run(q.generate(task(q)))
    assert task(q)['state']=='ready'
    calls=[];q.publisher=lambda root,values:calls.append(values);q.fetch=lambda _:{'entries':[row]}
    q.publish_ready();assert task(q)['state']=='ready' and len(calls)==1
    row['analysis']=json.loads(task(q)['result'])['analysis']
    q.publish_ready();assert task(q)['state']=='published'
    other=item(url='https://example.org/bad')
    q.enqueue(other);q.client=FakeClient(TimeoutError())
    with q.db() as db:
        failed=dict(db.execute('SELECT * FROM tasks WHERE paper_id=?',(other['id'],)).fetchone())
    for _ in range(3):asyncio.run(q.generate(failed))
    assert q.snapshot()['counts']['failed']==1
    q.retry(other['id']);assert q.snapshot()['counts']['pending']==1


def test_public_collection_failure_does_not_replace_other_columns_or_papers(tmp_path,monkeypatch):
    import src.ai_updates,src.opportunities,src.research_leads
    from tools import public_updates
    write(tmp_path/'data/daily.json',{'edition':'unchanged'})
    write(tmp_path/'data/ai-updates.json',{'entries':[{'id':'kept'}]})
    def failed(*_):raise OSError('private-network-error')
    def successful(*_):return {'entries':[{'id':'fresh'}],'outcome':'ok','checked_at':NOW.isoformat()}
    monkeypatch.setattr(src.ai_updates,'refresh',failed)
    monkeypatch.setattr(src.opportunities,'refresh',successful)
    monkeypatch.setattr(src.research_leads,'refresh',successful)
    monkeypatch.setattr(public_updates,'import_readings',lambda *_:0)
    public_updates.refresh(tmp_path)
    data=read(tmp_path/'data/public-updates.json')
    assert data['columns']['ai']['outcome']=='error'
    assert data['columns']['opportunities']['outcome']=='ok'
    assert read(tmp_path/'data/daily.json')=={'edition':'unchanged'}
    assert read(tmp_path/'data/ai-updates.json')['entries']==[{'id':'kept'}]


def test_ai_task_endpoints_require_pairing_but_pages_do_not(tmp_path):
    from fastapi.testclient import TestClient
    from connectors.codex_bridge.server import create_app,LOCAL_ORIGIN
    from tests.test_reading_queue import FakeClient
    (tmp_path/'site').mkdir()
    (tmp_path/'site/ai.html').write_text('AI public page')
    app=create_app(tmp_path,rpc=FakeClient({}))
    client=TestClient(app,base_url=LOCAL_ORIGIN)
    assert client.get('/ai.html').status_code==200
    assert client.get('/api/ai/reading-tasks').status_code==401
    token=client.post('/api/pair',json={'code':app.state.pair_code}).json()['token']
    headers={'Authorization':'Bearer '+token}
    assert client.get('/api/ai/reading-tasks',headers=headers).status_code==200
    assert client.post('/api/ai/reading-tasks/sync',headers=headers).json()['state']=='queued'
