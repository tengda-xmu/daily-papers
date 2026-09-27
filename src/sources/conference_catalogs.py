"""Parsers for official main-conference directories (no search-engine claims)."""
from datetime import datetime, timezone
import json
import re
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from src.conferences import SOURCE, public_url
from src.models import RawRecord

EXCLUDED = re.compile(r'workshop|findings|tutorial|demonstration|doctoral|student research|withdrawn|retracted|rejected|creative.ai|position.paper', re.I)


class WithdrawnPaper(ValueError):
    pass


def soup(body):
    return BeautifulSoup(body, 'html.parser')


def text(node):
    return node.get_text(' ', strip=True) if node else ''


def seed(c, year, title, url, **kw):
    if not title.strip() or not public_url(url):
        raise ValueError('Invalid official paper link')
    return {'title': title, 'url': url, 'year': int(year), 'conference': c['id'], **kw}


def directory(c, body, url, year):
    """Return only identities whose membership is established by this directory."""
    s = soup(body); rows = []; provider = c['provider']
    if provider == 'neurips':
        for a in s.select('li a[href*="/hash/"]'):
            li = a.find_parent('li'); track = li.get('data-track', '')
            if track and track != 'conference':
                continue
            if EXCLUDED.search(a['href']) or 'Abstract-Conference.html' not in a['href']:
                continue
            rows.append(seed(c, year, text(a), urljoin(url,a['href'])))
    elif provider == 'pmlr':
        # PMLR also hosts workshops, so both the volume and each entry must match.
        heading = ' '.join(text(h) for h in s.select('h1,h2'))
        if not re.search(r'International Conference on Machine Learning', heading, re.I) or EXCLUDED.search(heading):
            raise ValueError('Not an ICML main proceedings volume')
        for p in s.select('.paper'):
            a = next((a for a in p.select('a[href]') if text(a) == 'abs'), None)
            if a:
                rows.append(seed(c,year,text(p.select_one('.title')),urljoin(url,a['href'])))
    elif provider in ('cvf','ecva'):
        pattern = (rf'/content/{c["name"]}{year}/html/[^/]+\.html$' if provider == 'cvf'
                   else rf'/papers/eccv_{year}/papers_ECCV/html/[^/]+\.php$')
        for a in s.select('a[href]'):
            link = urljoin(url,a['href'])
            if re.search(pattern,link):
                rows.append(seed(c,year,text(a),link))
    elif provider == 'ijcai':
        for p in s.select('.paper_wrapper'):
            section = p.find_parent(class_='section')
            heading = text(section.select_one('.section_title')) if section else ''
            if not re.search(r'main track',heading,re.I):
                continue
            a = next((a for a in p.select('a[href]') if re.search(r'/proceedings/\d{4}/\d+/?$',a['href'])),None)
            if a:
                rows.append(seed(c,year,text(p.select_one('.title')),urljoin(url,a['href']),
                                 authors=text(p.select_one('.authors'))))
    elif provider == 'aaai':
        for section in s.select('.section'):
            heading = text(section.select_one('h2,h3'))
            if not re.search(r'technical track|main track',heading,re.I) or EXCLUDED.search(heading):
                continue
            for a in section.select('.obj_article_summary .title a[href]'):
                rows.append(seed(c,year,text(a),urljoin(url,a['href'])))
    elif provider == 'kdd':
        # KDD 2026 embeds both cycles as JSON; "rtp" is its Research Track.
        # Other tracks share the page and must not inherit that classification.
        for script in s.select('script:not([src])'):
            for match in re.finditer(r'const\s+cycle[12]Papers\s*=\s*', script.get_text()):
                payload, _ = json.JSONDecoder().raw_decode(script.get_text()[match.end():])
                for p in payload:
                    if p.get('track') != 'rtp': continue
                    doi = re.fullmatch(r'https://doi\.org/(10\.1145/[\d.]+)', p.get('url',''))
                    if doi:
                        rows.append(seed(c,year,p['title'],p['url'],doi=doi[1],proof_url=url))
        if rows:
            return list({r['url']:r for r in rows}.values())
        heading = ' '.join(text(h) for h in s.select('h1,h2'))
        if not re.search(r'research.track.*papers',heading,re.I) or EXCLUDED.search(heading):
            raise ValueError('Not a KDD research-track accepted paper list')
        for cell in s.select('td'):
            title = cell.select_one('strong')
            match = re.search(r'10\.1145/[\d.]+',text(cell))
            if title and match:
                rows.append(seed(c,year,text(title),'https://doi.org/'+match[0],
                                 doi=match[0],proof_url=url))
    return list({r['url']:r for r in rows}.values())


def make_record(c, row, *, title='', authors=None, abstract='', date='', doi='', pdf='', extra=None):
    year = row['year']; date = str(date or row.get('date') or year).replace('/','-')
    date = date if re.fullmatch(r'\d{4}(?:-\d{2}(?:-\d{2})?)?',date) else str(year)
    conference = {'id':c['id'],'name':c['name'],'year':year,'track':'main','verified':True,
                  'proof_url':row.get('proof_url',row['url']), 'paper_url':row['url'],
                  'doi':doi or row.get('doi',''),
                  'pdf_url':pdf if public_url(pdf) else '', 'date_kind':row.get('date_kind','publication'),
                  'date_precision':{4:'year',7:'month',10:'day'}[len(date)]}
    metadata = {'conference':conference, 'abstract_kind':'publisher_abstract' if abstract else 'missing',
                'date_precision':conference['date_precision'], 'date_kind':conference['date_kind'],
                'bibliography':{'type':'proceedings-article'}, 'sources':[SOURCE],
                'source_links':list(dict.fromkeys([row['url'],conference['proof_url']])), **(extra or {})}
    return RawRecord(SOURCE,row['url'],title or row['title'],authors=authors or row.get('authors',[]),
                     venue=f"{c['name']} {year}",abstract=abstract,published_at=date,doi=doi or row.get('doi',''),
                     landing_url=row['url'],oa_url=conference['pdf_url'],source_score=.68,raw_metadata=metadata)


