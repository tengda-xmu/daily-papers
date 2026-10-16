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


def original_figure_groups(article, figures, section, root):
    """Resolve model-selected IDs only against independently validated originals."""
    from tools.build_site import safe_url
    available = {f['figure_id']: f for f in figures}
    output = []
    for group in article.get('figure_groups', []):
        if group.get('section') != section:
            continue
        panels = []
        for panel in group.get('panels', [])[:4]:
            original = available.get(panel.get('figure_id'))
            if not original:
                continue
            asset = original.get('asset') or original.get('image_path', '')
            if not re.fullmatch(r'assets/figures/[A-Za-z0-9_.-]+\.(?:png|jpe?g|webp)', asset):
                continue
            label = original.get('figure_label', '原文图')
            title = original.get('title_zh') or original.get('title') or label
            caption = panel.get('caption', '')
            credit = original.get('credit', '')
            source_url, license_url = safe_url(original.get('source_url')), safe_url(original.get('license_url'))
            attribution = (f'<a href="{source_url}" target="_blank" rel="noopener noreferrer">原文图页</a>'
                           + (f' · <a href="{license_url}" target="_blank" rel="noopener noreferrer">{escape(original.get("license", "许可"))}</a>' if license_url != '#' else ''))
            panels.append('<figure class="article-original-figure"><a class="article-zoom" '
                          f'href="{escape(root+asset)}" aria-label="放大查看{escape(label)}">'
                          f'<img src="{escape(root+asset)}" alt="{escape(title)}" loading="lazy" decoding="async"></a>'
                          + '<figcaption><strong>' + escape(label) + '</strong>'
                          + ('<p>图解：' + prose(caption) + '</p>' if caption else '')
                          + '<details><summary>' + ('图注译文与出处' if original.get('caption_basis') == 'curated_translation' else '原文图注与出处')
                          + '</summary><p>' + escape(original.get('caption', '')) + '</p>'
                          + '<p>' + escape(credit) + '</p>' + attribution + '</details></figcaption></figure>')
        if panels:
            output.append('<section class="article-figure-group"><h3>' + escape(group.get('title', '论文原图'))
                          + '</h3><div class="article-figure-grid">' + ''.join(panels) + '</div></section>')
    return ''.join(output)


def reading_context(paper):
    """Presentation scope never promotes snippets or an unreviewed draft."""
    from src.paper_sources import clean_abstract
    complete = clean_abstract(paper.get('abstract'))
    ready = paper.get('analysis_status') == 'ready'
    full = paper.get('analysis_basis') == 'full_text' and (ready or bool(paper.get('article'))
        or (paper.get('analysis_status') is None and bool(paper.get('deep_read'))))
    material_verified = bool(re.fullmatch(r'[a-f0-9]{64}', str(paper.get('analysis_material_version', ''))))
    abstract_ready = ready and (bool(complete) or material_verified)
    basis = 'full_text' if full else 'abstract' if abstract_ready else ''
    article = paper.get('article')
    if not isinstance(article, dict) or article.get('basis', 'full_text') != basis:
        article = None
    label = '全文精读' if full else '摘要解读' if abstract_ready else '论文资料'
    scope = ('依据论文全文整理' if full else '依据公开摘要整理，未核验全文细节' if abstract_ready
             else '已取得公开摘要，图文解读尚未完成' if complete
             else '当前只有检索片段，完整摘要与全文尚未取得' if paper.get('abstract')
             else '尚未取得可核实的完整摘要或全文')
    return {'basis': basis, 'article': article, 'label': label, 'scope': scope, 'abstract': complete}


