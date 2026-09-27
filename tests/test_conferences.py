"""Official acceptance, incremental collection and existing recommendation contracts."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from src.conferences import BY_ID, CONFERENCES, SOURCE, GROUP, info, public_url
from src.sources.conference_catalogs import directory, article, seed, make_record, acl_records, openreview_records
from src.sources.conferences import ConferenceAdapter
from src.models import RawRecord, SourceStatus
from src.catalog import paper_facets
from src.pipeline import deduplicate, _sort_key, run_pipeline, build_adapters, record_id
from src.editions import History, append, remember_candidates, candidates, delete_editions, manifest, revision, write, read
from src.paper_sources import allowed, discover
from src.manual_search import SOURCES
from tools.build_site import render, paper_card, source_status_panel

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
TITLE = 'Neural networks for bearing fault diagnosis'
ABSTRACT = 'We evaluate neural network fault diagnosis using bearing monitoring data with controlled comparisons across operational conditions. The method uses measured vibration signals.'


def record(i=1, cid='iclr', title=TITLE):
    c = BY_ID[cid]
    row = seed(c, 2026, title + f' {i}', f'https://openreview.net/forum?id=test{i}')
    return make_record(c, row, authors=['Alice Smith'], abstract=ABSTRACT, date='2026-09-25',
                       pdf=f'https://openreview.net/pdf?id=test{i}')


@pytest.mark.parametrize('cid,html,url,count', [
    ('neurips', '<li data-track="conference"><a href="/paper_files/paper/2026/hash/a-Abstract-Conference.html">Main</a></li><li data-track="datasets"><a href="/paper_files/paper/2026/hash/b-Abstract-Conference.html">Dataset</a></li><li><a href="/hash/c-Abstract-Workshop.html">Workshop</a></li>', 'https://proceedings.neurips.cc/', 1),
    ('icml', '<h2>International Conference on Machine Learning</h2><div class="paper"><p class="title">Main</p><a href="a.html">abs</a></div>', 'https://proceedings.mlr.press/v267/', 1),
    ('cvpr', '<a href="/content/CVPR2026/html/a.html">Main</a><a href="/content/CVPR2026W/html/b.html">Workshop</a><a href="/content/ICCV2025/html/c.html">Other</a>', 'https://openaccess.thecvf.com/CVPR2026', 1),
    ('iccv', '<a href="/content/ICCV2026/html/a.html">Main</a><a href="/content/ICCV2026W/html/b.html">Workshop</a>', 'https://openaccess.thecvf.com/ICCV2026', 1),
    ('eccv', '<a href="/papers/eccv_2026/papers_ECCV/html/a.php">Main</a><a href="/papers/eccv_2026/workshops/html/b.php">Workshop</a>', 'https://www.ecva.net/papers.php', 1),
    ('ijcai', '<div class="section"><h2 class="section_title">Main Track</h2><div class="paper_wrapper"><div class="title">Main</div><a href="/proceedings/2026/1">Details</a></div></div><div class="section"><h2 class="section_title">Doctoral Consortium</h2><div class="paper_wrapper"><div class="title">Other</div><a href="/proceedings/2026/2">Details</a></div></div>', 'https://www.ijcai.org/proceedings/2026/', 1),
    ('aaai', '<div class="section"><h2>AAAI Technical Track on Machine Learning</h2><div class="obj_article_summary"><h3 class="title"><a href="/index.php/AAAI/article/view/1">Main</a></h3></div></div><div class="section"><h2>AAAI Demonstrations</h2><div class="obj_article_summary"><h3 class="title"><a href="/index.php/AAAI/article/view/2">Demo</a></h3></div></div>', 'https://ojs.aaai.org/', 1),
    ('kdd', '<h2>Research Track Papers</h2><table><tr><td><strong>Main</strong><br>DOI: https://doi.org/10.1145/1.2</td></tr></table>', 'https://kdd.org/kdd2026/research-track-papers/', 1),
])
def test_only_main_directories(cid, html, url, count):
    rows = directory(BY_ID[cid], html, url, 2026)
    assert len(rows) == count
    assert all(r['title'] == 'Main' and r['year'] == 2026 for r in rows)


def test_wrong_pmlr_volume_and_article_mismatch_rejected():
    with pytest.raises(ValueError):
        directory(BY_ID['icml'], '<h2>ICML Workshop</h2>', 'https://proceedings.mlr.press/v267/', 2026)
    with pytest.raises(ValueError):
        article(BY_ID['iclr'], seed(BY_ID['iclr'], 2026, 'A', 'https://openreview.net/forum?id=A'), '<meta name="citation_title" content="B">')


@pytest.mark.parametrize('cid', ['acl', 'emnlp'])
def test_anthology_long_short_excludes_findings_frontmatter_withdrawals(cid):
    xml = f'''<collection id="2026.{cid}"><volume id="long"><meta><booktitle>Main conference</booktitle><month>September</month></meta>
      <paper id="0"><title>Proceedings</title></paper><paper id="1"><title>A <i>main</i> paper</title><author><first>Alice</first><last>Smith</last></author><abstract>{ABSTRACT}</abstract><pdf>1</pdf></paper>
      <paper id="2"><title>Withdrawn</title><retraction/></paper></volume><volume id="short"><meta><booktitle>Main short</booktitle><month>September</month></meta><paper id="1"><title>Short</title></paper></volume>
      <volume id="findings"><meta><booktitle>Findings</booktitle></meta><paper id="1"><title>Exclude</title></paper></volume></collection>'''
    rows = acl_records(BY_ID[cid], xml, 2026)
    assert len(rows) == 2 and rows[0].title == 'A main paper'
    assert rows[0].published_at == '2026-09' and len(rows[0].abstract) > 120
    assert rows[0].oa_url == f'https://aclanthology.org/2026.{cid}-long.1.pdf'
    assert info(rows[0].to_dict())['date_precision'] == 'month'
    with pytest.raises(ValueError):
        acl_records(BY_ID[cid], xml, 2025)


def note(i, venue='ICLR.cc/2026/Conference', **extras):
    return {'id': str(i), 'forum': str(i), 'content': {k: {'value': v} for k,v in
        dict(venueid=venue, venue='ICLR 2026 Poster', title=TITLE+str(i), authors=['Alice Smith'], abstract=ABSTRACT).items()}, **extras}


def test_openreview_requires_exact_accepted_venue_and_original_note():
    rows = openreview_records(BY_ID['iclr'], {'notes': [note(1),note(2, 'ICLR.cc/2026/Conference/Submission'),note(3,ddate=123),note(4,forum='1')]}, 2026)
    assert len(rows) == 1 and rows[0].published_at == '2026'
    assert info(rows[0].to_dict())['date_kind'] == 'acceptance'


def test_kdd_current_json_tracks_and_safe_typographic_title_match():
    from src.paper_sources import matches
    html='<h1>Papers</h1><script>const cycle1Papers = '+json.dumps([
        {'track':'rtp','title':'Main','url':'https://doi.org/10.1145/1.2'},
        {'track':'ads','title':'Applied','url':'https://doi.org/10.1145/1.3'},
        {'track':'dtb','title':'Dataset','url':'https://doi.org/10.1145/1.4'}])+ ';</script>'
    rows=directory(BY_ID['kdd'],html,'https://kdd2026.kdd.org/papers/',2026)
    assert [r['title'] for r in rows] == ['Main']
    assert info(make_record(BY_ID['kdd'],rows[0]).to_dict())
    assert matches('Long scientific title with 360° video diﬀusion',{'title':'Long scientific title with 360deg video diffusion'})
    assert not matches('A different 360° video diffusion study',{'title':'Long scientific title with 360deg video diffusion'})


def test_withdrawn_conference_is_not_reintroduced_from_saved_candidates(tmp_path):
    p={**record().to_dict(),'id':'123456789abc'}
    remember_candidates(tmp_path,[p])
    class Adapter:
        status=SourceStatus(SOURCE,'ok')
        revoked={p['landing_url']}
        def fetch(self,*args):return []
    result=run_pipeline(until=NOW,adapters=[Adapter()],output_path=tmp_path/'daily.json')
    assert result['core']==result['extended']==[]
    assert candidates(tmp_path)[p['id']]['title']==p['title']


def test_year_precision_and_window_are_not_collection_dates(tmp_path):
    rows=[record(i) for i in range(5)]
    for r,date in zip(rows,['2026-09-28','2026-08-01','2027-01-01','2026','2026-09']):r.published_at=date
    adapter=ConferenceAdapter(config={'conferences':[BY_ID['iclr']]},cache_dir=tmp_path)
    adapter.collect=lambda *args:(rows,{'id':'iclr','status':'ok','count':5})
    result=adapter.fetch(datetime(2026,9,1,tzinfo=timezone.utc),NOW)
    assert {r.published_at for r in result}=={'2026-09-28','2026','2026-09'}


def test_resumes_openreview_pages_then_revokes_missing_acceptance(tmp_path):
    calls=[]; papers=[note(i) for i in range(201)]
    def fetch(url):
        q=parse_qs(urlsplit(url).query); offset=int(q['offset'][0]); calls.append(offset)
        return {'notes':papers[offset:offset+100], 'count':len(papers)} if '2026' in q['content.venueid'][0] else {'notes':[], 'count':0}
    config={'conferences':[BY_ID['iclr']], 'page_limit':1, 'cache_hours':0}
    for expected in (100,200,201):
        adapter=ConferenceAdapter(config=config,cache_dir=tmp_path,fetch=fetch)
        rows,state=adapter.collect(BY_ID['iclr'],NOW)
        assert len(rows)==expected and state['status']=='ok'
    papers.pop()
    adapter.collect(BY_ID['iclr'], NOW)
    rows,state=adapter.collect(BY_ID['iclr'],NOW)
    assert len(rows)==200
    assert calls[:6] == [0,0,100,0,200,0]


def test_cache_limits_failure_and_keeps_successful_records(tmp_path):
    calls=[]
    def fetch(url):
        calls.append(url)
        if '2025' in url: raise requests.HTTPError('missing')
        if url.endswith('/2026/'):
            return '<div class="section"><h2 class="section_title">Main Track</h2>'+''.join(f'<div class="paper_wrapper"><div class="title">{TITLE} {i}</div><a href="/proceedings/2026/{i}">Details</a></div>' for i in (1,2))+'</div>'
        i=url.rsplit('/',1)[1]
        return f'<meta name="citation_title" content="{TITLE} {i}"><div class="abstract">{ABSTRACT}</div>'
    config={'conferences':[BY_ID['ijcai']], 'page_limit':2, 'detail_limit':1,'cache_hours':24}
    adapter=ConferenceAdapter(config=config,cache_dir=tmp_path,fetch=fetch)
    one,state=adapter.collect(BY_ID['ijcai'],NOW)
    assert len(one)==1 and state['status']=='partial'
    two,state=adapter.collect(BY_ID['ijcai'],NOW)
    assert len(two)==2 and state['pending']==0
    assert calls.count('https://www.ijcai.org/proceedings/2025/') == 1
    assert calls.count('https://www.ijcai.org/proceedings/2026/') == 1
    # Expired network cache can still serve the last verified records.
    adapter.config['cache_hours']=0
    adapter.fetcher=lambda _: (_ for _ in ()).throw(requests.ConnectionError())
    rows,state=adapter.collect(BY_ID['ijcai'],NOW)
    assert len(rows)==2 and state['status']=='partial'


def test_official_host_scope_and_unverified_venue():
    pdf='https://raw.githubusercontent.com/mlresearch/v267/main/assets/a/a.pdf'
    assert public_url(pdf) and allowed(pdf)
    for bad in ('https://raw.githubusercontent.com/evil/v267/main/a.pdf', 'https://raw.githubusercontent.com/mlresearch/v267/main/code.py',
                'https://openreview.net.evil.org/pdf?id=1','https://user@openreview.net/pdf?id=1','https://openreview.net:8080/pdf?id=1'):
        assert not public_url(bad) and not allowed(bad)
    fake=record().to_dict(); fake['raw_metadata']['conference']['proof_url']='https://doi.org/10.1/fake'
    assert info(fake) is None
    assert paper_facets({'venue':'ICLR 2026', 'source':'arXiv'})['venue_group'] != GROUP


def test_arxiv_index_and_conference_merge_without_changing_stable_identity(tmp_path):
    r=record(); r.raw_metadata['source_links']=['https://arxiv.org/abs/2609.12345v2']
    preprint=RawRecord('arXiv','https://arxiv.org/abs/2609.12345v1',r.title,authors=['Smith, Alice'])
    p={**preprint.to_dict(),'id':record_id(preprint)}
    remember_candidates(tmp_path,[p])
    r.doi='10.1234/official'
    merged=deduplicate([preprint,r])
    assert len(merged)==1
    m=merged[0].to_dict()
    assert info(m)['name']=='ICLR' and paper_facets(m)['journal']=='ICLR'
    assert History(tmp_path).identity(m) == p['id']
    m['id']=p['id']; remember_candidates(tmp_path,[m])
    assert info(candidates(tmp_path)[p['id']])['name']=='ICLR'
    assert 'https://arxiv.org/abs/2609.12345v2' in candidates(tmp_path)[p['id']]['raw_metadata']['source_links']


def test_joint_selection_counts_relevance_and_deletion(tmp_path,monkeypatch):
    rows=[record(i) for i in range(1,13)]+[record(99,title='Clinical language translation benchmarks')]
    r=rows[0]; ordinary=deepcopy(r); ordinary.venue='Engineering Structures';ordinary.raw_metadata={}
    assert _sort_key(r)==_sort_key(ordinary)
    cns=deepcopy(ordinary);cns.venue='Nature Communications'
    assert _sort_key(cns)>_sort_key(r)
    class Adapter:
        name=SOURCE
        status=SourceStatus(SOURCE,'ok')
        def fetch(self,*args):return deepcopy(rows)
    # Make the unrelated paper unrelated in both title and abstract.
    rows[-1].abstract='Translation of languages in legal documents.'
    monkeypatch.setenv('GITHUB_RUN_ID','conference-first')
    first=run_pipeline(until=NOW,adapters=[Adapter()],output_path=tmp_path/'daily.json')
    assert len(first['core'])==5 and len(first['extended'])==5
    assert all(info(p) and 'Clinical' not in p['title'] for p in first['core']+first['extended'])
    current=append(tmp_path,{'update_run_id':'keep','generated_at':NOW.isoformat(),'core':[{**record(20).to_dict(),'id':'aaaaaaaaaaaa'}],'extended':[]})
    write(tmp_path/'daily.json',current)
    delete_editions(tmp_path,[first['edition']['id']],revision(manifest(tmp_path)),'test-conference-deletion')
    monkeypatch.setenv('GITHUB_RUN_ID','conference-again')
    again=run_pipeline(until=NOW,adapters=[],output_path=tmp_path/'daily.json')
    assert {p['id'] for p in again['core']+again['extended']} == {p['id'] for p in first['core']+first['extended']}
    assert all(info(p) for p in again['core']+again['extended'])


def test_site_and_manual_search_contract():
    p={**record().to_dict(),'id':'123456789abc','title_zh':'轴承故障诊断'}
    html=render({'core':[p],'extended':[p]})
    assert 'ICLR 2026 · 主会论文' in html and '期刊／会议' in html
    assert all(f'>{c["name"]}</option>' in html for c in CONFERENCES)
    assert '预印本版本' not in paper_card(p,'core')
    assert 'paper-figure' not in paper_card(p,'extended')
    assert SOURCE not in {s['id'] for s in SOURCES}
    assert SOURCE in {a.name for a in build_adapters()}
    panel=source_status_panel({'source_status':{SOURCE:{'status':'partial','conferences':[{'id':'iclr','status':'partial','count':10,'matched':2,'message':'HTTP 429'}]}}})
    assert '各会议采集情况' in panel and 'HTTP 429' in panel and all(c['name'] in panel for c in CONFERENCES)


def test_pdf_discovery_keeps_official_abstract_and_queue_metadata(tmp_path):
    import asyncio
    from connectors.codex_bridge.reading_queue import ReadingQueue
    from connectors.codex_bridge.documents import pdf_candidates
    from tests.test_reading_queue import FakeClient, task
    p={**record().to_dict(),'id':'123456789abc'}
    q=ReadingQueue(tmp_path,tmp_path/'runtime',FakeClient({}),asyncio.Lock())
    q.enqueue(p)
    clean=json.loads(task(q)['paper'])
    assert info(clean) and pdf_candidates(clean)[0]=='https://openreview.net/pdf?id=test1'
    result=discover(clean,lambda _: (_ for _ in ()).throw(AssertionError('No API needed for no-DOI record')))
    assert result['abstract']==ABSTRACT and 'https://openreview.net/pdf?id=test1' in result['urls']


def test_source_status_refresh_does_not_require_new_batch(tmp_path,monkeypatch):
    p={**record().to_dict(),'id':'123456789abc'}
    first=append(tmp_path,{'update_run_id':'keep','generated_at':NOW.isoformat(),'core':[p],'extended':[]})
    write(tmp_path/'daily.json',first)
    class Adapter:
        status=SourceStatus(SOURCE,'no_data',message='No recent relevant papers')
        def fetch(self,*args):return []
    monkeypatch.setenv('GITHUB_RUN_ID','status-only')
    result=run_pipeline(until=NOW,adapters=[Adapter()],output_path=tmp_path/'daily.json')
    assert result['edition']==first['edition'] and result['core']==first['core']
    assert result['source_status'][SOURCE]['status']=='no_data'


def test_no_doi_conference_figure_is_identity_matched_and_licensed(tmp_path,monkeypatch):
    from src.figures import figure_key
    from tools import collect_figures
    from tests.test_figure_collection import png, META
    p={**record().to_dict(),'id':'123456789abc'}
    key=figure_key(p)
    assert key.startswith('conference:') and figure_key({'id':p['id']})==''
    write(tmp_path/'data/daily.json',{'core':[p],'extended':[]})
    calls=[]
    def figure(doi,paper,fetch):
        calls.append((doi,paper['title']));return META,png()
    monkeypatch.setattr(collect_figures,'publisher_figure',figure)
    assert collect_figures.collect(root=tmp_path,now=NOW)['saved']==1
    assert key in read(tmp_path/'data/figures/catalog.json')['entries']
    assert collect_figures.collect(root=tmp_path,now=NOW)['existing']==1 and len(calls)==1
