import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from src import xiaohongshu as xhs
from src import social_content as social
from src.public_sources import read, write
from src.ai_updates import normalize, can_analyze, public_index, fingerprint
from connectors.codex_bridge.social_notes import NoteManager, NoteChange

NOW = datetime(2026, 9, 25, 14, tzinfo=timezone.utc)
ID = '66e16f01000000001a011234'
URL = 'https://www.xiaohongshu.com/explore/' + ID


def record(**kwargs):
    return dict(id='xhs-'+ID, title='科研智能体 MCP 实践经验', url=URL, platform='xiaohongshu', provider='xiaohongshu',
                source='研究作者', author='研究作者', published_at='2026-09-24', evidence_kind='manual_text',
                evidence_text='使用 MCP 连接文献工具。先配置已有的文献服务，再在支持 MCP 的智能体中启用，最后核对生成的引用。' * 3, **kwargs)


@pytest.mark.parametrize('url', ['https://evil.test/explore/'+ID, 'https://www.xiaohongshu.com.evil.test/explore/'+ID,
    'http://127.0.0.1/explore/'+ID, 'https://user@www.xiaohongshu.com/explore/'+ID,
    'https://www.xiaohongshu.com:43127/explore/'+ID, 'file:///etc/passwd', 'https://xhslink.com/../evil'])
def test_reject_non_note_and_unsafe_urls(url):
    with pytest.raises(ValueError): xhs.share_url(url)


def test_share_short_link_and_note_identity():
    assert xhs.share_url('推荐这篇笔记 https://xhslink.com/a/ABCxyz 。') == 'https://xhslink.com/a/ABCxyz'
    assert xhs.public_url('https://www.xiaohongshu.com/discovery/item/'+ID+'?xsec_token=transient') == URL


def test_parse_note_uses_matching_id_and_real_creation_time():
    payload={'note':{'noteDetailMap':{ID:{'note':{'noteId':ID,'title':'MCP 科研实践','desc':'先配置工具，再验证输出。','user':{'nickname':'作者'},'time':1726138800000}}}}}
    body=('<script>window.__INITIAL_STATE__='+json.dumps(payload)+'</script>').encode()
    note=xhs.parse_note(body,URL)
    assert note['title']=='MCP 科研实践' and note['author']=='作者'
    assert note['published_at'].startswith('2024-')
    assert note['evidence_kind']=='article'
    with pytest.raises(ValueError): xhs.parse_note(b'<title>Login</title>',URL)


def test_search_budget_cache_and_missing_dates(tmp_path):
    calls=[]
    def search(query,key):
        calls.append(query)
        return {'organic_results':[{'title':'MCP 实践','link':URL,'snippet':'索引摘要'}]}
    rows,status=xhs.discover(tmp_path,now=NOW,key='test',searcher=search)
    assert len(calls)==2 and all('site:xiaohongshu.com/explore/' in q for q in calls)
    assert rows[0]['published_at']=='' and rows[0]['evidence_kind']=='search_snippet'
    xhs.discover(tmp_path,now=NOW+timedelta(minutes=10),key='test',searcher=search)
    assert len(calls)==2
    # Different query rotation on another machine cannot exceed the day's persisted budget.
    for p in (tmp_path/'data/cache/xiaohongshu').glob('*.json'):
        if p.name!='budget.json': p.unlink()
    _,status=xhs.discover(tmp_path,now=NOW,key='test',searcher=search)
    assert all(s['status']=='quota_exhausted' for s in status)


def test_missing_key_keeps_manual_path_available(tmp_path):
    rows,status=xhs.discover(tmp_path,now=NOW,key='')
    assert not rows and all(s['status']=='configuration_missing' for s in status)


@pytest.mark.parametrize('title,column,kind',[
    ('青年编委公开招募','leads','academic_role'),('航空开放基金申报指南','leads','funding'),
    ('航空科研项目招标申请','leads','funding'),('AI 科研学术会议','leads','conference'),
    ('MCP 科研工作流实战','ai','ai'),('DeepSeek 发布新模型','ai','ai')])
def test_content_routing(title,column,kind):
    r=record();r['title']=title
    result=social.route(r)
    assert (result['column'],result['kind'])==(column,kind)


@pytest.mark.parametrize('title',['供应商注册通知','普通招聘求职','科研基金拟资助名单','AI 付费课程带货'])
def test_marketing_and_non_applications_excluded(title):
    r=record();r['title']=title
    assert social.route(r) is None


