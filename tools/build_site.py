"""Build the static daily digest and readable archives for GitHub Pages."""
from __future__ import annotations

import hashlib
import html
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.catalog import (JOURNALS, SOURCE_CATALOG, STATE_LABELS, TOPIC_CATALOG,
                         VENUE_GROUPS, paper_facets, source_state, string_list)
from src.research_focus import focus_tags
from src.figures import get_figure
from src.wechat_metadata import public_subscriptions
from src.wechat_subscriptions import effective_accounts, group_overview, published_accounts
from src.research_directions import load_profile, active_directions, profile_revision

DATA = ROOT / "data"
OUT = ROOT / "site"
TEMPLATES = ROOT / "tools" / "templates"
ASSETS = ROOT / "tools" / "assets"
CHINA = timezone(timedelta(hours=8))
REPO_URL = "https://github.com/tengda-xmu/daily-papers"
SOURCE_LABELS = {item["id"]: item["label"] for item in SOURCE_CATALOG}
SETUP_HINTS = {
    "Elsevier": "尚未配置 Elsevier 检索授权。",
    "Google Scholar": "尚未配置 Google Scholar 检索服务。",
    "ResearchGate": "需要 SerpApi 公开索引密钥，或导入本地连接器的论文元数据。",
    "微信公众号": "可在设置中手动添加公众号，按订阅目录收集公开文章线索。",
}
AUTH_HINTS = {
    "Web of Science": "需要 Clarivate Starter API 密钥；可申请免费试用，网页登录不等于 API 授权。",
}
SETUP_SECTIONS = {"Elsevier": "elsevier", "Google Scholar": "scholar", "ResearchGate": "researchgate",
                  "微信公众号": "wechat", "Web of Science": "wos"}


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def safe_url(value, fallback="#") -> str:
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return esc(value)
    except ValueError:
        pass
    return fallback


def display_time(value) -> tuple[str, str]:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
        stamp = stamp.astimezone(CHINA)
        return stamp.strftime("%Y-%m-%d"), stamp.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return "日期待更新", "尚未生成"


def template(name: str, **values) -> str:
    return Template((TEMPLATES / name).read_text(encoding="utf-8")).substitute(values)


def document(content: str, *, title: str, root: str = "./", active: str = "daily") -> str:
    version = hashlib.sha256(
        b"".join((ASSETS / name).read_bytes() for name in ("site.css", "site.js", "daily-update.js", "paper-chat.css", "paper-chat.js", "paper-reader.css", "paper-reader.js", "manual-search.css", "manual-search.js", "journal-manager.css", "journal-manager.js", "research-directions.css", "research-directions.js", "wechat-subscriptions.css", "wechat-subscriptions.js"))
    ).hexdigest()[:10]
    return template(
        "page.html", content=content.lstrip(), title=esc(title), root=root, version=version,
        repo=REPO_URL,
        daily_current='aria-current="page"' if active == "daily" else "",
        archive_current='aria-current="page"' if active == "archive" else "",
        setup_current='aria-current="page"' if active in ("setup", "journals") else "",
        search_current='aria-current="page"' if active == "search" else "",
        leads_current='aria-current="page"' if active == "leads" else "",
        search_assets=(f'<link rel="stylesheet" href="{root}assets/manual-search.css?v={version}">'
                       f'<script src="{root}assets/manual-search.js?v={version}" defer></script>') if active == 'search' else
                      (f'<link rel="stylesheet" href="{root}assets/journal-manager.css?v={version}">'
                       f'<script src="{root}assets/journal-manager.js?v={version}" defer></script>') if active == 'journals' else
                      (f'<link rel="stylesheet" href="{root}assets/research-directions.css?v={version}">'
                       f'<script src="{root}assets/research-directions.js?v={version}" defer></script>') if active == 'directions' else
                      (f'<link rel="stylesheet" href="{root}assets/wechat-subscriptions.css?v={version}">'
                       f'<script src="{root}assets/wechat-subscriptions.js?v={version}" defer></script>') if active == 'setup' else
                      f'<script src="{root}assets/daily-update.js?v={version}" defer></script>' if active == 'daily' else '',
    )


