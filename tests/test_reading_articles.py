import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re

import pytest

from src.auto_reading import public_analysis, public_status, status_label
from src.editions import write
from src.models import RawRecord, SourceStatus
from src.reading_article import (ARTICLE_VERSION, EXPERIMENT_FIELDS, ROUTE_FIELDS,
    article_key, profile_context, public_article, without_page_citations)
from tests.test_fulltext_reading import FullClient, material
from tests.test_reading_queue import sample, task
from connectors.codex_bridge.reading_queue import ReadingQueue
from connectors.codex_bridge.article_research import related_literature
from tools.reading_articles import diagram_svg, render_article, source_note


def article_fixture():
    lead='疲劳寿命预测需要同时考虑试验样本的覆盖范围和模型对不同工况的适用性。研究将物理约束引入数据建模，通过独立工况测试检验方法的预测能力。真正需要关注的是验证条件是否支持工程外推，以及有限数据下的不确定性是否得到了合理评价。'
    paragraph=('研究以可核对的试验数据为依据，建立物理约束与预测模型之间的联系。模型首先学习材料响应，随后在独立测试集上比较误差，最后检查不同工况下的稳定性。'
               '这一设计能说明既定试验范围内的方法表现，但还不能直接推断长期服役环境的可靠性。')
    route=('建议先确定载荷变化引起的失效机制，再构建与物理方程一致的参数约束。使用独立试件检验预测误差和置信区间覆盖率，并与不含约束的基线比较。'
           '该方案仍需通过额外试验确认，若数据不足，可先开展仿真敏感性分析。')
    graph=lambda kind:{'kind':kind,'title':'从试验数据到可靠性评价','description':'将试验条件、物理建模与独立验证连接起来，说明各阶段的信息流与检查目标。',
         'nodes':[{'id':chr(97+i),'stage':i//2,'label':label,'detail':'保留真实数据来源与适用条件'} for i,label in enumerate(['试验数据','物理约束','预测模型','独立测试','可靠性评价','外推检查'])],
         'edges':[{'from':a,'to':b,'label':'检验'} for a,b in [('a','c'),('b','c'),('c','d'),('d','e'),('e','f'),('f','c')]]}
    return {'version':ARTICLE_VERSION,'lead':lead,'sections':[{'id':k,'title':t,'paragraphs':[paragraph,paragraph]}
        for k,t in zip(('context','method','validation','findings'),('从工程需求理解问题','物理约束怎样进入模型','独立样本如何验证','现有结果支持到哪里'))],
        'experiments':[{k:'原文未报告' for k in EXPERIMENT_FIELDS}],
        'routes':[{'title':'建立物理约束与可靠性校准的闭环','direction_id':'fatigue_reliability',**{k:route for k in ROUTE_FIELDS}}],
        'diagrams':[graph('study'),graph('proposal')],
        'related':{'status':'ok','checked_at':'2026-10-03T00:00:00+00:00','papers':[]}}


def test_typed_article_and_pages_are_only_hidden_at_presentation():
    a=article_fixture(); a['secret']='private'
    clean=public_article(a)
    assert 'secret' not in clean
    assert without_page_citations('方法[P2–P3、P12]，图2，公式(5)，P(x)。')=='方法，图2，公式(5)，P(x)。'
    assert source_note('P2的增长率与P3公式(7)，不能补造。')=='增长率与公式(7)，不能补造。'
    a['sections'][0]['paragraphs'][0]+='[P1]'
    clean=public_article(a)
    assert '[P1]' not in clean['sections'][0]['paragraphs'][0]
    assert a['sections'][0]['paragraphs'][0].endswith('[P1]')  # Private draft unchanged.
    a=article_fixture();a['diagrams'][0]['edges'][0]['to']='missing'
    with pytest.raises(ValueError,match='未知节点'):public_article(a)


def test_article_escapes_model_markup_and_renders_experiment_and_route():
    p=sample(); p.update(article=article_fixture(),title_zh='基于物理模型的结构疲劳预测研究',analysis_basis='full_text')
    p['article']['sections'][0]['paragraphs'][0]+='<script>alert(1)</script> **重要条件**'
    page=render_article(p)
    assert '<script>alert' not in page and '<strong>重要条件</strong>' in page
    assert '实验与验证一览' in page and '候选创新' in page
    assert page.count('class="article-diagram"')==2
    assert '本站根据论文绘制' in page and '本站提出的研究建议' in page
    assert 'reading-article.css' in page and 'aria-label="文章目录"' in page
    graph=p['article']['diagrams'][0];graph['nodes'][0]['label']='<script>bad</script>'
    for mobile in (False,True):
        svg=diagram_svg(graph,mobile=mobile)
        assert '<script>' not in svg and '&lt;script&gt;' in svg
        assert svg.count('marker-end=')==len(graph['edges'])


def test_legacy_article_keeps_content_and_removes_page_labels():
    p=sample(); p.update(summary='导读[P1]。',deep_read={'method':'原文[P2–P3]介绍方法，公式(5)缺失。'},analysis_basis='full_text')
    page=render_article(p)
    assert '[P1]' not in page and '[P2' not in page and '公式(5)' in page and '现有内容可继续阅读' in page


def test_related_search_budget_cache_and_teasers(tmp_path):
    calls=[]
    def search(source,query,*args,**kwargs):
        calls.append((source,query))
        return [RawRecord.from_mapping({'title':'A related verified fatigue study','doi':'10.1234/related',
          'abstract':'The independent experimental observations support physics based fatigue prediction under different loading conditions. '*3,
          'published_at':'2025-02-03','landing_url':'https://doi.org/10.1234/related','source':source}),
          RawRecord.from_mapping({'title':'Search fragment only','abstract':'A promising method ... '*20,'published_at':'2025-01-01'})],SourceStatus(source,'ok',2)
    now=datetime(2026,10,3,tzinfo=timezone.utc)
    r=related_literature(sample(),['fatigue','physical','reliability'],tmp_path,search=search,now=now)
    assert len(calls)==6 and len(r['papers'])==1 and r['papers'][0]['basis']=='abstract'
    assert related_literature(sample(),['fatigue','physical','reliability'],tmp_path,search=search,now=now)==r and len(calls)==6


class ArticleClient(FullClient):
    def __init__(self,p,approve=True):
        super().__init__(p);self.approve=approve;self.article_calls=[]
    async def thread(self,existing=None,*,purpose='reading'):
        assert purpose in ('reading','article')
        return await super().thread(existing)
    async def turn(self,thread,prompt,images=()):
        self.article_calls.append(prompt)
        if prompt.startswith('从以下已核实'):
            labels=list(dict.fromkeys(re.findall(r'\[(P\d+)\]',prompt)))
            v={'facts':[{'kind':'method','detail':'物理约束模型使用独立试验结果进行验证。','refs':labels}],
               'experiments':[{**{k:'本批未报告' for k in EXPERIMENT_FIELDS},'refs':labels}], 'queries':['fatigue model']}
        elif prompt.startswith('撰写一篇'):v=article_fixture()
        elif prompt.startswith('核验以下'):v={'approved':self.approve,'issues':[] if self.approve else ['对比实验误用数量']}
        else:
            async for e in super().turn(thread,prompt,images):yield e
            return
        yield {'type':'delta','text':json.dumps(v,ensure_ascii=False)}
        yield {'type':'completed','status':'completed'}


def article_queue(tmp_path,approve=True):
    p=sample();client=ArticleClient(p,approve);m=material(p)
    q=ReadingQueue(tmp_path,tmp_path/'run',client,asyncio.Lock(),resolver=lambda p,**kw:m)
    q.quiet_until=0
    q.enqueue(p);asyncio.run(q.process(task(q)))
    q.articles.research=lambda *args:{'status':'ok','checked_at':'2026-10-03T00:00:00+00:00','papers':[]}
    return q,client


def test_existing_fulltext_upgrades_without_rereading_and_publication_is_independent(tmp_path):
    q,c=article_queue(tmp_path);old=task(q);row=q.articles.next_task()
    asyncio.run(q.articles.process(row))
    result=task(q)
    assert result['article_state']=='complete' and result['state']=='ready'
    assert result['checkpoint']==old['checkpoint']
    value=public_analysis(json.loads(result['result']))
    assert value['analysis']['article']['version']==ARTICLE_VERSION
    assert value['material']==json.loads(old['result'])['material']
    count=len(c.article_calls)
    assert q.articles.next_task() is None and len(c.article_calls)==count
    q.publisher=lambda *args:(_ for _ in ()).throw(OSError('network'));q.fetch=lambda path:{}
    with pytest.raises(OSError):q.publish_ready()
    assert task(q)['result']==result['result'] and q.articles.next_task() is None


def test_failed_article_preserves_fulltext_and_retry_reuses_facts(tmp_path):
    q,c=article_queue(tmp_path,False);old=task(q)
    with q.db() as db:db.execute("UPDATE tasks SET state='published',published_result=result")
    for _ in range(3):
        row=q.articles.next_task();asyncio.run(q.articles.process(row))
        with q.db() as db:db.execute('UPDATE tasks SET article_next_attempt=0')
    row=task(q)
    assert row['article_state']=='failed' and row['state']=='published' and row['result']==old['result']
    assert len([p for p in c.article_calls if p.startswith('从以下已核实')])==2
    assert q.articles.next_task() is None
    assert '全文精读已完成' in q.snapshot()['tasks'][0]['label']
    q.retry(row['paper_id']);assert task(q)['article_attempts']==0
    c.approve=True;asyncio.run(q.articles.process(q.articles.next_task()))
    assert task(q)['article_state']=='complete'


def test_article_new_material_and_profile_get_new_revision_deleted_tasks_are_ignored(tmp_path):
    q,c=article_queue(tmp_path);row=q.articles.next_task();key=row['article_key']
    m=json.loads(row['material']);m['version']='b'*64
    with q.db() as db:db.execute('UPDATE tasks SET material=?',(json.dumps(m),))
    assert q.articles.next_task() is None  # Old reading cannot cover the new PDF.
    with q.db() as db:db.execute('UPDATE tasks SET material=?,enabled=0',(row['material'],))
    assert q.articles.next_task() is None
    assert article_key('a'*64,profile_context(tmp_path))!=article_key('a'*64,[])


def test_profile_upgrade_reuses_experimental_facts(tmp_path,monkeypatch):
    q,c=article_queue(tmp_path)
    asyncio.run(q.articles.process(q.articles.next_task()))
    directions=profile_context(tmp_path)
    directions[0]['keywords'].append('new research interest')
    monkeypatch.setattr('connectors.codex_bridge.article_queue.profile_context',lambda root:directions)
    row=q.articles.next_task()
    assert row is not None
    asyncio.run(q.articles.process(row))
    assert task(q)['article_state']=='complete'
    assert len([p for p in c.article_calls if p.startswith('从以下已核实')])==2


def test_public_status_does_not_expose_private_article_errors():
    s=public_status({'paper_id':'123456abcdef','state':'published','basis':'full_text',
        'updated_at':'2026-10-03T00:00:00+00:00','article_state':'failed','article_error':'private path'})
    assert 'private' not in json.dumps(s) and '全文精读已完成' in status_label(s)


def test_article_cancellation_preserves_stages_and_restart_can_continue(tmp_path):
    q,c=article_queue(tmp_path);old=task(q);normal=c.turn
    async def cancel(thread,prompt,images=()):
        if prompt.startswith('撰写一篇'):raise asyncio.CancelledError()
        async for e in normal(thread,prompt,images):yield e
    c.turn=cancel
    with pytest.raises(asyncio.CancelledError):asyncio.run(q.articles.process(q.articles.next_task()))
    assert task(q)['article_attempts']==0 and task(q)['result']==old['result']
    c.turn=normal
    again=ReadingQueue(tmp_path,tmp_path/'run',c,asyncio.Lock())
    again.articles.research=q.articles.research
    asyncio.run(again.articles.process(again.articles.next_task()))
    assert task(again)['article_state']=='complete'
    assert len([p for p in c.article_calls if p.startswith('从以下已核实')])==2


def test_new_source_during_article_generation_cannot_overwrite_new_revision(tmp_path):
    q,c=article_queue(tmp_path);old=task(q);normal=c.turn
    async def replace(thread,prompt,images=()):
        if prompt.startswith('撰写一篇'):
            p=json.loads(old['paper']);p['pdf_url']='https://example.org/new.pdf';q.enqueue(p)
        async for e in normal(thread,prompt,images):yield e
    c.turn=replace
    asyncio.run(q.articles.process(q.articles.next_task()))
    assert task(q)['revision']==old['revision']+1 and task(q)['result']==old['result']
    assert task(q)['state']=='pending'


def test_related_outage_retries_next_day_without_inventing_sources(tmp_path):
    calls=[]
    def fail(*args,**kwargs):calls.append(1);raise OSError('offline')
    now=datetime(2026,10,3,tzinfo=timezone.utc)
    value=related_literature(sample(),['a query','b query','c query'],tmp_path,search=fail,now=now)
    assert value['status']=='unavailable' and not value['papers'] and len(calls)==6
    assert value['expires_at']==now.timestamp()+86400


def test_invalid_new_article_does_not_poison_legacy_analysis(tmp_path):
    q,c=article_queue(tmp_path);v=json.loads(task(q)['result'])
    assert public_analysis(v)['analysis']['analysis_basis']=='full_text'
    v['analysis'].update(article=article_fixture(),article_updated_at='2026-10-03T00:00:00+00:00',article_profile='c'*64)
    v['analysis']['article']['experiments'][0]['samples']='**3 independent specimens**'
    assert public_analysis(v)['analysis']['article']['experiments'][0]['samples'].startswith('**3')
    v['analysis']['article']['related']['papers']=[{'title':'private','url':'http://127.0.0.1/doc','year':2026,'basis':'full_text'}]
    with pytest.raises(ValueError):public_analysis(v)


def test_local_article_uses_only_confirmed_public_result(tmp_path):
    from fastapi.testclient import TestClient
    from tests.test_codex_bridge import FakeCodex
    from connectors.codex_bridge.server import create_app, LOCAL_ORIGIN
    q,c=article_queue(tmp_path)
    asyncio.run(q.articles.process(q.articles.next_task()))
    identifier=task(q)['paper_id']
    app=create_app(tmp_path,runtime=tmp_path/'run',rpc=FakeCodex())
    with TestClient(app,base_url=LOCAL_ORIGIN) as client:
        assert client.get('/readings/'+identifier+'.html').status_code==404
        with q.db() as db:db.execute("UPDATE tasks SET published_result=result,state='published'")
        page=client.get('/readings/'+identifier+'.html')
        assert page.status_code==200 and '实验与验证一览' in page.text
        assert 'checkpoint' not in page.text and 'source.pdf' not in page.text
        diagram=client.get('/assets/reading-diagrams/'+identifier+'-study-mobile.svg')
        assert diagram.status_code==200 and '<svg' in diagram.text
        assert client.get('/readings/private.html').status_code==404