def test_source_content_not_mistaken_for_verified_application(tmp_path):
    r=record();r.update(title='航空开放基金申报指南',verification='verified',evidence_kind='article')
    write(tmp_path/social.PATH,{'entries':[social.route(r)]})
    row=social.column(tmp_path,'leads')[0]
    assert row['kind']=='funding' and row['verification']=='pending' and row['id']=='xhs-'+ID


def test_lead_digest_uses_shared_queue_without_entering_ai_column(tmp_path):
    from connectors.codex_bridge.ai_queue import AIQueue
    from tools.public_pages import social_details
    from tests.test_public_updates import analysis
    raw=record();raw['title']='航空开放基金申报指南'
    write(tmp_path/social.MANUAL,{'entries':[social.route(raw)]})
    index=public_index(tmp_path)
    assert not index['entries'] and len(index['social_readings'])==1
    queue=AIQueue(tmp_path,tmp_path/'runtime',None,asyncio.Lock(),fetch=lambda _:index)
    queue.sync()
    assert queue.snapshot()['counts']['pending']==1
    row=index['social_readings'][0]
    value=analysis(row)
    value['analysis']['steps']=[]
    value['analysis']['evidence']=[row['evidence_text'][:15]]
    write(tmp_path/'data/ai-readings'/ (row['id']+'.json'),value)
    assert public_index(tmp_path)['social_readings'][0]['analysis_status']=='ready'
    assert '中文导读' in social_details({**raw,'kind':'funding','analysis':value['analysis']})


def test_unavailable_short_link_keeps_resolved_note_identity(tmp_path):
    def fail(_):raise xhs.NoteReadUnavailable(URL)
    manager=NoteManager(tmp_path,reader=fail)
    value=manager.preview(NoteChange(share='https://xhslink.com/o/abc',title='MCP 科研实践'))
    assert value['id']=='xhs-'+ID and value['url']==URL and value['read_status']=='unavailable'


def test_failed_refresh_does_not_renew_verification(tmp_path):
    raw=social.route(record());raw.update(evidence_kind='article',verified_at='2026-07-01T00:00:00Z',checked_at='2026-07-01T00:00:00Z')
    write(tmp_path/social.PATH,{'entries':[raw]})
    def fail(*a):raise OSError()
    result=social.refresh(tmp_path,now=NOW,wechat_fetch=lambda *_:([],[]),xhs_fetch=lambda *a,**k:([raw],[]),enrich=fail)
    assert result['entries'][0]['verified_at']==raw['verified_at']
    assert result['entries'][0]['read_status']=='unavailable'


def test_unsafe_social_id_cannot_become_reading_file_path():
    row=record();row['id']='../../private'
    assert '/' not in normalize(row)['id']


def test_same_wechat_headline_on_different_dates_is_not_overwritten(tmp_path):
    records=[{'account':'科研账号','title':'开放基金申报指南','published_at':day,'landing_url':'https://weixin.sogou.com/search','summary':''}
             for day in ('2026-09-24','2026-09-25')]
    result=social.refresh(tmp_path,now=NOW,wechat_fetch=lambda *_:(records,[]),xhs_fetch=lambda *a,**k:([],[]))
    assert len(result['entries'])==2 and len({r['id'] for r in result['entries']})==2


def test_same_event_merged_but_practice_retained():
    official={'id':'official','title':'DeepSeek-V4.1 Release','url':'https://api-docs.deepseek.com/news/v41','published_at':'2026-09-24','provider':'official'}
    report=record();report.update(title='DeepSeek-V4.1 正式发布',official_urls=[official['url']])
    report=social.route(report)
    merged=social.combine([official],[report])
    assert len(merged)==1 and len(merged[0]['source_links'])==2 and merged[0]['published_at']=='2026-09-24'
    practice={**report,'title':'DeepSeek-V4.1 科研实践','content_type':'practice'}
    assert len(social.combine([official],[practice]))==2
    other={**report,'title':'DeepSeek-V4.2 正式发布','official_urls':[]}
    assert len(social.combine([official],[other]))==2


def test_two_columns_survive_source_failures_and_no_papers(tmp_path):
    old=social.route(record())
    write(tmp_path/social.PATH,{'entries':[old], 'sources':[]})
    def fail(*a,**k):raise OSError('offline')
    r=social.refresh(tmp_path,now=NOW,wechat_fetch=fail,xhs_fetch=fail,enrich=fail)
    assert r['entries']==[old] and r['outcome']=='partial'
    assert social.column(tmp_path,'ai')[0]['id']==old['id']