def paper_figure(paper: dict, root: str = "./") -> str:
    figure = get_figure(paper.get("doi", ""))
    if not figure:
        return ""
    image_url = esc(root + figure["image_path"])
    title = f'{figure["figure_label"]} · {figure["title_zh"]}'
    return f'''
  <figure class="paper-figure">
    <a class="figure-preview" href="{image_url}" target="_blank" rel="noopener noreferrer" aria-label="{esc(title)}，查看大图">
      <img src="{image_url}" width="{figure['width']}" height="{figure['height']}" alt="{esc(figure['title_zh'])}，{esc(figure['figure_label'])}" loading="lazy" decoding="async">
      <span class="figure-open">查看大图</span>
    </a>
    <figcaption>
      <strong class="figure-title">{esc(title)}</strong>
      <p class="figure-explanation">图解：{esc(figure['caption_zh'])}</p>
      <p class="figure-credit">{esc(figure['credit'])}<br><a href="{safe_url(figure['source_url'])}" target="_blank" rel="noopener noreferrer">原文图页</a> · <a href="{safe_url(figure['license_url'])}" target="_blank" rel="noopener noreferrer">{esc(figure['license'])}</a><br>原图未改动，中文图解由本站整理。</p>
    </figcaption>
  </figure>'''


def paper_card(paper: dict, tier: str, rank: int = 0, root: str = "./", topic_labels=None) -> str:
    topic_labels = topic_labels or {k: v['label'] for k, v in TOPIC_CATALOG.items()}
    facets = paper_facets(paper)
    title = paper.get("title") or "未命名论文"
    doi = paper.get("doi") or ""
    url = safe_url(paper.get("landing_url") or paper.get("url") or
                   (f"https://doi.org/{doi}" if doi else ""))
    source = paper.get("source") or "来源待补充"
    venue = paper.get("venue") or ""
    author_names = string_list(paper.get("authors"))
    authors = ", ".join(author_names)
    short_authors = ", ".join(author_names[:3]) + (" 等" if len(author_names) > 3 else "")
    topics = string_list(paper.get("topic_tags"))
    tags = "".join(
        f'<span class="tag">{esc(topic_labels.get(topic, topic))}</span>'
        for topic in topics
    )
    summary = paper.get("summary") or paper.get("abstract") or "暂无摘要，请查看原文。"
    recommendation = paper.get("recommendation") or ""
    deep = paper.get("deep_read") or {}
    deep_items = [(label, deep.get(key)) for key, label in (
        ("problem", "研究问题"), ("method", "方法与路线"), ("innovation", "创新与比较"), ("findings", "证据与发现"),
        ("limitations", "局限与边界"), ("connection", "方向关联"), ("next_steps", "后续研究建议"),
    ) if deep.get(key)]
    deep_html = ""
    if deep_items and (tier == "core" or paper.get("analysis_status") == "ready"):
        deep_html = '<dl class="reading-notes">' + "".join(
            f'<div><dt>{label}</dt><dd>{esc(value)}</dd></div>' for label, value in deep_items
        ) + '</dl>'
    citation = ". ".join(value for value in (authors, title, venue,
                                            str(paper.get("published_at") or "")[:4],
                                            f"https://doi.org/{doi}" if doi else "") if value)
    primary_action = f'<a href="{url}" target="_blank" rel="noopener noreferrer">阅读原文<span class="sr-only">（新窗口）</span></a>' if url != "#" else ""
    actions = ""
    if paper.get("oa_url"):
        oa_url = safe_url(paper["oa_url"])
        if oa_url != "#":
            actions += f'<a href="{oa_url}" target="_blank" rel="noopener noreferrer">开放获取</a>'
    if doi:
        actions += f'<a href="https://doi.org/{esc(doi)}" target="_blank" rel="noopener noreferrer">DOI</a>'
    actions += f'<button type="button" class="text-button copy-citation" data-citation="{esc(citation)}">复制引用</button>'
    display_title = paper.get("title_zh") or title
    title_html = f'<a href="{url}" target="_blank" rel="noopener noreferrer">{esc(display_title)}</a>' if url != "#" else esc(display_title)
    search = " ".join(str(value or "") for value in
                      (title, display_title, authors, venue, summary, paper.get("abstract"), doi))
    venue_line = f'<span class="venue">{esc(venue)}</span>' if venue else ""
    recommendation_html = f'<p class="recommendation"><strong>阅读建议</strong>{esc(recommendation)}</p>' if recommendation else ""
    preview = str(summary)
    if len(preview) > 230:
        preview = preview[:230]
        if " " in preview[-24:]:
            preview = preview.rsplit(" ", 1)[0]
        preview = preview.rstrip(" ,.;，。；") + "…"
    panel_id = f"paper-details-{tier}-{rank}"
    full_authors = f'<p class="full-authors"><strong>作者</strong>{esc(authors)}</p>' if len(author_names) > 3 else ""
    ready = paper.get("analysis_status") == "ready"
    summary_label = "中文导读" if ready else "摘要"
    note_label = "中文精读" if ready else "摘要与笔记"
    translated_class = " translated" if paper.get("title_zh") else ""
    original = f'<p class="original-title" lang="en">{esc(title)}</p>' if paper.get("title_zh") else ""
    basis = "依据公开全文整理" if paper.get("analysis_basis") == "full_text" else "依据公开摘要整理，未核验全文细节"
    evidence_links = " · ".join(f'<a href="{safe_url(link)}" target="_blank" rel="noopener noreferrer">论文依据 {i + 1}</a>'
                                for i, link in enumerate(paper.get("analysis_sources") or []))
    provenance = f'<p class="analysis-provenance">{basis}。解读与建议不代表作者结论。 {evidence_links}</p>' if ready else ""
    journal_badge = '<span class="journal-priority">CNS 子刊</span>' if facets["venue_group"] == "CNS 子刊" else ""
    if source == "arXiv" or str(doi).lower().startswith("10.48550/arxiv."):
        journal_badge += '<span class="publication-type">预印本版本</span>'
    method_badges = "".join(f'<span class="method-focus">{esc(tag)}</span>'
                           for tag in focus_tags(title, paper.get("abstract", "")))
    direction = paper.get('recommended_direction') or next(iter(topics), '')
    direction_badge = f'<button type="button" class="text-button paper-direction" data-topic-filter="{esc(direction)}">{esc(topic_labels.get(direction, direction))}</button>' if direction else ''
    figure_html = paper_figure(paper, root) if tier == "core" else ""
    chat_button = (f'<button type="button" class="text-button codex-entry" data-paper-id="{esc(paper["id"])}" '
                   f'data-paper-title="{esc(display_title)}">Codex 对话</button>') if paper.get("id") else ""
    return f'''
<article class="paper {tier}" data-sources="{esc(json.dumps(facets['source_ids'], ensure_ascii=False))}"
 data-topics="{esc(json.dumps(topics, ensure_ascii=False))}" data-venue="{esc(facets['venue_group'])}"
 data-journal="{esc(facets['journal'])}" data-search="{esc(search)}" data-rank="{rank}"
 data-date="{esc(paper.get('published_at'))}">
  <div class="paper-meta"><span class="source">{esc(SOURCE_LABELS.get(source, source))}</span><span>{esc(str(paper.get('published_at') or '')[:10])}</span>{journal_badge}{method_badges}{direction_badge}</div>
  <h3 class="{translated_class.strip()}">{title_html}</h3>
  <p class="bibliography"><span class="authors">{esc(short_authors)}</span>{venue_line}</p>
  <p class="abstract">{esc(preview)}</p>
{figure_html}
  <div class="paper-tools">{primary_action}<button type="button" class="text-button paper-toggle" data-label="{note_label}" aria-expanded="false" aria-controls="{panel_id}">{note_label}<span aria-hidden="true">＋</span></button>{chat_button}</div>
  <div class="paper-detail-panel" id="{panel_id}" hidden>{original}{provenance}<p class="detail-label">{summary_label}</p><p class="full-abstract">{esc(summary)}</p>{recommendation_html}{full_authors}{deep_html}<div class="tags">{tags}</div><div class="paper-actions">{actions}</div></div>
</article>'''


