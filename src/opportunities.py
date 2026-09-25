"""Public academic appointments and research funding, including industrial calls."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from src.models import parse_date
from src.public_sources import ROOT, clean, date, fetch, identifier, read, refresh_sources, write

KINDS = {'academic_role': '学术任职招募', 'funding': '项目与基金申报'}
SUBTYPES = {'editor': '期刊编委', 'young_editor': '青年编委', 'committee': '学会与专业委员',
            'grant': '科研基金', 'government': '政府科技项目', 'open_topic': '开放课题',
            'international': '国际合作', 'joint_fund': '联合基金', 'industry': '企业委托科研',
            'collaboration': '产学研合作', 'challenge': '揭榜与技术攻关'}
GROUPS = {'comac': '中国商飞', 'avic': '中国航空工业', 'aecc': '中国航发', 'other': '其他企业'}
STAGES = {'application': '正式申报', 'guide': '指南建议征集', 'preview': '项目预告',
          'standing': '常年受理', 'results': '结果公示', 'unknown': '阶段待核对'}
DETAILS = {'eligibility': '申请条件', 'applicant': '申请主体', 'materials': '材料清单', 'method': '申请方式',
           'term': '任期', 'duties': '职责', 'year': '项目年度', 'amount': '资助额度', 'duration': '执行周期',
           'partner_requirements': '联合申报要求', 'deliverables': '研究任务与交付要求'}


def classify(text):
    text = clean(text)
    if re.search(r'获批|获奖名单|资助结果|拟资助|拟立项|评审结果|征订|供应商注册|物资采购|工程施工|会员注册|membership registration', text, re.I):
        return None
    if re.search(r'编委|editorial board|associate editor|委员|committee', text, re.I) and re.search(
            r'招募|征集|申请|招聘|增补|遴选|自荐|招贤|诚邀|call|join|apply|opportunit|seeking|recruit', text, re.I):
        return 'academic_role'
    if re.search(r'基金|课题|科研项目|科技项目|科研合作|产学研|揭榜|技术攻关|grant|funding|fellowship|research call|research collaboration', text, re.I) and re.search(
            r'申报|指南|申请|征集|开放|揭榜|招标|call|apply|opportunit|funding|open', text, re.I):
        return 'funding'
    return None


def enrich(row):
    title, text = clean(row.get('title')), clean(row.get('evidence_text', ''))
    date_text = re.sub(r'(?<=\d)\s+(?=[\d年月日])|(?<=[年月])\s+(?=\d)', '', clean(row.get('metadata_text')) or text)
    if re.search(r'获批|获奖名单|资助结果|拟资助|拟立项|评审结果|征订|供应商注册|物资采购|工程施工|会员注册|Funding finder', title, re.I):
        return None
    kind = row.get('kind') if row.get('kind') in KINDS else classify(title)
    if not kind:
        return None
    if kind == 'funding' and not row.get('organization_group') and not re.search(
            r'航空|航天|机械|力学|材料|智能|计算|制造|结构|可靠|物理|信息|能源|engineer|artificial|\bAI\b|comput|material|manufactur|EPSRC|innovate|innovation|physical|mechanic|energy|robot',
            title + ' ' + text + ' ' + row.get('summary', ''), re.I):
        return None
    row = {**row, 'kind': kind, 'id': identifier(row['url'])}
    subtypes = [('young_editor', r'青年编委|young.*editor'), ('editor', r'编委|editor'),
                ('committee', r'委员|committee'), ('joint_fund', r'联合基金|joint fund'),
                ('collaboration', r'产学研|校企|collaboration'), ('challenge', r'揭榜|攻关|challenge'),
                ('open_topic', r'开放课题|开放基金|open research'), ('international', r'国际合作|international cooperation'),
                ('government', r'政府|国家重点研发|科技项目')]
    row.setdefault('subtype', next((key for key, rx in subtypes if re.search(rx, title, re.I)),
                                  'industry' if row.get('organization_group') else 'grant'))
    if row.get('organization_group'):
        row['organization_type'] = 'enterprise'
    # Do not infer a parent group merely because its name appears in eligibility text.
    row.setdefault('stage', 'guide' if re.search(r'指南.*(?:建议|征集)|征集.*指南', title) else
                   'preview' if re.search(r'预告|预通知|forthcoming', title, re.I) else 'application')
    if not row.get('deadline') and text:
        patterns = [r'(?:截止受理日期|截止日期|截止时间|申请截止|申报截止|deadline)(?:为|是)?\s*[:：]?\s*(20\d{2}[年./-]\d{1,2}[月./-]\d{1,2})',
                    r'(20\d{2}[年./-]\d{1,2}[月./-]\d{1,2})日?前.{0,14}(?:提交|发送|申报|报送)',
                    r'(?:Closing date|Application deadline)\s*[:：]?\s*(\d{1,2} [A-Za-z]+ 20\d{2})']
        values = {date(m)[:10] for rx in patterns for m in re.findall(rx, date_text, re.I)} - {''}
        if len(values) == 1:
            row['deadline'] = values.pop()
            # A date alone is not sufficient to invent a midnight time or timezone.
            row.setdefault('deadline_zone', '时区及具体时刻见官方通知')
        elif len(values) > 1:
            row['deadline_note'] = '原文包含多个截止日期，请展开官方通知核对对应阶段。'
    row.setdefault('region', '未注明')
    row.setdefault('year', next(iter(re.findall(r'20\d{2}', title)), ''))
    row['summary'] = clean(row.get('summary', ''))[:350]
    # Only short excerpts are published; full fetched documents are not redistributed.
    row.pop('evidence_text', None)
    row.pop('metadata_text', None)
    return row


def state(row, now):
    today = now.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    deadline = parse_date(row.get('deadline_at'))
    if (deadline and now > deadline) or (not deadline and row.get('deadline', '') and row['deadline'] < today):
        return 'ended', '已截止'
    verified = parse_date(row.get('verified_at'))
    if row.get('verification') != 'verified' or not verified or now - verified > timedelta(days=30):
        return 'unknown', '待核对'
    if row.get('stage') == 'results':
        return 'ended', '结果已公布'
    if row.get('stage') == 'preview' or row.get('opens', '') > today:
        return 'planned', '尚未开放'
    if row.get('stage') == 'standing':
        return 'standing', '常年受理'
    if row.get('deadline'):
        end = parse_date(row['deadline'])
        if (end.date() - datetime.fromisoformat(today).date()).days <= 7:
            return 'closing', '即将截止'
        return 'open', '正在申请' if row.get('stage') != 'guide' else '正在征集指南建议'
    return 'unknown', '截止时间待核对'


def retain(row, now):
    deadline = parse_date(row.get('deadline_at') or row.get('deadline'))
    if deadline:
        return now - deadline <= timedelta(days=90)
    if row.get('stage') in ('standing', 'preview'):
        return True
    if not row.get('published_at') and row.get('year') and str(row['year']).isdigit() and int(row['year']) < now.year:
        return False
    published = parse_date(row.get('published_at'))
    return not published or timedelta(0) <= now - published <= timedelta(days=90)


def refresh(root=ROOT, *, now=None, fetcher=fetch):
    now = now or datetime.now(timezone.utc)
    config = read(root / 'config/opportunity-sources.json')
    previous = read(root / 'data/opportunities.json')
    fresh, statuses = refresh_sources(config, previous, now, fetcher)
    # Keep checking still-valid calls even when their announcement leaves a list page.
    known = {r['url'] for r in fresh}
    by_host = {host: s for s in config.get('sources', []) for host in s.get('hosts', [])}
    from urllib.parse import urlsplit
    follow = []
    for old in previous.get('entries', []):
        source = by_host.get(urlsplit(old.get('url', '')).hostname)
        checked = parse_date(old.get('checked_at'))
        if old.get('url') in known or not source or not retain(old, now) or (checked and now-checked < timedelta(hours=20)):
            continue
        follow.append({'id':'follow-'+old['id'], 'name':old.get('organization', old.get('source', source['name'])),
                       'url':old['url'], 'hosts':source['hosts'], 'format':'document',
                       'defaults':{k:old[k] for k in ('kind','subtype','region','organization_group','topics') if k in old}})
    if follow:
        extra, follow_status = refresh_sources({'sources':follow}, previous, now, fetcher)
        fresh.extend(extra)
        statuses.extend(follow_status)
    rows = {r['id']: r for r in previous.get('entries', [])}
    verified = lambda r: parse_date(r.get('verified_at')) or datetime.min.replace(tzinfo=timezone.utc)
    seeds = [r for r in config.get('entries', []) if identifier(r['url']) not in rows or
             verified(r) > verified(rows[identifier(r['url'])])]
    for raw in seeds + fresh:
        row = enrich(raw)
        if row:
            old = rows.get(row['id'], {})
            if old.get('verification') == 'verified' and row.get('verification') != 'verified':
                continue
            changes = old.get('corrections', [])[:]
            if old.get('deadline') and row.get('deadline') and old['deadline'] != row['deadline']:
                changes.append({'previous_deadline': old['deadline'], 'deadline': row['deadline'],
                                'source': row['url'], 'checked_at': now.isoformat()})
            row['corrections'] = changes
            rows[row['id']] = row
    result = {'version': 1, 'entries': list(rows.values()), 'sources': statuses, 'checked_at': now.isoformat(),
              'outcome': 'partial' if any(s['status'] == 'error' for s in statuses) else 'ok'}
    write(root / 'data/opportunities.json', result)
    return result


def collect(root=ROOT, now=None):
    now = now or datetime.now(timezone.utc)
    value = read(root / 'data/opportunities.json', {'entries': [], 'sources': []})
    # Exact, call-specific application URLs identify reposts; generic portal roots do not.
    from urllib.parse import urlsplit
    merged = {}
    for row in value['entries']:
        if not retain(row, now):
            continue
        target = row.get('application_url', '')
        path = urlsplit(target).path
        key = (target, row.get('stage'), row.get('deadline')) if re.search(r'/(?:competition|call|topic)/[^/]+', path) else row['id']
        old = merged.get(key)
        if not old or sum(bool(row.get(k)) for k in DETAILS) > sum(bool(old.get(k)) for k in DETAILS):
            merged[key] = row
    value['entries'] = list(merged.values())
    return value
