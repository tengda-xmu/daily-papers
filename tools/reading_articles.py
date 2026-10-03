"""Static editorial pages and deterministic SVG research diagrams."""
from collections import defaultdict
from html import escape
import re
import unicodedata

from src.reading_article import EXPERIMENT_FIELDS, ROUTE_FIELDS, without_page_citations

EXPERIMENT_LABELS = ('验证任务', '数据来源', '样本与试件', '分组与工况', '数据划分', '对照与消融', '评价指标', '关键结果', '验证边界')
ROUTE_LABELS = ('问题与短板', '改进机制', '候选创新', '数据与实验设计', '对照与消融', '成功判据', '风险与替代方案')


def prose(value):
    clean = without_page_citations(value)
    clean = re.sub(r'(?<![A-Za-z0-9])[PS]\d+(?:\s*[-–—、,，]\s*[PS]?\d+)*(?:页|章节)', '', clean)
    clean = re.sub(r'【(?:论文原文[^】]*|资料边界|解读|建议)】', '', clean)
    escaped = escape(clean)
    return re.sub(r'\*\*([^*\n]{1,120})\*\*', r'<strong>\1</strong>', escaped)


def source_note(value):
    # Defect records sometimes repeat the page in their prose as well as the
    # separate private `page` field. Preserve formula/figure numbers and impact.
    value = re.sub(r'(?<![A-Za-z0-9])[PS]\d+\s*(?:页|章节)?(?:的)?(?=[\u4e00-\u9fff（(])', '', str(value))
    return prose(value)


def wrap(value, width):
    lines, line, size = [], '', 0
    for ch in value:
        weight = 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
        if size + weight > width and line:
            lines.append(line); line, size = '', 0
        line += ch; size += weight
    if line:
        lines.append(line)
    return lines