def reading_fallback(paper, context, figures, root):
    """Keep existing notes readable while a separately reviewed article is queued."""
    from src.reading_notes import NOTE_FIELDS
    from src.paper_titles import chinese_abstract
    from tools.build_site import safe_url
    basis = context['basis']
    if basis:
        notice = ('全文精读已完成，文章版正在逐步升级。现有内容可继续阅读。' if basis == 'full_text' else
                  '摘要解读已完成，图文文章正在升级。以下保留现有摘要笔记，尚未按图文稿逐项复核。')
        body = '<p class="article-upgrade">' + notice + '</p>'
        if paper.get('summary'):
            body += '<div class="article-lead"><p>' + prose(paper['summary']) + '</p></div>'
        labels = ('研究问题', '方法与路线', '创新与比较', '证据与发现', '局限与边界', '方向关联', '后续研究建议')
        for key, label in zip(NOTE_FIELDS, labels):
            value = (paper.get('deep_read') or {}).get(key)
            if value:
                body += '<section><h2>' + label + '</h2><p>' + prose(value) + '</p></section>'
        if paper.get('recommendation'):
            body += '<section><h2>阅读建议</h2><p>' + prose(paper['recommendation']) + '</p></section>'
    else:
        body = '<p class="article-upgrade">' + context['scope'] + '。取得完整资料并通过核验后，这个地址会更新为图文解读。</p>'
        abstract = context['abstract'] or paper.get('abstract')
        if abstract:
            label = '公开摘要' if context['abstract'] else '检索片段（非完整摘要）'
            translation = chinese_abstract(paper)
            if translation:
                translated_label = '摘要译文' if context['abstract'] else '检索片段译文（非完整摘要）'
                body += '<section class="article-material"><h2>' + translated_label + '</h2><p>' + prose(translation) + '</p></section>'
                label = '英文原文' if context['abstract'] else '英文原文（检索片段，非完整摘要）'
            body += '<section class="article-material"><h2>' + label + '</h2><p>' + prose(abstract) + '</p></section>'
    if figures:
        # Unreviewed editorial captions never accompany the fallback gallery.
        groups = [{'section': 'method', 'title': '已取得的论文原图',
                   'panels': [{'figure_id': f['figure_id'], 'caption': ''} for f in figures[i:i+4]]}
                  for i in range(0, len(figures), 4)]
        body += '<p class="article-editor-note">原图未改动。以下图注来自标注的来源，不据此补写摘要未报告的实验结论。</p>'
        body += original_figure_groups({'figure_groups': groups}, figures, 'method', root)
    if basis and context['abstract']:
        body += '<details class="article-material"><summary>查看公开摘要</summary><p>' + prose(context['abstract']) + '</p></details>'
    links = [safe_url(url) for url in paper.get('analysis_sources', [])]
    links = [url for url in links if url != '#']
    if links:
        body += '<p class="article-evidence-links">' + ' · '.join(
            f'<a href="{url}" target="_blank" rel="noopener noreferrer">论文依据 {i+1}</a>' for i, url in enumerate(links)) + '</p>'
    return body


