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
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.catalog import (JOURNALS, SOURCE_CATALOG, STATE_LABELS, TOPIC_CATALOG,
                         VENUE_GROUPS, paper_facets, source_state, string_list)
from src.research_focus import focus_tags
from src.figures import get_figure

DATA = ROOT / "data"
OUT = ROOT / "site"
TEMPLATES = ROOT / "tools" / "templates"
ASSETS = ROOT / "tools" / "assets"
CHINA = timezone(timedelta(hours=8))
REPO_URL = "https://github.com/tengda-xmu/daily-papers"
MANUAL_UPDATE_URL = f"{REPO_URL}/actions/workflows/daily.yml"
SOURCE_LABELS = {item["id"]: item["label"] for item in SOURCE_CATALOG}
SETUP_HINTS = {
    "Elsevier": "尚未配置 Elsevier 检索授权。",
    "Google Scholar": "尚未配置 Google Scholar 检索服务。",
    "ResearchGate": "需要 SerpApi 公开索引密钥，或导入本地连接器的论文元数据。",
    "微信公众号": "需要 WeRSS 订阅地址，或完成本机扫码、添加订阅并同步文章。",
}
AUTH_HINTS = {
    "Web of Science": "需要 Clarivate Starter API 密钥，可申请试用或机构方案。",
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
        (ASSETS / "site.css").read_bytes() + (ASSETS / "site.js").read_bytes()
    ).hexdigest()[:10]
    return template(
        "page.html", content=content.lstrip(), title=esc(title), root=root, version=version,
        repo=REPO_URL,
        daily_current='aria-current="page"' if active == "daily" else "",
        archive_current='aria-current="page"' if active == "archive" else "",
        setup_current='aria-current="page"' if active == "setup" else "",
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


def paper_card(paper: dict, tier: str, rank: int = 0, root: str = "./") -> str:
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
        f'<span class="tag">{esc(TOPIC_CATALOG.get(topic, {}).get("label", topic))}</span>'
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
    figure_html = paper_figure(paper, root) if tier == "core" else ""
    return f'''
<article class="paper {tier}" data-sources="{esc(json.dumps(facets['source_ids'], ensure_ascii=False))}"
 data-topics="{esc(json.dumps(topics, ensure_ascii=False))}" data-venue="{esc(facets['venue_group'])}"
 data-journal="{esc(facets['journal'])}" data-search="{esc(search)}" data-rank="{rank}"
 data-date="{esc(paper.get('published_at'))}">
  <div class="paper-meta"><span class="source">{esc(SOURCE_LABELS.get(source, source))}</span><span>{esc(str(paper.get('published_at') or '')[:10])}</span>{journal_badge}{method_badges}</div>
  <h3 class="{translated_class.strip()}">{title_html}</h3>
  <p class="bibliography"><span class="authors">{esc(short_authors)}</span>{venue_line}</p>
  <p class="abstract">{esc(preview)}</p>
{figure_html}
  <div class="paper-tools">{primary_action}<button type="button" class="text-button paper-toggle" data-label="{note_label}" aria-expanded="false" aria-controls="{panel_id}">{note_label}<span aria-hidden="true">＋</span></button></div>
  <div class="paper-detail-panel" id="{panel_id}" hidden>{original}{provenance}<p class="detail-label">{summary_label}</p><p class="full-abstract">{esc(summary)}</p>{recommendation_html}{full_authors}{deep_html}<div class="tags">{tags}</div><div class="paper-actions">{actions}</div></div>
</article>'''


def source_directory(statuses: dict, counts: Counter, root: str = "./") -> str:
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
            elif item["id"] == "微信公众号" and connector_message.startswith("WeRSS "):
                message = connector_message
            elif kind == "adapter" and state in ("ok", "no_data"):
                message += f' 本轮获取 {(statuses.get(item["id"]) or {}).get("count", 0)} 条元数据。'
            setup_link = f'<a href="{root}setup.html#{SETUP_SECTIONS.get(item["id"], "public")}">授权与配置</a>' if state in ("configuration_missing", "authorization_required", "quota_exhausted", "access_denied") else ""
            state_class = "ok" if state in ("ok", "no_data") else "pending" if state in ("planned", "platform", "not_run") else "warn"
            rows.append(f'''
<div class="source-row">
  <div><a class="source-name" href="{safe_url(item['url'])}" target="_blank" rel="noopener noreferrer">{esc(item['label'])}</a><p>{esc(message)}</p></div>
  <div class="source-state"><span class="state {state_class}">{esc(STATE_LABELS.get(state, '状态待确认'))}</span>
  <button type="button" class="text-button" data-source-filter="{esc(item['id'])}">本期 {counts[item['id']]} 篇</button>{setup_link}</div>
</div>''')
        if not rows:
            continue
        sections.append(f'<details class="directory-group" {"open" if kind == "adapter" else ""}><summary>{label}<span>{len(rows)} 项</span></summary><p class="directory-description">{description}</p>{"".join(rows)}</details>')
    return "".join(sections)


def journal_directory() -> str:
    groups = []
    for group in VENUE_GROUPS:
        journals = [j for j in JOURNALS if j["group"] == group["id"]]
        if not journals:
            continue
        buttons = "".join(
            f'<button type="button" class="journal-button" data-journal-filter="{esc(j["name"])}" data-group="{esc(group["id"])}">{esc(j["name"])}</button>'
            for j in journals
        )
        groups.append(f'<details class="directory-group" {"open" if group["id"].startswith("CNS") else ""}><summary>{esc(group["id"])}<span>{len(journals)} 本</span></summary><div class="journal-list">{buttons}</div></details>')
    return "".join(groups)


def render(payload: dict, *, archive_date: str | None = None) -> str:
    core = payload.get("core") or []
    extended = payload.get("extended") or []
    all_papers = core + extended
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
    topics = list(dict.fromkeys([*TOPIC_CATALOG, *(t for p in all_papers for t in string_list(p.get("topic_tags")))]))
    topic_options = "".join(f'<option value="{esc(t)}">{esc(TOPIC_CATALOG.get(t, {}).get("label", t))}</option>' for t in topics)
    group_options = "".join(f'<option value="{esc(g["id"])}">{esc(g["id"])}</option>' for g in VENUE_GROUPS)
    journals = {j["name"]: j["group"] for j in JOURNALS}
    journals.update({f["journal"]: f["venue_group"] for f in facets_list if f["journal"]})
    journal_options = "".join(f'<option value="{esc(name)}" data-group="{esc(group)}">{esc(name)}</option>' for name, group in journals.items())
    day, generated = display_time(payload.get("generated_at"))
    issue = archive_date or day
    root = "../" if archive_date else "./"
    core_html = "".join(paper_card(p, "core", i, root) for i, p in enumerate(core))
    extended_html = "".join(paper_card(p, "extended", i, root) for i, p in enumerate(extended))
    missing = sum(source_state(s, statuses)[0] in ("configuration_missing", "authorization_required") for s in SOURCE_CATALOG if s["kind"] == "adapter")
    ok = sum(source_state(s, statuses)[0] in ("ok", "no_data") for s in SOURCE_CATALOG if s["kind"] == "adapter")
    adapter_count = sum(s["kind"] == "adapter" for s in SOURCE_CATALOG)
    notice = ""
    if missing:
        notice = f'<div class="notice"><span class="status-dot" aria-hidden="true"></span><p>{missing} 类来源尚待授权或连接器配置，其他来源继续采集。</p><a href="{root}setup.html">完成来源配置</a></div>'
    elif not all_papers:
        notice = '<div class="notice"><span class="status-dot" aria-hidden="true"></span><p>本期尚无符合条件的论文，请查看来源状态或历史归档。</p><a href="#sources">查看来源状态</a></div>'
    cns_children = sum(j["group"] == "CNS 子刊" for j in JOURNALS)
    policy = payload.get("selection_policy") or {}
    reading_policy = "点击论文下方按钮展开摘要与阅读笔记。"
    cns_window = ""
    if policy.get("core_requires_chinese_analysis"):
        days = int(policy.get("cns_lookback_days", 180))
        focus = " · 侧重大模型与智能体" if policy.get("within_venue_priority") else ""
        reading_policy = f"CNS 子刊优先{focus} · 精选近 {days} 天论文"
        cns_window = f" CNS 专项与已有精读回溯 {days} 天，每日更新。"
    archive_notice = f'<p class="archive-notice">正在阅读 {esc(archive_date)} 归档。<a href="../">返回最新一期</a></p>' if archive_date else ""
    content = template(
        "daily.html", title="每日论文推荐", day=esc(issue), generated=esc(generated),
        reading_policy=esc(reading_policy), cns_window=esc(cns_window),
        extended_policy="每篇均有完整中文精读，点击论文下方展开。" if policy.get("extended_requires_chinese_analysis") else "",
        archive_notice=archive_notice, history_url="./" if archive_date else "archive/", manual_update_url=MANUAL_UPDATE_URL,
        notice=notice, source_options="".join(source_options), health_label=f"{ok} / {adapter_count} 类来源正常",
        topic_options=topic_options, group_options=group_options, journal_options=journal_options,
        core_count=len(core), extended_count=len(extended), total=len(all_papers),
        core_html=core_html, extended_html=extended_html,
        core_empty="hidden" if core else "", extended_empty="hidden" if extended else "",
        no_data="" if not all_papers else "hidden", no_match="hidden",
        sources=source_directory(statuses, counts, root), journals=journal_directory(),
        window_start=esc(display_time(payload.get("since"))[0]), window_end=esc(display_time(payload.get("until"))[0]),
        source_count=len(SOURCE_CATALOG), journal_count=len(JOURNALS), group_count=len(VENUE_GROUPS),
        cns_children=cns_children, adapter_count=adapter_count, ok_count=ok, root=root, repo=REPO_URL,
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


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ASSETS, OUT / "assets", dirs_exist_ok=True)
    payload = read_json(DATA / "daily.json", {})
    (OUT / "index.html").write_text(render(payload), encoding="utf-8")
    (OUT / "data.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    build_archive()
    (OUT / "setup.html").write_text(document(template("setup.html"), title="来源配置 | 每日论文推荐", active="setup"), encoding="utf-8")


if __name__ == "__main__":
    main()