def diagram_svg(diagram, *, mobile=False):
    """SVG comes from a fixed renderer; model strings are always escaped."""
    groups = defaultdict(list)
    for node in diagram['nodes']:
        groups[node['stage']].append(node)
    stages = sorted(groups)
    width = 380 if mobile else 880
    max_columns = 1 if mobile else min(3, max(len(groups[s]) for s in stages))
    node_width = 294 if mobile else min(410, (width-150-30*(max_columns-1))/max_columns)
    title_wrap = int((node_width-40)/9.2)
    detail_wrap = int((node_width-40)/7.2)
    node_height = max(128, max(45 + 24*len(wrap(n['label'], title_wrap))
                              + 21*len(wrap(n['detail'], detail_wrap)) for n in diagram['nodes']))
    nodes, y = {}, 62
    for s in stages:
        for start in range(0, len(groups[s]), max_columns):
            group = groups[s][start:start+max_columns]
            left = (width-(len(group)*node_width+(len(group)-1)*30))/2
            for col, node in enumerate(group):
                nodes[node['id']] = (left+col*(node_width+30), y, node_width, node_height, node)
            y += node_height+62
    height = y + 12
    accent = '#153e64' if diagram['kind'] == 'study' else '#0f766e'
    output = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        f'<title id="title">{escape(diagram["title"])}</title><desc id="desc">{escape(diagram["description"])}</desc>',
        '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8" fill="none" stroke="#6d879b" stroke-width="1.4"/></marker></defs>',
        f'<rect width="{width}" height="{height}" fill="#f7fafc" rx="12"/>',
        f'<text x="32" y="32" fill="{accent}" font-family="Microsoft YaHei, sans-serif" font-size="14">'
        + ('研究逻辑' if diagram['kind']=='study' else '拟开展的验证路线') + '</text>']
    # Draw relations below node cards; all actual edges are retained in mobile layout.
    for index, edge in enumerate(diagram['edges']):
        ax, ay, aw, ah, _ = nodes[edge['from']]; bx, by, bw, bh, _ = nodes[edge['to']]
        adjacent = by>ay and by-ay <= node_height+63
        if abs(ay-by) < 10 and bx > ax:
            x1, y1, x2, y2 = ax+aw, ay+ah/2, bx, by+bh/2
            path = f'M{x1},{y1} H{x2}'
            tx, ty = (x1+x2)/2, y1-9
        elif adjacent:
            x1, y1, x2, y2 = ax+aw/2, ay+ah, bx+bw/2, by
            middle = (y1+y2)/2
            path = f'M{x1},{y1} V{middle} H{x2} V{y2}'
            tx, ty = x2, middle+18
        else:
            # Skip-stage and feedback relations use the outer gutters, never
            # crossing an intervening card. The textual legend names each link.
            left_side = by>ay
            rail = 10+(index%3)*11 if left_side else width-10-(index%3)*11
            x1,x2 = (ax,bx) if left_side else (ax+aw,bx+bw)
            path = f'M{x1},{ay+ah/2} H{rail} V{by+bh/2} H{x2}'
        dash = '' if adjacent or abs(ay-by)<10 else ' stroke-dasharray="5 4"'
        output.append(f'<path d="{path}" fill="none" stroke="#6d879b" stroke-width="1.6"{dash} marker-end="url(#arrow)"/>')
        if edge['label'] and adjacent:
            output.append(f'<text x="{tx}" y="{ty}" text-anchor="middle" font-family="Microsoft YaHei,sans-serif" font-size="12" fill="#526b7e" stroke="#f7fafc" stroke-width="4" paint-order="stroke">{escape(edge["label"])}</text>')
    for x, yy, w, h, node in nodes.values():
        output.append(f'<rect x="{x}" y="{yy}" width="{w}" height="{h}" rx="7" fill="#fff" stroke="#d5e2e9"/>')
        output.append(f'<rect x="{x}" y="{yy}" width="4" height="{h}" rx="2" fill="{accent}"/>')
        title_lines = wrap(node['label'], title_wrap)
        detail_lines = wrap(node['detail'], detail_wrap)
        for i, line in enumerate(title_lines):
            output.append(f'<text x="{x+17}" y="{yy+29+i*24}" font-family="Microsoft YaHei,sans-serif" font-size="18" font-weight="700" fill="{accent}">{escape(line)}</text>')
        offset = 36 + len(title_lines)*24
        for i, line in enumerate(detail_lines):
            output.append(f'<text x="{x+17}" y="{yy+offset+i*21}" font-family="Microsoft YaHei,sans-serif" font-size="14" fill="#3b5264">{escape(line)}</text>')
    return ''.join(output) + '</svg>'