def render_article(paper, *, root='../', figures=(), coverage=None):
    from tools.build_site import document, display_time, safe_url
    from src.paper_titles import chinese_title
    context = reading_context(paper)
    article, basis = context['article'], context['basis']
    title = chinese_title(paper) or paper['title']
    original = f'<p class="article-original" lang="en">{escape(paper["title"])}</p>' if title != paper['title'] else ''
    url = safe_url(paper.get('landing_url') or 'https://doi.org/'+paper.get('doi',''))
    authors = ', '.join(paper.get('authors') or [])
    time_label = display_time(paper.get('article_updated_at') or paper.get('analyzed_at'))[1]
    from tools.coverage_pages import coverage_panel
    coverage_button, coverage_html = coverage_panel(paper['id'], coverage, root=root, suffix='article', paper=paper)
    source_links = ''
    oa_url = safe_url(paper.get('oa_url'))
    if oa_url != '#' and oa_url != url:
        source_links += f'<a href="{oa_url}" target="_blank" rel="noopener noreferrer">开放获取</a>'
    doi_url = safe_url('https://doi.org/'+paper['doi']) if paper.get('doi') else '#'
    if doi_url != '#' and doi_url not in (url, oa_url):
        source_links += f'<a href="{doi_url}" target="_blank" rel="noopener noreferrer">DOI</a>'
    updated = (('解读更新 ' if basis else '资料更新 ') + escape(time_label) + ' · '
               if paper.get('article_updated_at') or paper.get('analyzed_at') else '')
    header = (f'<article class="reading-article" data-paper-id="{escape(paper["id"])}" data-analysis-basis="{basis}" data-article-version="{article.get("version", "") if article else ""}"><a class="article-back" href="{root}">返回每日推荐</a>'
        f'<header class="article-header"><p class="article-type">{context["label"]}</p><h1>{escape(title)}</h1>{original}'
        f'<p class="article-byline">{escape(authors)}<br>{escape(paper.get("venue", ""))}</p>'
        f'<p class="article-updated">{updated}{context["scope"]}</p>'
        f'<div class="article-links"><a href="{url}" target="_blank" rel="noopener noreferrer">阅读原文</a>{source_links}'
        f'<button type="button" class="text-button copy-citation" data-citation="{escape(authors+". "+paper["title"]+". "+paper.get("venue","")+". "+url)}">复制引用</button>'
        f'{coverage_button}<button type="button" class="text-button codex-entry" data-local-only data-paper-id="{escape(paper["id"])}" data-paper-title="{escape(title)}">Codex 对话</button></div>{coverage_html}</header>')
    if not article:
        body = reading_fallback(paper, context, figures, root)
    else:
        def figure(kind):
            diagram = next((d for d in article.get('diagrams', []) if d['kind'] == kind), None)
            if diagram is None:
                return ''
            src = f'{root}assets/reading-diagrams/{paper["id"]}-{kind}'
            label = ('依据摘要整理的研究思路示意图（非论文原图）' if basis == 'abstract' else
                     '本站根据论文绘制' if kind == 'study' else '本站提出的研究建议')
            relations = '；'.join(next(n['label'] for n in diagram['nodes'] if n['id']==e['from']) + ' → '
                + next(n['label'] for n in diagram['nodes'] if n['id']==e['to']) + ('：'+e['label'] if e['label'] else '') for e in diagram['edges'])
            return (f'<figure class="article-diagram"><h3>{escape(diagram["title"])}</h3><a class="article-zoom" href="{src}.svg" aria-label="放大查看{escape(diagram["title"])}">'
                f'<picture><source media="(max-width:600px)" srcset="{src}-mobile.svg"><img src="{src}.svg" alt="{escape(diagram["description"])}" loading="lazy"></picture></a>'
                f'<figcaption><strong>{label}</strong><p>{prose(diagram["description"])}</p><a href="{src}.svg" download>下载 SVG</a>'
                f'<details><summary>图示关系说明</summary><p>实线表示相邻阶段的信息流，虚线表示跨阶段联系或反馈。</p><p>{escape(relations)}</p></details></figcaption></figure>')
        body = '<div class="article-lead"><p>'+prose(article['lead'])+'</p></div>'
        body += '<details class="article-toc"><summary>文章目录</summary><nav aria-label="文章目录">' + ''.join(
            f'<a href="#{s["id"]}">{escape(s["title"])}</a>' for s in article['sections'])
        if article.get('routes'):
            body += '<a href="#future">从这篇论文走向下一项研究</a>'
        if article.get('related'):
            body += '<a href="#related">相关研究与资料说明</a>'
        body += '</nav></details>'
        for section in article['sections']:
            body += f'<section id="{section["id"]}"><h2>{escape(section["title"])}</h2>' + ''.join('<p>'+prose(p)+'</p>' for p in section['paragraphs'])
            if basis == 'abstract' and section['id'] == 'inspiration':
                body += '<p class="article-editor-note">以上为本站阅读启发与建议，不是论文已验证的结论。</p>'
            body += original_figure_groups(article, figures, section['id'], root)
            if section['id'] == 'method':
                body += figure('study')
            if section['id'] == 'validation' and article.get('experiments'):
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
        if article.get('routes'):
            body += '<section class="article-future" id="future"><h2>从这篇论文走向下一项研究</h2><p class="article-editor-note">以下为结合研究方向提出的候选方案，实验设计与预期目标有待验证。</p>'
        for route in article.get('routes', []):
            body += '<section class="research-route"><h3>'+escape(route['title'])+'</h3>'
            for key,label in zip(ROUTE_FIELDS,ROUTE_LABELS):
                body += f'<p><strong>{label}。</strong>{prose(route[key])}</p>'
            body += '</section>'
        if article.get('routes'):
            body += figure('proposal')+'</section>'
        related = article.get('related')
        if related:
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
    end_note = ('本文依据论文资料整理，研究建议由本站提出。原文、相关文献和图示标注共同说明内容来源。' if basis else
                '本页保留当前可获得的论文资料；完整资料取得后，将在同一地址更新经过核验的解读。')
    content = header+body+'<footer class="article-end"><p>'+end_note+'</p></footer></article>'
    content += '<dialog class="article-figure-dialog"><form method="dialog"><button type="submit">关闭大图</button></form><img alt="论文图示放大查看"></dialog>'
    return document(content, title=title+' | '+context['label'], root=root, active='reading')