def source_directory(statuses: dict, counts: Counter, root: str = "./", wechat_count: int = 0, *, reading_url: str | None = None) -> str:
    sections = []
    for kind, label, description in (
        ("adapter", "数据采集", "已实现来源及本轮采集状态；公开接口无需登录即可运行。"),
        ("planned", "待接入来源", "已纳入目录，尚未启用自动采集。"),
        ("platform", "出版平台", "依据论文的期刊与原文链接归类，可筛选本期已有记录。"),
    ):
        rows = []
        for item in (s for s in SOURCE_CATALOG if s["kind"] == kind):
            state, message = source_state(item, statuses)
            if state == "configuration_missing":
                message = SETUP_HINTS.get(item["id"], "该来源需要完成初始配置。")
            elif state == "authorization_required":
                message = AUTH_HINTS.get(item["id"], "该来源需要机构授权。")
            elif kind == "adapter":
                message = {
                    "ok": "本轮采集完成。", "no_data": "本轮检索未返回论文。",
                    "not_run": "尚无本轮采集记录。", "error": "采集失败，请查看运行记录。",
                    "access_denied": "授权或访问受限，请检查来源设置。",
                    "quota_exhausted": "接口限流或配额受限，等待恢复或调整配额。",
                    "partial": "部分期刊采集完成，其余请求失败；已有结果保留。",
                }.get(state, "请查看运行记录了解详情。")
            if state in ("ok", "no_data") and "RSS fallback" in (source_state(item, statuses)[1] or ""):
                message = "通过 arXiv 官方 RSS 获取；检索 API 本轮受限。"
            if item["id"] == "Elsevier" and state in ("ok", "no_data") and "STANDARD" in (source_state(item, statuses)[1] or ""):
                message = "Scopus 标准元数据检索已接入；完整视图需额外授权。"
            connector_message = source_state(item, statuses)[1] or ""
            if item["id"] == "ResearchGate" and connector_message.startswith(("本地连接器已导入", "公开索引已接入")):
                message = connector_message
            elif item["id"] == "微信公众号" and connector_message.startswith(("WeRSS ", "公开索引已接入")):
                message = connector_message
            elif item["id"] == "Web of Science" and connector_message.startswith(("Clarivate ", "密钥")):
                message = connector_message
            elif kind == "adapter" and state in ("ok", "no_data"):
                message += f' 本轮获取 {(statuses.get(item["id"]) or {}).get("count", 0)} 条元数据。'
            setup_link = f'<a href="{root}setup.html#{SETUP_SECTIONS.get(item["id"], "public")}">授权与配置</a>' if state in ("configuration_missing", "authorization_required", "quota_exhausted", "access_denied") else ""
            state_class = "ok" if state in ("ok", "no_data") else "pending" if state in ("planned", "platform", "not_run") else "warn"
            state_label = STATE_LABELS.get(state, '状态待确认')
            count_link = f'<button type="button" class="text-button" data-source-filter="{esc(item["id"])}">本期 {counts[item["id"]]} 篇</button>'
            if reading_url is not None:
                count_link = f'<a class="text-button" href="{esc(reading_url)}?source={quote(item["id"], safe="")}#reading">本期 {counts[item["id"]]} 篇</a>'
            if item["id"] == "微信公众号" and (wechat_count or reading_url is not None):
                target = root + 'leads.html' if reading_url is not None else '#wechat-articles'
                count_link = f'<a class="text-button" href="{esc(target)}">本期 {wechat_count} 条线索</a>'
                if connector_message.startswith("公开索引已接入"):
                    state_label = "公开索引可用" if state == "ok" else "公开索引部分可用"
            rows.append(f'''
<div class="source-row">
  <div><a class="source-name" href="{safe_url(item['url'])}" target="_blank" rel="noopener noreferrer">{esc(item['label'])}</a><p>{esc(message)}</p></div>
  <div class="source-state"><span class="state {state_class}">{esc(state_label)}</span>
  {count_link}{setup_link}</div>
</div>''')
        if not rows:
            continue
        sections.append(f'<details class="directory-group" {"open" if kind == "adapter" else ""}><summary>{label}<span>{len(rows)} 项</span></summary><p class="directory-description">{description}</p>{"".join(rows)}</details>')
    return "".join(sections)