def render_article(paper, *, root='../'):
    from tools.build_site import document, display_time, safe_url
    from src.paper_titles import chinese_title
    article = paper.get('article')
    title = chinese_title(paper) or paper['title']
    original = f'<p class="article-original" lang="en">{escape(paper["title"])}</p>' if title != paper['title'] else ''
    url = safe_url(paper.get('landing_url') or 'https://doi.org/'+paper.get('doi',''))
    authors = ', '.join(paper.get('authors') or [])
    time_label = display_time(paper.get('article_updated_at') or paper.get('analyzed_at'))[1]
    header = (f'<article class="reading-article" data-paper-id="{escape(paper["id"])}"><a class="article-back" href="{root}">返回每日推荐</a>'
        f'<header class="article-header"><p class="article-type">论文精读</p><h1>{escape(title)}</h1>{original}'
        f'<p class="article-byline">{escape(authors)}<br>{escape(paper.get("venue", ""))}</p>'
        f'<p class="article-updated">精读更新 {escape(time_label)} · 依据论文全文整理</p>'
        f'<div class="article-links"><a href="{url}" target="_blank" rel="noopener noreferrer">阅读原文</a>'
        f'<button type="button" class="text-button copy-citation" data-citation="{escape(authors+". "+paper["title"]+". "+paper.get("venue","")+". "+url)}">复制引用</button>'
        f'<button type="button" class="text-button codex-entry" data-local-only data-paper-id="{escape(paper["id"])}" data-paper-title="{escape(title)}">Codex 对话</button></div></header>')
    if not article:
        labels = ('研究问题', '方法与路线', '创新与比较', '证据与发现', '局限与边界', '方向关联', '后续研究建议')
        from src.reading_notes import NOTE_FIELDS
        body = '<p class="article-upgrade">全文精读已完成，文章版正在逐步升级。现有内容可继续阅读。</p>'
        body += f'<div class="article-lead"><p>{prose(paper.get("summary", ""))}</p></div>'
        body += ''.join(f'<section><h2>{label}</h2><p>{prose(paper.get("deep_read", {}).get(key, ""))}</p></section>' for key, label in zip(NOTE_FIELDS, labels))
    else:
        def figure(kind):
            diagram = next(d for d in article['diagrams'] if d['kind'] == kind)
            src = f'{root}assets/reading-diagrams/{paper["id"]}-{kind}'
            label = '本站根据论文绘制' if kind == 'study' else '本站提出的研究建议'
            relations = '；'.join(next(n['label'] for n in diagram['nodes'] if n['id']==e['from']) + ' → '
                + next(n['label'] for n in diagram['nodes'] if n['id']==e['to']) + ('：'+e['label'] if e['label'] else '') for e in diagram['edges'])
            return (f'<figure class="article-diagram"><h3>{escape(diagram["title"])}</h3><a class="article-zoom" href="{src}.svg" aria-label="放大查看{escape(diagram["title"])}">'
                f'<picture><source media="(max-width:600px)" srcset="{src}-mobile.svg"><img src="{src}.svg" alt="{escape(diagram["description"])}" loading="lazy"></picture></a>'
                f'<figcaption><strong>{label}</strong><p>{prose(diagram["description"])}</p><a href="{src}.svg" download>下载 SVG</a>'
                f'<details><summary>图示关系说明</summary><p>实线表示相邻阶段的信息流，虚线表示跨阶段联系或反馈。</p><p>{escape(relations)}</p></details></figcaption></figure>')
        body = '<div class="article-lead"><p>'+prose(article['lead'])+'</p></div>'
        body += '<details class="article-toc"><summary>文章目录</summary><nav aria-label="文章目录">' + ''.join(
            f'<a href="#{s["id"]}">{escape(s["title"])}</a>' for s in article['sections']) + '<a href="#future">从这篇论文走向下一项研究</a><a href="#related">相关研究与资料说明</a></nav></details>'
        for section in article['sections']:
            body += f'<section id="{section["id"]}"><h2>{escape(section["title"])}</h2>' + ''.join('<p>'+prose(p)+'</p>' for p in section['paragraphs'])
            if section['id'] == 'method':
                body += figure('study')
            if section['id'] == 'validation':
                body += '<div class="experiment-evidence"><table><caption>实验与验证一览</caption><thead><tr><th scope="col">验证任务</th><th scope="col">样本与规模</th><th scope="col">主要结果</th></tr></thead><tbody>'
                for row in article['experiments']:
                    body += '<tr><th scope="row">'+prose(row['name'])+'</th>'
                    body += '<td data-label="样本与规模">'+prose(row['samples'])+'</td><td data-label="主要结果">'+prose(row['results'])+'</td>'
                    body += '</tr>'
                body += '</tbody></table><details class="experiment-details"><summary>展开数据来源、分组、对照与验证边界</summary>'
                for row in article['experiments']:
                    body += '<section><h4>'+prose(row['name'])+'</h4>' + ''.join('<p><strong>'+EXPERIMENT_LABELS[EXPERIMENT_FIELDS.index(key)]+'。</strong>'+prose(row[key])+'</p>' for key in ('data','groups','split','baselines','metrics','limitations'))+'</section>'
                body += '</details></div>'
            body += '</section>'
        body += '<section class="article-future" id="future"><h2>从这篇论文走向下一项研究</h2><p class="article-editor-note">以下为结合研究方向提出的候选方案，实验设计与预期目标有待验证。</p>'
        for route in article['routes']:
            body += '<section class="research-route"><h3>'+escape(route['title'])+'</h3>'
            for key,label in zip(ROUTE_FIELDS,ROUTE_LABELS):
                body += f'<p><strong>{label}。</strong>{prose(route[key])}</p>'
            body += '</section>'
        body += figure('proposal')+'</section>'
        related = article['related']
        body += '<section id="related" class="article-related"><h2>相关研究与资料说明</h2>'
        body += '<p>核对截至 '+escape(related['checked_at'][:10])+'。比较范围限于实际取得的资料，候选创新仍需进一步检验。</p>'
        if related['status'] != 'ok':
            body += '<p class="article-upgrade">部分公开检索暂不可用，同类研究核对待补充，将自动重试。</p>'
        body += '<ul>'+''.join(f'<li><a href="{escape(r["url"])}" target="_blank" rel="noopener noreferrer">{escape(r["title"])}</a>'
            f'<span>{r["year"]} · {"全文" if r["basis"]=="full_text" else "摘要"}依据{" · 预印本" if r["preprint"] else ""}</span></li>' for r in related['papers'])+'</ul>'
        if not related['papers'] and related['status'] == 'ok':
            body += '<p>本次未取得足以支持直接比较的相关文献；这不表示没有既有研究。</p>'
        body += '</section>'
    issues = [i for i in paper.get('analysis_issues', []) if i['kind']=='source_defect']
    if issues:
        body += '<aside class="article-source-note"><h2>原文资料边界</h2><ul>'+''.join('<li>'+source_note(i['detail'])+'</li>' for i in issues)+'</ul></aside>'
    content = header+body+'<footer class="article-end"><p>本文依据论文资料整理，研究建议由本站提出。原文、相关文献和图示标注共同说明内容来源。</p></footer></article>'
    content += '<dialog class="article-figure-dialog"><form method="dialog"><button type="submit">关闭大图</button></form><img alt="研究思路图放大查看"></dialog>'
    return document(content, title=title+' | 全文精读', root=root, active='reading')


