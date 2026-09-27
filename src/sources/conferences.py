"""Cached, resumable official AI conference collection, independent of paid search."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urljoin, urlsplit, parse_qs, quote
from urllib.request import getproxies

import requests

from src.conferences import CONFIG, SOURCE, public_url
from src.editions import read, write
from src.models import RawRecord, SourceStatus, in_date_window
from src.paper_sources import PublicFetcher
from src.research_directions import load_profile, match_directions
from src.sources.conference_catalogs import directory, article, soup, text, seed, make_record, acl_records, openreview_records, EXCLUDED, WithdrawnPaper


@dataclass
class ConferenceStatus(SourceStatus):
    conferences: list = field(default_factory=list)


def failure(exc):
    response = getattr(exc, 'response', None)
    if response is not None:
        return f'HTTP {response.status_code}'
    if isinstance(exc, requests.Timeout):
        return '请求超时'
    if isinstance(exc, requests.RequestException):
        return '网络连接失败'
    if isinstance(exc, ValueError):
        return str(exc)[:120] or '来源内容无法核验'
    return type(exc).__name__


class ConferenceAdapter:
    name = SOURCE

    def __init__(self, config=None, cache_dir=None, profile=None, fetch=None):
        self.config = config or json.loads(CONFIG.read_text(encoding='utf-8'))
        self.cache = Path(cache_dir or CONFIG.parents[1]/'data/cache/conferences')
        self.profile = profile or load_profile()
        self.fetcher = fetch
        self.session = requests.Session()
        self.session.proxies.update({k:v for k,v in getproxies().items() if k in ('http','https')})
        self.session.headers['User-Agent']='daily-papers/1.0 (https://github.com/tengda-xmu/daily-papers)'
        self.network = PublicFetcher()
        self.network.session = self.session
        self._status = ConferenceStatus(SOURCE,'not_run')
        self.errors=[]; self.cached=0
        self.revoked=set()

    @property
    def status(self): return self._status

    def get(self, url):
        if not public_url(url) and not (url.startswith('https://api.crossref.org/works/') or url.startswith('https://api.openalex.org/works/')):
            raise ValueError('Unsupported conference source')
        key=hashlib.sha256(url.encode()).hexdigest()
        path=self.cache/'http'/f'{key}.json'; old=read(path)
        ttl=self.config.get('cache_hours',24)*3600
        if time.time()-old.get('checked_at',0)<ttl:
            self.cached+=1
            if old.get('error'):
                if 'body' not in old: raise ValueError(old['error'])
                self.errors.append(old['error'])
            return old['body']
        try:
            if self.fetcher:
                body=self.fetcher(url)
            else:
                time.sleep(.35)
                body, _ = self.network(url, limit=25_000_000)
            if isinstance(body,bytes):body=body.decode('utf-8')
            if not isinstance(body,str):body=json.dumps(body)
            write(path,{'url':url,'checked_at':time.time(),'success_at':time.time(),'body':body})
            return body
        except (ValueError,requests.RequestException,OSError) as exc:
            reason=failure(exc)
            write(path,{**old,'url':url,'checked_at':time.time(),'error':reason})
            if 'body' in old:
                self.errors.append(reason);return old['body']
            raise

    def indexes(self,c,year):
        provider=c['provider']; base=c['index']; result=[]
        if provider in ('neurips','pmlr','cvf'):
            s=soup(self.get(base))
            for a in s.select('a[href]'):
                link=urljoin(base,a['href']); title=text(a)
                if provider=='neurips':
                    m=re.search(r'/paper_files/paper/(\d{4})(?:/vol\d+-main-conference)?/?$',link)
                    if m and not EXCLUDED.search(title):result.append((int(m[1]),link))
                elif provider=='pmlr':
                    # The official directory supplies the conference name and year.
                    container=a.find_parent('li') or a.parent
                    title=text(container)
                    if re.fullmatch(r'https://proceedings.mlr.press/v\d+/?',link) and re.search(r'International Conference on Machine Learning|Proceedings of ICML \d{4}\b',title,re.I) and not EXCLUDED.search(title):
                        years=re.findall(r'\b20\d{2}\b',title)
                        if years:result.append((int(years[-1]),link))
                else:
                    m=re.search(rf'/{c["name"]}(\d{{4}})/?$',link)
                    if m:result.append((int(m[1]),link+'?day=all'))
            return list(dict.fromkeys(sorted((p for p in result if p[0]<=year),reverse=True)))[:2]
        if provider=='ecva':
            body=self.get(base)
            years=sorted({int(y) for y in re.findall(r'/eccv_(\d{4})/',body) if int(y)<=year},reverse=True)[:2]
            return [(y,base) for y in years]
        if provider=='aaai':
            s=soup(self.get(base))
            for a in s.select('a[href*="/issue/view/"]'):
                title=text(a)
                match=re.search(r'AAAI-(\d{2})\s+Technical Tracks?\b',title,re.I)
                if match and int('20'+match[1]) in (year,year-1) and not EXCLUDED.search(title):
                    result.append((int('20'+match[1]),urljoin(base,a['href'])))
            return list(dict.fromkeys(result))
        if provider=='ijcai':
            return [(y,f'https://www.ijcai.org/proceedings/{y}/') for y in (year,year-1)]
        if provider=='kdd':
            for y in (year,year-1):
                url=c.get('year_indexes', {}).get(str(y), f'https://kdd.org/kdd{y}/')
                try:
                    s=soup(self.get(url))
                    links=[urljoin(url,a['href']) for a in s.select('a[href]') if re.fullmatch(r'Research Track Papers|Papers',text(a),re.I)]
                    result.extend((y,link) for link in dict.fromkeys(links))
                    if not links: self.errors.append(f'{y} 尚无可核验论文目录')
                except (ValueError,requests.RequestException,OSError):
                    self.errors.append(f'{y} 目录暂不可用')
            return result
        return []

    def openreview(self,c,year,state):
        records=[]; notes=[]
        for y in (year,year-1):
            venue=c['venue'].format(year=y); cursor=state.setdefault('cursors',{}).get(venue,0)
            state.setdefault('seen', {})
            seen = set(state['seen'].get(venue, [])) if cursor else set()
            try:
                for _ in range(self.config.get('page_limit',3)):
                    url='https://api2.openreview.net/notes?'+urlencode({'content.venueid':venue,'limit':100,'offset':cursor,'sort':'pdate:desc'})
                    payload=json.loads(self.get(url))
                    if not isinstance(payload.get('notes'), list) or not isinstance(payload.get('count'), int):
                        raise ValueError('Invalid OpenReview response')
                    batch = openreview_records(c,payload,y)
                    records.extend(batch); seen.update(r.source_id for r in batch)
                    cursor+=len(payload.get('notes',[]))
                    if not payload.get('notes') or cursor>=payload['count']:
                        self.reconcile_year(state, y, seen)
                        cursor=0;break
                state['cursors'][venue]=cursor
                state['seen'][venue] = sorted(seen) if cursor else []
            except (ValueError,requests.RequestException,OSError):
                # The conference's own accepted Oral/Poster catalogue is a
                # separate public source, not an API authentication workaround.
                try:
                    url=f'https://iclr.cc/virtual/{y}/papers.html'; body=self.get(url)
                    match=re.search(r'"(/static/virtual/data/iclr-\d{4}-orals-posters.json)"',body)
                    if not match:raise ValueError('No official accepted paper dataset')
                    payload=json.loads(self.get(urljoin(url,match[1])))
                    if not isinstance(payload.get('results'), list) or payload.get('next'):
                        raise ValueError('Incomplete accepted paper dataset')
                    batch = []
                    for n in payload.get('results',[]):
                        if not n.get('visible',True) or not re.match(r'Accept\b',n.get('decision','')) or EXCLUDED.search(n.get('decision','')):continue
                        if parse_qs(urlsplit(n.get('sourceurl','')).query).get('id')!=[venue]:continue
                        paper_url=n.get('paper_url') or ''
                        if urlsplit(paper_url).hostname!='openreview.net':continue
                        identifier=parse_qs(urlsplit(paper_url).query).get('id',[''])[0]
                        if not identifier:continue
                        row=seed(c,y,n['name'],paper_url,proof_url=urljoin(url,n.get('virtualsite_url') or url),date_kind='acceptance')
                        batch.append(make_record(c,row,authors=[a['fullname'] for a in n.get('authors',[])],abstract=n.get('abstract',''),
                                                   pdf='https://openreview.net/pdf?id='+identifier))
                    self.reconcile_year(state, y, {r.source_id for r in batch})
                    records.extend(batch)
                    notes.append(f'{y} 使用官方 Oral/Poster 录用目录')
                    state['cursors'][venue]=0
                except (ValueError,requests.RequestException,OSError):self.errors.append(f'{y} OpenReview 和官方录用目录暂不可用')
        return records,notes

    @staticmethod
    def reconcile_year(state, year, identifiers):
        removed = {k for k,v in state['records'].items()
                   if v.get('raw_metadata', {}).get('conference', {}).get('year') == year and k not in identifiers}
        state['revoked'] = sorted(set(state.get('revoked', [])) | removed)
        state['records'] = {k:v for k,v in state['records'].items() if k not in removed}

    def crossref(self,c,row):
        doi=row['doi']; payload=json.loads(self.get('https://api.crossref.org/works/'+quote(doi,safe='')))
        item=payload['message']
        if item.get('DOI','').lower()!=doi.lower():raise ValueError('DOI mismatch')
        title=(item.get('title') or [''])[0]
        from src.paper_sources import normalized_title
        if normalized_title(title)!=normalized_title(row['title']):raise ValueError('Conference title mismatch')
        parts=(item.get('published') or {}).get('date-parts', [[]])[0]
        date='-'.join(str(v) if i==0 else f'{v:02}' for i,v in enumerate(parts))
        abstract=text(soup(item.get('abstract','')))
        pdf=next((l['URL'] for l in item.get('link',[]) if l.get('content-type')=='application/pdf' and public_url(l.get('URL',''))),'')
        if not abstract:
            try:
                other=json.loads(self.get('https://api.openalex.org/works/https://doi.org/'+quote(doi,safe='/')))
                if str(other.get('doi','')).lower()=='https://doi.org/'+doi.lower():
                    inverted=other.get('abstract_inverted_index') or {}
                    abstract=' '.join(w for _,w in sorted((i,w) for w,indexes in inverted.items() for i in indexes))
                    pdf=pdf or next((l['pdf_url'] for l in other.get('locations',[]) if l.get('is_oa') and public_url(l.get('pdf_url',''))),'')
            except (ValueError,requests.RequestException,OSError):pass
        pdf = pdf or 'https://dl.acm.org/doi/pdf/' + doi
        return make_record(c,row,title=title,authors=[' '.join(filter(None,[a.get('given'),a.get('family')])) for a in item.get('author',[])],
                           abstract=abstract,date=date,doi=doi,pdf=pdf)

    def collect(self,c,until):
        path=self.cache/(c['id']+'.json'); state=read(path,{'records':{},'cursors':{},'processed':{}})
        state.setdefault('records',{});state.setdefault('processed',{})
        self.errors=[]; before=self.cached; records=[]; notes=[]; seeds=[]; year=until.year
        started=time.monotonic()
        try:
            if c['provider']=='openreview':
                records,notes=self.openreview(c,year,state)
            elif c['provider']=='acl':
                for y in (year,year-1):
                    try:
                        url=f'https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/{y}.{c["id"]}.xml'
                        batch = acl_records(c,self.get(url),y)
                        self.reconcile_year(state, y, {r.source_id for r in batch})
                        records.extend(batch)
                    except (ValueError,requests.RequestException,OSError) as exc:self.errors.append(f'{y} 官方元数据暂不可用（{failure(exc)}）')
            else:
                indexes=self.indexes(c,year)
                if not indexes:raise ValueError('No verified proceedings directory')
                directories = state.setdefault('directories', {})
                cursor = state.get('directory_cursor', 0) % len(indexes)
                count = min(len(indexes), self.config.get('page_limit', 3))
                for step in range(count):
                    y,url = indexes[(cursor + step) % len(indexes)]
                    key = f'{y}:{url}'
                    try:
                        batch = directory(c,self.get(url),url,y)
                        if not batch: raise ValueError('No verified main-track papers in directory')
                        previous = {r['url'] for r in directories.get(key, [])}
                        current = {r['url'] for r in batch}
                        for identifier in previous - current:
                            state['records'].pop(identifier, None)
                        state['revoked'] = sorted(set(state.get('revoked', [])) | (previous - current))
                        directories[key] = batch
                    except (ValueError,requests.RequestException,OSError) as exc:self.errors.append(f'{y} 论文目录暂不可用（{failure(exc)}）')
                state['directory_cursor'] = (cursor + count) % len(indexes)
                for y,url in indexes:
                    seeds.extend(directories.get(f'{y}:{url}', []))
                remaining = sum(f'{y}:{url}' not in directories for y,url in indexes)
                if remaining: notes.append(f'还有 {remaining} 个目录分页待采集')
                if not seeds:raise ValueError('No verified main-track papers')
                # Prioritize title matches, then visit all remaining entries over
                # subsequent runs; abstracts may reveal additional relevance.
                terms=[k.casefold() for d in self.profile['directions'] if d.get('enabled') for k in d['keywords']]
                seeds=list({r['url']:r for r in seeds}.values())
                seeds.sort(key=lambda r:(r['url'] in state['processed'],state['processed'].get(r['url'],0),
                                         -sum(t in r['title'].casefold() for t in terms),-r['year'],r['url']))
                for row in seeds[:self.config.get('detail_limit',20)]:
                    if time.monotonic()-started>90:break
                    try:
                        value=self.crossref(c,row) if c['provider']=='kdd' else article(c,row,self.get(row['url']))
                        records.append(value);state['processed'][row['url']]=time.time()
                    except WithdrawnPaper:
                        state['records'].pop(row['url'], None)
                        state['revoked'] = sorted(set(state.get('revoked', [])) | {row['url']})
                        state['processed'][row['url']]=time.time()
                        notes.append('已排除撤回论文')
                    except (ValueError,KeyError,requests.RequestException,OSError) as exc:
                        self.errors.append(f'论文详情：{failure(exc)}')
                        # Failed entries rotate as well; their old record remains.
                        state['processed'][row['url']]=time.time()
            for r in records:state['records'][r.source_id]=r.to_dict()
            state['revoked'] = sorted(set(state.get('revoked', [])) - {r.source_id for r in records})
            if not self.errors:state['last_success']=datetime.now(timezone.utc).isoformat()
        except (ValueError,KeyError,requests.RequestException,OSError) as exc:
            self.errors.append(failure(exc))
        write(path,state)
        self.revoked.update(state.get('revoked', []))
        all_records=[RawRecord(**r) for r in state['records'].values()]
        return all_records,{'id':c['id'],'name':c['name'],'status':'partial' if self.errors and all_records else 'error' if self.errors else 'ok',
                           'count':len(all_records),'matched':0,'cached':self.cached-before,'last_success':state.get('last_success',''),
                           'message':'；'.join(dict.fromkeys([*notes,*self.errors]))[:500],
                           'pending':max(0,len(seeds)-len(state['records'])) if seeds else 0}

    def fetch(self,since,until):
        rows=[]; details=[]
        for c in self.config['conferences']:
            records,status=self.collect(c,until)
            selected=[r for r in records if in_date_window(r.published_at,since,until) and match_directions(r,self.profile)]
            status['matched']=len(selected)
            if status['status']=='ok' and not selected:status['status']='no_data'
            rows.extend(selected);details.append(status)
        failed=any(s['status'] in ('partial','error') for s in details)
        state='partial' if failed and any(s['count'] for s in details) else 'error' if failed else 'ok' if rows else 'no_data'
        self._status=ConferenceStatus(SOURCE,state,len(rows),'官方主会论文；缓存 24 小时；按研究方向和现有时间窗口筛选。',details)
        return rows