def source_status_panel(payload: dict, root: str = './', *, reading_url: str | None = None) -> str:
    statuses = payload.get('source_status') or {}
    papers = (payload.get('core') or []) + (payload.get('extended') or [])
    counts = Counter(source for paper in papers for source in paper_facets(paper)['source_ids'])
    adapters = [source for source in SOURCE_CATALOG if source['kind'] == 'adapter']
    ok = sum(source_state(source, statuses)[0] in ('ok', 'no_data') for source in adapters)
    missing = sum(source_state(source, statuses)[0] in ('configuration_missing', 'authorization_required') for source in adapters)
    notice = ''
    if missing:
        notice = f'<p class="notice">{missing} 类来源尚待授权或连接器配置，其他来源继续采集。可通过下方“授权与配置”完成接入。</p>'
    elif not papers:
        notice = '<p class="notice">本期尚无符合条件的论文，请查看下方来源状态或浏览历史归档。</p>'
    policy = payload.get('selection_policy') or {}
    cns_window = f'CNS 专项与已有精读回溯 {int(policy.get("cns_lookback_days", 180))} 天，每日更新。' if policy.get('core_requires_chinese_analysis') else ''
    run_id = str(payload.get('update_run_id', ''))
    run_url = f'{REPO_URL}/actions/runs/{run_id}' if run_id.isdigit() else f'{REPO_URL}/actions/workflows/daily.yml'
    return template('source-status.html', ok_count=ok, adapter_count=len(adapters), notice=notice,
        generated=esc(display_time(payload.get('generated_at'))[1]),
        window_start=esc(display_time(payload.get('since'))[0]), window_end=esc(display_time(payload.get('until'))[0]),
        cns_window=esc(cns_window), root=root, run_url=run_url,
        sources=source_directory(statuses, counts, root, len(payload.get('wechat_articles') or []), reading_url=reading_url))