def build_articles(data, out, analyses):
    from src.editions import entries, enrich, read, relative_path, selected
    sources = [read(data/'daily.json')] + [read(data/relative_path(e)) for e in entries(data)]
    papers = {}
    for payload in sources:
        for paper in selected(enrich(payload, analyses)):
            if paper.get('analysis_basis') == 'full_text' and paper.get('analysis_status') == 'ready':
                papers.setdefault(paper['id'], paper)
    directory = out/'readings'; directory.mkdir(parents=True, exist_ok=True)
    images = out/'assets/reading-diagrams'; images.mkdir(parents=True, exist_ok=True)
    # Only remove files owned by this generator, after resolving within its directory.
    expected = set(papers)
    for path in directory.glob('*.html'):
        if path.stem not in expected and re.fullmatch('[a-f0-9]{12}', path.stem) and path.resolve().parent == directory.resolve():
            path.unlink()
    for path in images.glob('*.svg'):
        if re.fullmatch('[a-f0-9]{12}-(?:study|proposal)(?:-mobile)?', path.stem) and path.resolve().parent == images.resolve():
            path.unlink()
    for identifier, paper in papers.items():
        (directory/(identifier+'.html')).write_text(render_article(paper), encoding='utf-8')
        for diagram in (paper.get('article') or {}).get('diagrams', []):
            for mobile in (False, True):
                name = identifier+'-'+diagram['kind']+('-mobile' if mobile else '')+'.svg'
                (images/name).write_text(diagram_svg(diagram, mobile=mobile), encoding='utf-8')
