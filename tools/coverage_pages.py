"""Paper-linked public-account introductions and authorized static reprints."""
from html import escape
from copy import deepcopy
import json
import re


STATE_LABELS = {
    'pending': '尚未检索，将随每日更新自动补充。',
    'matched': '已核对以下文章介绍的是这篇论文。',
    'no_match': '公开索引暂未返回结果，可能尚未收录；后续将再次检查。',
    'pending_match': '找到候选来源，待核实是否介绍这篇论文。',
    'unavailable': '来源暂时无法读取，待恢复后继续核对。',
    'configuration_missing': '自动搜索暂不可用，待配置恢复后继续检索。',
    'deferred': '公众号介绍待检索，将在更新时继续。',
    'error': '本次检索未完成，将在后续更新重试。',
}


def article_status(row):
    """Keep paper identity verification separate from access to article text."""
    parts = []
    method = row.get('verification_method')
    if method == 'manual_screenshot':
        parts.append('已依据用户提供的截图核实论文关联。')
    elif method == 'image':
        parts.append('已依据文章配图核实论文关联。')
    if row.get('body_status') == 'challenge':
        parts.append('已确认来源，正文读取受限：微信要求访问验证，请通过原文链接查看。')
    elif row.get('body_status') == 'unavailable':
        parts.append('已确认来源，正文读取受限；可通过原文链接查看。')
    elif row.get('body_status') == 'pending':
        parts.append('已确认来源，正文尚待读取。')
    return ''.join(parts)


def candidate_status(row):
    return {
        'visual_pending': '图片中的论文信息待核实',
        'article_unavailable': '正文暂时无法读取，论文关联待核实',
        'article_challenge': '访问验证受限，论文关联待核实',
        'challenge': '访问验证受限，论文关联待核实',
        'index_only': '仅有公开索引线索，论文关联待核实',
    }.get(row.get('reason'), '论文关联待核实')


def state_label(state, coverage):
    search_status = coverage.get('search_status')
    if state == 'unavailable' and search_status == 'blocked':
        return '公开索引访问受限，将在恢复后继续检索。'
    if state == 'configuration_missing' and search_status == 'partial':
        return '部分搜索渠道暂不可用，已保留其他渠道的检索结果。'
    if state == 'error' and search_status == 'partial':
        return '部分公开索引未能完成检索，将在后续更新重试。'
    if state == 'no_match':
        if search_status == 'not_searched':
            return STATE_LABELS['pending']
        if search_status == 'configuration_missing':
            return STATE_LABELS['configuration_missing']
        if search_status == 'legacy_unchecked':
            return '旧检索结果待复查，将使用更新后的检索方式继续核对。'
        if search_status == 'blocked':
            return '公开索引访问受限，尚不能判断是否有公众号介绍。'
        if search_status in ('partial', 'error'):
            return '公开索引检索尚未完成，后续将继续检查。'
    if state == 'deferred':
        reason = coverage.get('deferred_reason')
        if reason == 'budget_exhausted':
            return '这篇论文待重新检索，将在更新时继续。'
        if reason == 'runtime_limit':
            return '上次检索尚未完成，将在更新时继续。'
    return STATE_LABELS.get(state, STATE_LABELS['pending'])


def article_name(row):
    # URL-derived identities are independent of paper IDs, permitting one
    # authorized article to introduce several papers without duplicate pages.
    import hashlib
    return hashlib.sha256(row['url'].encode()).hexdigest()[:24]


def coverage_panel(paper_id, coverage=None, *, root='./', suffix=''):
    from tools.build_site import display_time, safe_url
    coverage = coverage or {}
    identifier = 'paper-coverage-' + re.sub(r'[^a-zA-Z0-9_-]', '', str(paper_id) + '-' + suffix)
    button = (f'<button type="button" class="text-button coverage-toggle" aria-expanded="false" '
              f'aria-controls="{identifier}">公众号介绍<span aria-hidden="true">＋</span></button>')
    state = coverage.get('state', 'pending')
    body = '<p class="coverage-status">' + state_label(state, coverage) + '</p>'
    waiting = state in ('pending', 'deferred', 'configuration_missing') or coverage.get('last_attempt_state') in ('deferred', 'configuration_missing')
    has_search_status = 'last_search_at' in coverage or 'search_status' in coverage
    checked_at = (coverage.get('last_search_at') if has_search_status
                  else coverage.get('last_attempt_at') or coverage.get('checked_at'))
    queued_at = coverage.get('queued_at')
    # Older records stamped the collection time even when no search ran.
    # Keep direct/legacy render callers honest as well as projected records.
    if waiting and not has_search_status and not coverage.get('last_attempt_at'):
        queued_at = queued_at or checked_at
        checked_at = None
    if checked_at:
        body += '<p class="coverage-checked">最近检索 ' + escape(display_time(checked_at)[1]) + '</p>'
    if waiting and queued_at:
        body += '<p class="coverage-checked">入队时间 ' + escape(display_time(queued_at)[1]) + '</p>'
    rows = []
    for row in coverage.get('articles', []):
        if row.get('match_status') != 'verified':
            continue
        url = safe_url(row.get('url'))
        if url == '#':
            continue
        date = str(row.get('published_at') or '')[:10]
        meta = escape(row.get('account') or '公众号名称待核实') + (' · ' + escape(date) if date else '')
        action = ''
        if row.get('display_mode') == 'reprint':
            action = f'<a href="{root}wechat/{article_name(row)}.html">站内阅读</a>'
        status = article_status(row)
        verified_at = row.get('verified_at')
        rows.append('<li><h4>' + escape(row.get('title') or '公众号文章') + '</h4>'
                    + '<p class="coverage-source">' + meta + '</p>'
                    + ('<p class="coverage-verification">' + escape(status) + '</p>' if status else '')
                    + ('<p class="coverage-checked">关联核验 ' + escape(display_time(verified_at)[1]) + '</p>' if verified_at else '')
                    + ('<p>' + escape(row['summary']) + '</p>' if row.get('summary') else '')
                    + f'<div class="coverage-actions">{action}<a href="{url}" target="_blank" rel="noopener noreferrer">公众号原文</a></div></li>')
    if rows:
        body += '<ul class="coverage-list">' + ''.join(rows) + '</ul>'
    candidates = []
    for row in coverage.get('candidates', []):
        url = safe_url(row.get('url'))
        if url != '#':
            candidates.append(f'<li><a href="{url}" target="_blank" rel="noopener noreferrer">'
                              + escape(row.get('title') or '候选公众号文章') + '</a>（'
                              + candidate_status(row) + '）</li>')
    if candidates:
        body += '<details class="coverage-candidates"><summary>待核实的候选来源</summary><ul>' + ''.join(candidates) + '</ul></details>'
    if rows and coverage.get('last_attempt_state') not in (None, '', 'matched', 'no_match'):
        body += '<p class="coverage-status">' + state_label(coverage['last_attempt_state'], coverage) + '已保留上次确认的介绍。</p>'
    panel = f'<section class="paper-coverage-panel" id="{identifier}" aria-label="公众号介绍" hidden>{body}</section>'
    return button, panel


