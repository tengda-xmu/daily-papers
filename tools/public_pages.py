"""Render public AI reading and application details using the existing site style."""
import json
from collections import Counter
from src.ai_updates import CATEGORIES, SCENARIOS
from src.opportunities import DETAILS, GROUPS, STAGES, SUBTYPES


def source_attributes(row):
    from tools.build_site import esc
    from src.social_content import provenance
    sources = row.get('source_links') or [provenance(row)]
    platforms = sorted({s.get('platform','official') for s in sources})
    authors = sorted({s['name'] for s in sources if s.get('platform') in ('wechat','xiaohongshu') and s.get('name')})
    return f'data-platforms="{esc(" ".join(platforms))}" data-authors="{esc(json.dumps(authors,ensure_ascii=False))}"'


def author_options(rows):
    from tools.build_site import esc
    from src.social_content import provenance
    authors = sorted({s['name'] for row in rows for s in (row.get('source_links') or [provenance(row)]) if s.get('platform') in ('wechat','xiaohongshu') and s.get('name')})
    return ''.join(f'<option value="{esc(v)}">{esc(v)}</option>' for v in authors)


def social_details(row):
    from tools.build_site import esc, safe_url, display_time
    labels = {'official':'官方依据', 'wechat':'公众号', 'xiaohongshu':'小红书'}
    sources = row.get('source_links', [])
    note = ''
    if row.get('platform') in ('wechat','xiaohongshu'):
        basis = {'article':'已读取文章内容，观点及效果来自作者', 'manual_text':'手动补充内容，尚未独立核实', 'search_snippet':'仅有索引摘要，内容待核对', 'excerpt':'仅有公开短摘要，内容待核对'}
        note = f'<p class="analysis-pending">{esc("原创／实践经验 · " if row.get("content_type") == "practice" else "平台报道 · ")}{esc(basis.get(row.get("evidence_kind"), "内容待核对"))}</p>'
        if row.get('read_status') == 'unavailable':
            note += '<p class="analysis-pending">本轮暂未读取到原文，保留已有资料。</p>'
        if row.get('kind') != 'ai':
            analysis = row.get('analysis') or {}
            if analysis:
                steps = ''.join(f'<li>{esc(s["text"])}</li>' for s in analysis.get('steps', []))
                note += f'<details class="public-details"><summary>中文导读</summary><p>{esc(analysis.get("summary"))}</p><p>{esc(analysis.get("application"))}</p><p>{esc(analysis.get("requirements"))}</p>{("<ol>" + steps + "</ol>") if steps else ""}</details>'
            else:
                note += '<p class="analysis-pending">中文导读待补充</p>'
    links = ''.join(f'<a href="{safe_url(s["url"])}" target="_blank" rel="noopener noreferrer">{esc(labels.get(s.get("platform"),"来源"))}：{esc(s.get("name"))}{(" · " + esc(display_time(s["published_at"])[0])) if s.get("published_at") else ""}</a>' for s in sources)
    links += ''.join(f'<a href="{safe_url(url)}" target="_blank" rel="noopener noreferrer">相关官方资料（对应关系待核对）</a>' for url in row.get('official_urls',[]))
    return note + (f'<details class="public-details"><summary>来源与核对</summary><div class="lead-footer">{links}</div></details>' if links else '')


def opportunity_details(row):
    from tools.build_site import esc, safe_url
    fields = ''.join(f'<dt>{esc(label)}</dt><dd>{esc(row.get(key) or "请查看官方通知")}</dd>'
                     for key, label in DETAILS.items() if key not in
                     (('term', 'duties') if row['kind'] == 'funding' else ('year', 'amount', 'duration', 'partner_requirements', 'deliverables')))
    links = []
    if row.get('application_url'):
        links.append(f'<a href="{safe_url(row["application_url"])}" target="_blank" rel="noopener noreferrer">前往申请</a>')
    if row.get('application_email'):
        links.append(f'<span>申请邮箱：{esc(row["application_email"])}</span>')
    for attachment in row.get('attachments', []):
        links.append(f'<a href="{safe_url(attachment["url"])}" target="_blank" rel="noopener noreferrer">{esc(attachment["label"])}</a>')
    changes = ''.join(f'<li>截止时间由 {esc(c["previous_deadline"])} 调整至 {esc(c["deadline"])}；'
                      f'<a href="{safe_url(c["source"])}" target="_blank" rel="noopener noreferrer">更正依据</a></li>' for c in row.get('corrections', []))
    return f'<details class="public-details"><summary>申请详情</summary><dl>{fields}</dl><p>{esc(row.get("deadline_note", ""))}</p><div class="lead-footer">{"".join(links)}</div>{"<ul>" + changes + "</ul>" if changes else ""}</details>'