def journal_directory() -> str:
    groups = []
    for group in VENUE_GROUPS:
        journals = [j for j in JOURNALS if j["group"] == group["id"]]
        if not journals:
            continue
        links = "".join(
            f'<a class="journal-button" href="./?journal={quote(j["name"], safe="")}#reading" title="查看当期推荐：{esc(j["name"])}">{esc(j["name"])}</a>'
            for j in journals
        )
        groups.append(f'<details class="directory-group" {"open" if group["id"].startswith("CNS") else ""}><summary>{esc(group["id"])}<span>{len(journals)} 本</span></summary><div class="journal-list">{links}</div></details>')
    return "".join(groups)


def wechat_entries(records: list[dict]) -> str:
    entries = []
    for row in sorted(records, key=lambda r: r.get("published_at") or "", reverse=True):
        indexed = (row.get("raw_metadata") or {}).get("access_mode") == "public_index"
        label = "公开检索入口" if indexed else "微信公众号原文"
        entries.append(f'''<li class="archive-entry"><div><a href="{safe_url(row.get('landing_url'))}" target="_blank" rel="noopener noreferrer">{esc(row.get('title'))}</a>
<p>{esc(row.get('venue'))} · {esc(display_time(row.get('published_at'))[0])} · {label}</p>
<p>{esc(row.get('abstract', ''))}</p><a class="text-link" href="{safe_url(row.get('landing_url'))}" target="_blank" rel="noopener noreferrer">{label}</a></div></li>''')
    return ''.join(entries)


def wechat_articles_panel(records: list[dict]) -> str:
    if not records:
        return ''
    return f'''<details id="wechat-articles" class="reference-panel"><summary><span>微信公众号 · 科研线索</span><span class="reference-meta">{len(records)} 条</span></summary>
<div class="reference-body"><p class="section-description">按已订阅的公众号筛选。标注“公开检索入口”的条目提供标题、来源和检索片段，点击标题可继续查找原文；文章内容及结论尚待核验。
文章跳转若要求验证码，请在浏览器中手动完成。这些线索不占用核心与扩展论文名额。</p><ul>{wechat_entries(records)}</ul></div></details>'''