def render_reprint(row):
    from tools.build_site import document, safe_url
    body = []
    for block in row.get('blocks', []):
        kind = block.get('type')
        if kind in ('heading', 'paragraph'):
            tag = 'h2' if kind == 'heading' else 'p'
            body.append(f'<{tag}>' + escape(block.get('text', '')) + f'</{tag}>')
        elif kind == 'image' and re.fullmatch(r'assets/wechat/[a-f0-9]{32,64}\.(?:png|jpe?g|webp|gif)', block.get('src', '')):
            alt = escape(block.get('alt') or '原文配图')
            body.append(f'<figure class="article-original-figure"><a class="article-zoom" href="../{block["src"]}" aria-label="放大{alt}">'
                        f'<img src="../{block["src"]}" alt="{alt}" loading="lazy"></a>'
                        + (f'<figcaption>{escape(block["alt"])}</figcaption>' if block.get('alt') else '') + '</figure>')
    content = ('<article class="reading-article wechat-reprint"><a class="article-back" href="../">返回每日推荐</a>'
               + '<header class="article-header"><h1>' + escape(row['title']) + '</h1>'
               + '<p class="article-byline">' + escape(row.get('account', '')) + '</p>'
               + ('<p class="article-byline">' + escape(row['author']) + '</p>' if row.get('author') else '')
               + '<p class="article-updated">' + escape(str(row.get('published_at') or '')[:10]) + ' · 授权转载</p>'
               + f'<p><a href="{safe_url(row["url"])}" target="_blank" rel="noopener noreferrer">阅读公众号原文</a></p>'
               + ('<p class="article-source-note">' + escape(row['reprint_notice']) + '</p>' if row.get('reprint_notice') else '') + '</header>'
               + ''.join(body) + '<footer class="article-end"><p>本文转载自 ' + escape(row.get('account') or '原公众号')
               + '，内容及观点属于原作者。论文精读另行依据论文原文核验。</p></footer></article>'
               + '<dialog class="article-figure-dialog"><form method="dialog"><button type="submit">关闭大图</button></form><img alt="原文配图放大查看"></dialog>')
    return document(content, title=row['title'] + ' | 公众号介绍', root='../', active='reading')


def build_coverage(root, out, coverage=None):
    from src.paper_coverage import load_public, publish_assets
    coverage = deepcopy(load_public(root) if coverage is None else coverage)
    available = publish_assets(root, out) or set()
    for record in coverage.values():
        for article in record.get('articles', []):
            if 'blocks' in article:
                if any(b.get('type') == 'image' and b.get('src') not in available for b in article['blocks']):
                    article['reprint_notice'] = '部分原文配图暂不可用，请通过公众号原文查看完整图文。'
                    article['reprint_complete'] = False
                article['blocks'] = [b for b in article['blocks'] if b.get('type') != 'image' or b.get('src') in available]
    directory = out / 'wechat'
    directory.mkdir(parents=True, exist_ok=True)
    articles = {article_name(a): a for record in coverage.values() for a in record.get('articles', [])
                if a.get('match_status') == 'verified' and a.get('display_mode') == 'reprint'}
    for path in directory.glob('*.html'):
        if re.fullmatch(r'[a-f0-9]{24}', path.stem) and path.stem not in articles and path.resolve().parent == directory.resolve():
            path.unlink()
    for name, article in articles.items():
        (directory / (name + '.html')).write_text(render_reprint(article), encoding='utf-8')
    (out / 'paper-coverage.json').write_text(json.dumps({'version': 2, 'papers': coverage}, ensure_ascii=False, indent=2), encoding='utf-8')