def article(c, row, body):
    s = soup(body)
    def meta(name):
        node=s.select_one(f'meta[name="{name}"]');return node.get('content','') if node else ''
    title=meta('citation_title') or text(s.select_one('#papertitle,.title,h1,h2'))
    from src.paper_sources import normalized_title
    if s.select_one('.retraction_notice,.withdrawn-notice') or re.match(r'^\[(?:withdrawn|retracted)\]', title, re.I):
        raise WithdrawnPaper('Official article withdrawn')
    if normalized_title(title) != normalized_title(row['title']):
        raise ValueError('Official article identity mismatch')
    abstract=text(s.select_one('#abstract,.abstract,.item.abstract,.paper-abstract')) or meta('citation_abstract')
    if not abstract and c['provider']=='neurips':
        heading=next((n for n in s.select('h2,h3,h4') if text(n).casefold()=='abstract'),None)
        if heading:
            paragraph=heading.find_next_sibling(); abstract=text(paragraph)
    authors=[a.get('content','') for a in s.select('meta[name="citation_author"]')]
    if not authors:
        authors=text(s.select_one('#authors,.authors')).strip(' ;*') or row.get('authors',[])
    pdf=meta('citation_pdf_url')
    if not pdf:
        a=next((a for a in s.select('a[href]') if text(a).strip().casefold() in ('pdf','paper','download pdf')
                and not re.search(r'supp|appendix',a['href'],re.I)),None)
        pdf=urljoin(row['url'],a['href']) if a else ''
    else:
        pdf=urljoin(row['url'],pdf)
    links=[urljoin(row['url'],a['href']) for a in s.select('a[href]') if re.search(r'arxiv.org/(abs|pdf)/|openreview.net/forum',a['href'])]
    doi=meta('citation_doi')
    if not doi:
        d=next((a['href'].split('doi.org/',1)[1] for a in s.select('a[href]') if 'doi.org/10.' in a['href']), '')
        doi=d
    return make_record(c,row,title=title,authors=authors,abstract=abstract,
                       date=meta('citation_publication_date') or meta('citation_date'),doi=doi,pdf=pdf,
                       extra={'source_links':list(dict.fromkeys([row['url'],*links]))})


def acl_records(c, body, year):
    try:
        tree=ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError('Invalid Anthology XML') from exc
    result=[]
    expected=f'{year}.{c["id"]}'
    if tree.get('id')!=expected: raise ValueError('Wrong Anthology collection')
    node_text=lambda n: ''.join(n.itertext()).strip() if n is not None else ''
    for volume in tree.findall('volume'):
        if volume.get('id') not in ('long','short','main'): continue
        meta=volume.find('meta'); title=node_text(meta.find('booktitle'))
        if EXCLUDED.search(title): continue
        month=node_text(meta.find('month')).split('-')[0].split('–')[0]
        try: date=f'{year}-{datetime.strptime(month,"%B").month:02}'
        except ValueError: date=str(year)
        for p in volume.findall('paper'):
            if p.find('retraction') is not None or p.get('withdrawn')=='true' or not p.get('id', '').isdigit() or int(p.get('id')) == 0: continue
            identifier=f'{expected}-{volume.get("id")}.{p.get("id")}'
            url=f'https://aclanthology.org/{identifier}/'
            row=seed(c,year,node_text(p.find('title')),url,date=date)
            authors=[' '.join(filter(None,[node_text(a.find('first')),node_text(a.find('last'))])) for a in p.findall('author')]
            result.append(make_record(c,row,authors=authors,abstract=node_text(p.find('abstract')),
                                      doi=node_text(p.find('doi')),pdf=url.rstrip('/')+'.pdf' if p.find('pdf') is not None else ''))
    return result


def openreview_records(c, payload, year):
    expected=c['venue'].format(year=year); result=[]
    for n in payload.get('notes',[]):
        content={k:v.get('value') if isinstance(v,dict) else v for k,v in n.get('content',{}).items()}
        if content.get('venueid')!=expected or n.get('ddate') or EXCLUDED.search(str(content.get('venue',''))): continue
        if n.get('forum',n.get('id'))!=n.get('id'):continue
        row=seed(c,year,content.get('title',''),'https://openreview.net/forum?id='+n['id'],date_kind='acceptance')
        date=datetime.fromtimestamp(n['pdate']/1000, timezone.utc).strftime('%Y-%m-%d') if n.get('pdate') else str(year)
        result.append(make_record(c,row,authors=content.get('authors',[]),abstract=content.get('abstract',''),date=date,
                                  pdf=urljoin('https://openreview.net',content.get('pdf') or '/pdf?id='+n['id'])))
    return result