def source_status(sources):
    from tools.build_site import esc, safe_url, display_time
    labels = {'ok': '已更新', 'no_data': '本轮没有新增可识别内容', 'error': '本轮获取失败，保留已有内容',
              'partial':'部分来源可用，保留已有内容', 'configuration_missing':'尚未配置搜索服务或订阅',
              'quota_exhausted':'查询额度已用完，保留已有内容', 'access_denied':'来源访问受限，保留已有内容'}
    rows = ''.join(f'<li><a href="{safe_url(s["url"])}" target="_blank" rel="noopener noreferrer">{esc(s["name"])}</a>：'
                   f'{labels.get(s["status"], "尚未采集")}；本轮 {int(s.get("count",0))} 条；最近成功 {esc(display_time(s.get("last_success"))[1])}'
                   f'{("；" + esc(s["message"])) if s.get("message") else ""}</li>' for s in sources)
    return f'<details class="leads-source-status"><summary>来源与更新说明</summary><p>每日北京时间 21:00 起更新。发布时间取自原始公告；采集失败时保留已有内容。</p><ul>{rows}</ul></details>'


def render_ai(data):
    from tools.build_site import document, esc, safe_url, template, display_time
    rows = []
    states = {'released': '已发布', 'preview': '预览／候补', 'research': '研究成果', 'unknown': '开放情况见原文'}
    for index, row in enumerate(data.get('entries', [])):
        social = row.get('platform') in ('wechat','xiaohongshu')
        analysis = row.get('analysis') or {}
        title = analysis.get('title_zh') or row.get('title_zh') or row['title']
        summary = analysis.get('summary') or row.get('summary', '')
        published = display_time(row.get('published_at'))[0] if row.get('published_at') else '发布日期待核对'
        tags = ''.join(f'<span>{esc(CATEGORIES[k])}</span>' for k in row.get('categories', []) if k in CATEGORIES)
        steps = ''.join(f'<li>{esc(s["text"])}</li>' for s in analysis.get('steps', []))
        details = ''
        if analysis:
            details = f'<details class="public-details"><summary>展开详情</summary><p>{esc(analysis.get("application"))}</p><p>{esc(analysis.get("requirements"))}</p>'
            if steps:
                details += f'<ol>{steps}</ol>'
            details += f'<a href="{safe_url(row.get("evidence_url") or row["url"])}" target="_blank" rel="noopener noreferrer">{"原文与使用经验" if social else "官方依据与使用说明"}</a></details>'
        else:
            details = '<p class="analysis-pending">中文导读待补充</p>'
        related = ''.join(f'<a href="{safe_url(link)}" target="_blank" rel="noopener noreferrer">相关官方发布</a>' for link in row.get('related_urls', []))
        rows.append(f'''<li class="lead-entry" id="{esc(row['id'])}" {source_attributes(row)} data-kind="ai" data-categories="{esc(' '.join(row.get('categories', [])))}"
data-topics="{esc(' '.join(row.get('scenarios', [])))}" data-provider="{esc(row.get('organization', row.get('source', '')))}"
data-published="{esc(row.get('published_at', ''))}" data-rank="{index}" data-state="{esc(row.get('release_state', 'unknown'))}">
<div class="lead-heading"><h2><a href="{safe_url(row['url'])}" target="_blank" rel="noopener noreferrer">{esc(title)}</a></h2><span class="lead-state">{states.get(row.get('release_state'), states['unknown'])}</span></div>
<p class="lead-meta">{esc(row.get('source'))} · {esc(published)}{(' · ' + esc(row['product_version'])) if row.get('product_version') else ''}</p>
<p class="lead-summary">{esc(summary)}</p>{social_details(row)}{details}<div class="lead-footer"><a href="{safe_url(row['url'])}" target="_blank" rel="noopener noreferrer">{"公开检索入口" if "weixin.sogou.com" in row['url'] else "查看平台原文" if social else "查看官方发布"}</a>{tags}{related}</div></li>''')
    counts = Counter(c for r in data.get('entries', []) for c in r.get('categories', []))
    categories = ''.join(f'<button type="button" data-lead-kind="{key}" aria-pressed="false">{label} <span>{counts[key]}</span></button>' for key, label in CATEGORIES.items())
    options = lambda pairs: ''.join(f'<option value="{esc(k)}">{esc(v)}</option>' for k, v in pairs)
    organizations = sorted({r.get('organization', r.get('source', '')) for r in data.get('entries', [])})
    return document(template('ai.html', count=len(rows), entries=''.join(rows), categories=categories,
        providers=options((v, v) for v in organizations), topics=options(SCENARIOS.items()), authors=author_options(data.get('entries',[])),
        generated=esc(display_time(data.get('checked_at'))[1]), source_status=source_status(data.get('sources', []))),
        title='AI 前沿 | 每日论文推荐', active='ai')