def test_manual_sync_visible_without_waiting_for_daily_collection(tmp_path):
    row=social.route(record())
    write(tmp_path/social.MANUAL,{'entries':[row]})
    index=public_index(tmp_path)
    assert index['entries'][0]['id']==row['id']
    write(tmp_path/social.MANUAL,{'entries':[]})
    write(tmp_path/social.PATH,{'entries':[row],'manual_ids':[row['id']]})
    assert public_index(tmp_path)['entries']==[]


def test_social_analysis_requires_content_and_keeps_id(tmp_path):
    row=normalize(social.route(record()))
    assert row['id']=='xhs-'+ID and can_analyze(row)
    snippet=normalize({**row,'evidence_kind':'search_snippet','verification':'pending','evidence_text':''})
    assert not can_analyze(snippet)
    assert fingerprint(snippet)!=fingerprint(row)
    from connectors.codex_bridge.ai_queue import AIQueue
    queue=AIQueue(tmp_path,tmp_path/'runtime',None,asyncio.Lock())
    queue.enqueue(row)
    assert queue.snapshot()['counts']['pending']==1
    queue.enqueue(snippet)
    assert queue.snapshot()['counts']['missing_evidence']==1


def test_note_drafts_preview_conflicts_delete_and_explicit_sync(tmp_path):
    cloud={'entries':[]}; calls=[]
    def remote(method,body=None):
        calls.append(method)
        if method=='GET': return {'sha':'s','content':base64.b64encode(json.dumps(cloud).encode()).decode()}
        cloud.update(json.loads(base64.b64decode(body['content'])));return {}
    def reader(url):raise OSError('restricted')
    manager=NoteManager(tmp_path,remote=remote,reader=reader)
    data=NoteChange(share=URL,title='MCP 科研实践',author='测试作者',body='独立实践经验。'*300)
    preview=manager.preview(data)
    assert preview['column']=='ai' and not manager.path.exists()
    data.revision=manager.snapshot()['revision']
    state=manager.save(data)
    assert state['pending'] and not calls
    with pytest.raises(HTTPException):manager.save(data)
    state=manager.sync(state['revision'])
    assert not state['pending'] and calls==['GET','PUT']
    assert 'body' not in cloud['entries'][0] and len(cloud['entries'][0]['evidence_text'])<=900
    assert manager.snapshot()['entries'][0]['body']==data.body
    state=manager.remove(preview['id'],state['revision'])
    assert state['pending'] and cloud['entries']
    manager.sync(state['revision'])
    assert not cloud['entries']


def test_sync_failure_preserves_draft(tmp_path):
    def fail(*a):raise OSError('offline')
    manager=NoteManager(tmp_path,remote=fail,reader=fail)
    row=NoteChange(share=URL,title='MCP 科研工具',revision=manager.snapshot()['revision'])
    state=manager.save(row)
    with pytest.raises(OSError):manager.sync(state['revision'])
    assert manager.snapshot()==state


def test_public_render_sources_and_authors_are_escaped():
    from tools.public_pages import render_ai
    row=normalize(social.route(record()))
    row['author']='<img src=x onerror=alert(1)>'
    page=render_ai({'entries':[row],'sources':[]})
    assert 'lead-platform' in page and 'lead-author' in page and 'data-platforms="xiaohongshu"' in page
    assert '<img src=x' not in page and '手动补充内容' in page


def test_manual_endpoints_are_paired_and_do_not_publish_implicitly(tmp_path):
    from fastapi.testclient import TestClient
    from connectors.codex_bridge.server import create_app, LOCAL_ORIGIN
    from tests.test_reading_queue import FakeClient
    app=create_app(tmp_path,rpc=FakeClient({}))
    client=TestClient(app,base_url=LOCAL_ORIGIN)
    assert client.get('/api/social-notes').status_code==401
    assert client.post('/api/social-notes/preview',json={'share':URL}).status_code==401
    token=client.post('/api/pair',json={'code':app.state.pair_code}).json()['token']
    headers={'Authorization':'Bearer '+token}
    app.state.social_notes.reader=lambda url:{'title':'MCP 科研工具','url':URL,'evidence_kind':'excerpt'}
    state=client.get('/api/social-notes',headers=headers).json()
    saved=client.post('/api/social-notes',headers=headers,json={'share':URL,'title':'MCP 科研工具','revision':state['revision']})
    assert saved.status_code==200 and saved.json()['pending']
    assert not (tmp_path/social.MANUAL).exists()
    assert client.post('/api/social-notes',headers=headers,json={'share':'http://127.0.0.1:1/','revision':saved.json()['revision']}).status_code==400