def render_leads(payload: dict) -> str:
    records = payload.get('wechat_articles') or []
    entries = f'<ul class="archive-list">{wechat_entries(records)}</ul>' if records else '<p class="empty">本期暂无公众号线索，可在设置中查看订阅与采集状态。</p>'
    return document(template('leads.html', count=len(records), entries=entries,
        generated=esc(display_time(payload.get('generated_at'))[1])), title='科研线索 | 每日论文推荐', active='leads')


def render(payload: dict, *, archive_date: str | None = None) -> str:
    core = payload.get("core") or []
    extended = payload.get("extended") or []
    all_papers = core + extended
    current_profile = load_profile()
    profile = payload.get('research_profile')
    topic_labels = ({d['id']: d['name'] for d in profile['directions']} if profile else
                    {k: v['label'] for k, v in TOPIC_CATALOG.items()})
    shown_directions = active_directions(profile) if profile else [
        {'id': k, 'name': v} for k, v in topic_labels.items()]
    statuses = payload.get("source_status") or {}
    facets_list = [paper_facets(p) for p in all_papers]
    counts = Counter(source for f in facets_list for source in f["source_ids"])
    source_options = []
    for kind, label in (("adapter", "数据采集"), ("planned", "待接入来源"), ("platform", "出版平台")):
        options = []
        for item in (s for s in SOURCE_CATALOG if s["kind"] == kind):
            state, _ = source_state(item, statuses)
            options.append(f'<option value="{esc(item["id"])}">{esc(item["label"])}（{counts[item["id"]]} 篇）</option>')
        if options:
            source_options.append(f'<optgroup label="{label}">{"".join(options)}</optgroup>')
    unknown_sources = sorted(set(counts) - set(SOURCE_LABELS))
    source_options.extend(f'<option value="{esc(s)}">{esc(s)} · {counts[s]} 篇</option>' for s in unknown_sources)
    topics = list(dict.fromkeys([*(d['id'] for d in shown_directions), *(t for p in all_papers for t in string_list(p.get("topic_tags")))]))
    topic_options = "".join(f'<option value="{esc(t)}">{esc(topic_labels.get(t, t))}</option>' for t in topics)
    group_options = "".join(f'<option value="{esc(g["id"])}">{esc(g["id"])}</option>' for g in VENUE_GROUPS)
    journals = {j["name"]: j["group"] for j in JOURNALS}
    journals.update({f["journal"]: f["venue_group"] for f in facets_list if f["journal"]})
    journal_options = "".join(f'<option value="{esc(name)}" data-group="{esc(group)}">{esc(name)}</option>' for name, group in journals.items())
    day, generated = display_time(payload.get("generated_at"))
    issue = archive_date or day
    root = "../" if archive_date else "./"
    core_html = "".join(paper_card(p, "core", i, root, topic_labels) for i, p in enumerate(core))
    extended_html = "".join(paper_card(p, "extended", i, root, topic_labels) for i, p in enumerate(extended))
    direction_chips = ''.join(f'<button type="button" data-topic-filter="{esc(d["id"])}" aria-pressed="false">{esc(d["name"])}<span>{sum(d["id"] in p.get("topic_tags", []) for p in all_papers)}</span></button>' for d in shown_directions)
    direction_bar = f'<div class="direction-bar"><span>本期方向</span><div class="direction-chips">{direction_chips}</div>'
    direction_bar += f'<a href="{root}directions.html">管理方向</a></div>'
    if not archive_date and profile and profile_revision(profile) != profile_revision(current_profile):
        direction_bar += '<p class="direction-pending">方向设置已变更，点击“手动更新”后按新设置生成推荐。</p>'
    ok = sum(source_state(s, statuses)[0] in ("ok", "no_data") for s in SOURCE_CATALOG if s["kind"] == "adapter")
    adapter_count = sum(s["kind"] == "adapter" for s in SOURCE_CATALOG)
    cns_children = sum(j["group"] == "CNS 子刊" for j in JOURNALS)
    archive_notice = f'<p class="archive-notice">正在阅读 {esc(archive_date)} 归档。<a href="../">返回最新一期</a></p>' if archive_date else ""
    update_control = '<button class="manual-update" id="manual-update" type="button">手动更新</button>' if not archive_date else ''
    update_panel = '''<div id="daily-update-panel" class="daily-update-panel" hidden>
  <p id="daily-update-status" role="status" aria-live="polite"></p>
  <form id="daily-update-pair" hidden>
    <p>请启动本机论文助手，首次使用粘贴配对码；记住浏览器后可直接更新。</p>
    <div class="update-pair-fields"><label for="daily-pair-code" class="sr-only">配对码</label><input id="daily-pair-code" type="password" placeholder="粘贴配对码" autocomplete="off" required><button type="submit" class="manual-update">连接并更新</button></div>
    <label><input id="daily-remember" type="checkbox" checked> 记住此浏览器</label>
  </form>
  <div class="update-links"><a id="daily-update-run" target="_blank" rel="noopener noreferrer" hidden>查看更新进度</a><a id="daily-update-local" href="http://127.0.0.1:43127/recommendations.html" target="_blank" rel="noopener noreferrer" hidden>在本机更新推荐</a></div>
</div>''' if not archive_date else ''
    reference_panels = ''
    if archive_date:
        reference_panels = '<div class="reference-area">' + wechat_articles_panel(payload.get('wechat_articles') or []) + source_status_panel(payload, root) + '</div>'
    content = template(
        "daily.html", title="每日论文推荐", day=esc(issue), generated=esc(generated),
        archive_notice=archive_notice, history_url="./" if archive_date else "archive/",
        update_control=update_control, update_panel=update_panel,
        direction_bar=direction_bar,
        generated_at=esc(payload.get('generated_at', '')), update_run_id=esc(payload.get('update_run_id', '')),
        source_options="".join(source_options), health_label=f"{ok} / {adapter_count} 类来源正常",
        sources_url='#sources' if archive_date else root + 'setup.html#sources', reference_panels=reference_panels,
        topic_options=topic_options, group_options=group_options, journal_options=journal_options,
        core_count=len(core), extended_count=len(extended), total=len(all_papers),
        core_html=core_html, extended_html=extended_html,
        core_empty="hidden" if core else "", extended_empty="hidden" if extended else "",
        no_data="" if not all_papers else "hidden", no_match="hidden",
        cns_children=cns_children, root=root,
    )
    return document(content, title=f"{issue} · 每日论文推荐" if archive_date else "每日论文推荐 | 腾达", root=root,
                    active="archive" if archive_date else "daily")


