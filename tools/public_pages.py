"""Render public AI reading and application details using the existing site style."""
import json
from collections import Counter
from src.ai_updates import CATEGORIES, SCENARIOS
from src.opportunities import DETAILS, GROUPS, STAGES, SUBTYPES


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
    labels = {'ok': '已更新', 'no_data': '本轮没有新增可识别内容', 'error': '本轮获取失败，保留已有内容'}
    rows = ''.join(f'<li><a href="{safe_url(s["url"])}" target="_blank" rel="noopener noreferrer">{esc(s["name"])}</a>：'
                   f'{labels.get(s["status"], "尚未采集")}；最近成功 {esc(display_time(s.get("last_success"))[1])}</li>' for s in sources)
    return f'<details class="leads-source-status"><summary>来源与更新说明</summary><p>每日北京时间 21:00 起更新。发布时间取自原始公告；采集失败时保留已有内容。</p><ul>{rows}</ul></details>'


def render_ai(data):
    from tools.build_site import document, esc, safe_url, template, display_time
    rows = []
    states = {'released': '已发布', 'preview': '预览／候补', 'research': '研究成果', 'unknown': '开放情况见原文'}
    for index, row in enumerate(data.get('entries', [])):
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
            details += f'<a href="{safe_url(row.get("evidence_url") or row["url"])}" target="_blank" rel="noopener noreferrer">官方依据与使用说明</a></details>'
        else:
            details = '<p class="analysis-pending">中文导读待补充</p>'
        related = ''.join(f'<a href="{safe_url(link)}" target="_blank" rel="noopener noreferrer">相关官方发布</a>' for link in row.get('related_urls', []))
        rows.append(f'''<li class="lead-entry" id="{esc(row['id'])}" data-kind="ai" data-categories="{esc(' '.join(row.get('categories', [])))}"
data-topics="{esc(' '.join(row.get('scenarios', [])))}" data-provider="{esc(row.get('organization', row.get('source', '')))}"
data-published="{esc(row.get('published_at', ''))}" data-rank="{index}" data-state="{esc(row.get('release_state', 'unknown'))}">
<div class="lead-heading"><h2><a href="{safe_url(row['url'])}" target="_blank" rel="noopener noreferrer">{esc(title)}</a></h2><span class="lead-state">{states.get(row.get('release_state'), states['unknown'])}</span></div>
<p class="lead-meta">{esc(row.get('source'))} · {esc(published)}{(' · ' + esc(row['product_version'])) if row.get('product_version') else ''}</p>
<p class="lead-summary">{esc(summary)}</p>{details}<div class="lead-footer"><a href="{safe_url(row['url'])}" target="_blank" rel="noopener noreferrer">查看官方发布</a>{tags}{related}</div></li>''')
    counts = Counter(c for r in data.get('entries', []) for c in r.get('categories', []))
    categories = ''.join(f'<button type="button" data-lead-kind="{key}" aria-pressed="false">{label} <span>{counts[key]}</span></button>' for key, label in CATEGORIES.items())
    options = lambda pairs: ''.join(f'<option value="{esc(k)}">{esc(v)}</option>' for k, v in pairs)
    organizations = sorted({r.get('organization', r.get('source', '')) for r in data.get('entries', [])})
    return document(template('ai.html', count=len(rows), entries=''.join(rows), categories=categories,
        providers=options((v, v) for v in organizations), topics=options(SCENARIOS.items()),
        generated=esc(display_time(data.get('checked_at'))[1]), source_status=source_status(data.get('sources', []))),
        title='AI 前沿 | 每日论文推荐', active='ai')