def build_articles(data, out, analyses):
    from src.editions import entries, enrich, read, relative_path, selected, manifest, edition_id
    index = entries(data)
    sources = [read(data/'daily.json')] + [read(data/relative_path(e)) for e in index]
    removed = manifest(data)['deleted']
    known_days = {e['date'] for e in index + removed}
    removed_ids = {e['id'] for e in removed}
    # Match build_archive's retained pre-edition date links. A date alias for
    # an indexed/deleted batch must not restore old membership or removed data.
    for source in (data/'archive').glob('*.json'):
        payload = read(source)
        if source.stem not in known_days and edition_id(payload) not in removed_ids:
            sources.append(payload)
    papers = {}
    def priority(paper):
        ready = paper.get('analysis_status') == 'ready'
        return (ready and paper.get('analysis_basis') == 'full_text', bool(paper.get('article')),
                ready, paper.get('article_updated_at') or paper.get('analyzed_at') or '')
    for payload in sources:
        # A stale abstract overlay must not replace a published full-text page.
        for paper in selected(payload) + selected(enrich(payload, analyses)):
            identifier = paper.get('id', '')
            if not re.fullmatch(r'[a-f0-9]{12}', str(identifier)):
                continue
            if identifier not in papers or priority(paper) >= priority(papers[identifier]):
                papers[identifier] = paper
    directory = out/'readings'; directory.mkdir(parents=True, exist_ok=True)
    images = out/'assets/reading-diagrams'; images.mkdir(parents=True, exist_ok=True)
    from src.article_figures import eligible_article_figures, copy_article_figures, public_article_figure_manifest
    from src.paper_coverage import load_public
    coverage = load_public(data.parent)
    from src.editions import write
    write(out/'article-figures.json', public_article_figure_manifest(data.parent, list(papers.values())))
    # Only remove files owned by this generator, after resolving within its directory.
    expected = set(papers)
    for path in directory.glob('*.html'):
        if path.stem not in expected and re.fullmatch('[a-f0-9]{12}', path.stem) and path.resolve().parent == directory.resolve():
            path.unlink()
    for path in images.glob('*.svg'):
        if re.fullmatch('[a-f0-9]{12}-(?:study|proposal)(?:-mobile)?', path.stem) and path.resolve().parent == images.resolve():
            path.unlink()
    for identifier, paper in papers.items():
        figures = eligible_article_figures(data.parent, paper)
        copy_article_figures(data.parent, paper, out)
        (directory/(identifier+'.html')).write_text(render_article(paper, figures=figures, coverage=coverage.get(identifier)), encoding='utf-8')
        for diagram in (reading_context(paper)['article'] or {}).get('diagrams', []):
            for mobile in (False, True):
                name = identifier+'-'+diagram['kind']+('-mobile' if mobile else '')+'.svg'
                (images/name).write_text(diagram_svg(diagram, mobile=mobile), encoding='utf-8')