def build_archive() -> None:
    archive_out = OUT / "archive"
    archive_out.mkdir(parents=True, exist_ok=True)
    entries = []
    for source in sorted((DATA / "archive").glob("*.json"), reverse=True):
        payload = read_json(source, {})
        shutil.copyfile(source, archive_out / source.name)
        (archive_out / f"{source.stem}.html").write_text(render(payload, archive_date=source.stem), encoding="utf-8")
        core, extended = len(payload.get("core") or []), len(payload.get("extended") or [])
        entries.append(f'''<li class="archive-entry"><div><a class="archive-date" href="{esc(source.stem)}.html">{esc(source.stem)}</a><p>核心推荐 {core} 篇 · 扩展阅读 {extended} 篇</p></div><a class="text-link" href="{esc(source.stem)}.json" download>下载数据</a></li>''')
    content = template("archive.html", entries="".join(entries) or '<li class="empty">尚无历史归档，首期日报生成后会在此展示。</li>', count=len(entries))
    (archive_out / "index.html").write_text(document(content, title="历史归档 | 每日论文推荐", root="../", active="archive"), encoding="utf-8")


def wechat_directory() -> str:
    try:
        snapshot = public_subscriptions(read_json(DATA / "inbox/wechat-subscriptions.json", {}))
    except (ValueError, TypeError):
        snapshot = {'updated_at': '', 'discovery': {'status': 'not_run', 'daily_queries': 0,
            'daily_additions': 0, 'max_subscriptions': 0}}
    accounts = [row for row in effective_accounts(ROOT, manual=published_accounts(ROOT)) if row['enabled']]
    discovery = snapshot['discovery']
    groups = {}
    for account in accounts:
        for group in account["groups"] or ["科研综合"]:
            groups.setdefault(group, []).append(account["name"])
    entries = "".join(f'<li><strong>{esc(group)}</strong>：{esc("、".join(names))}</li>' for group, names in groups.items())
    state = {"ok": "正常运行", "not_run": "尚未运行", "disabled": "已停用", "quota_exhausted": "微信限频，等待下次运行",
             "access_denied": "微信授权需要更新", "error": "本轮发现失败", "capacity_reached": "已达到订阅上限"}[discovery["status"]]
    return f'''<details><summary>查看已订阅的 {len(accounts)} 个公众号与自动扩展状态</summary>
<p>目录同步：{esc(display_time(snapshot["updated_at"])[1])}（北京时间）。自动发现：{esc(state)}。</p>
<ul>{entries}</ul><p>每天最多检索 {discovery["daily_queries"]} 个主题、新增 {discovery["daily_additions"]} 个相关账号；
自动发现订阅上限 {discovery["max_subscriptions"]} 个。达到上限后保留候选，需调整配置后继续扩展。
领域标签按账号名称和简介匹配，不代表对账号或文章的质量背书。</p></details>'''


def wechat_manager() -> str:
    overview = group_overview(ROOT, manual=published_accounts(ROOT))
    options = ''.join(f'<option value="{esc(group["name"])}"'
        + (' selected' if group['name'] == '科研综合' else '')
        + f'>{esc(group["name"])}（{group["total"]} 个）</option>' for group in overview['groups'])
    rows = ''.join(f'<tr><th scope="row">{esc(group["name"])}</th><td>{esc(group["description"])}</td>'
        f'<td>{group["total"]}</td><td>{group["enabled"]}</td></tr>' for group in overview['groups'])
    return template('wechat-manager.html', group_options=options, group_rows=rows,
        group_total=f'{len(overview["groups"])} 类 · {overview["total"]} 个公众号',
        group_note=f'已发布目录：共 {overview["total"]} 个公众号，启用 {overview["enabled"]} 个，'
            f'{overview["multi_group"]} 个归入多个分组。')


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ASSETS, OUT / "assets", dirs_exist_ok=True)
    payload = read_json(DATA / "daily.json", {})
    (OUT / "index.html").write_text(render(payload), encoding="utf-8")
    (OUT / "data.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    build_archive()
    (OUT / "setup.html").write_text(document(template("setup.html", wechat_directory=wechat_directory(), wechat_manager=wechat_manager(),
        sources_panel=source_status_panel(payload, reading_url='./'),
        journals=journal_directory(), journal_count=len(JOURNALS), group_count=len({j['group'] for j in JOURNALS})),
        title="设置 | 每日论文推荐", active="setup"), encoding="utf-8")
    (OUT / 'leads.html').write_text(render_leads(payload), encoding='utf-8')
    from src.manual_search import SOURCES
    choices = ''.join(f'<label title="{esc(s["mode"])}"><input type="checkbox" name="library" value="{esc(s["id"])}" checked> {esc(s["label"])}</label>' for s in SOURCES)
    (OUT / "search.html").write_text(document(template("search.html", sources=choices),
        title="手动检索文献 | 每日论文推荐", active="search"), encoding="utf-8")
    (OUT / "journals.html").write_text(document(template("journals.html"),
        title="管理期刊 | 每日论文推荐", active="journals"), encoding="utf-8")
    (OUT / 'directions.html').write_text(document(template('directions.html',
        profile=esc(json.dumps(load_profile(), ensure_ascii=False))),
        title='研究方向 | 每日论文推荐', active='directions'), encoding='utf-8')


if __name__ == "__main__":
    main()
