"""Build the standalone static GitHub Pages site from ``data/daily.json``."""
from __future__ import annotations

import html
import json
import shutil
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.catalog import (SOURCE_CATALOG, VENUE_GROUPS, JOURNALS, TOPIC_CATALOG,
                         STATE_LABELS, paper_facets, source_state)
DATA = ROOT / "data"
OUT = ROOT / "site"


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def paper_card(paper: dict, tier: str) -> str:
    facets = paper_facets(paper)
    title = esc(paper.get("title", "未命名论文"))
    url = esc(paper.get("landing_url") or paper.get("url") or
              (f"https://doi.org/{paper['doi']}" if paper.get("doi") else "#"))
    source = esc(paper.get("source", "unknown"))
    venue = esc(paper.get("venue", ""))
    venue_group = esc(facets['venue_group'])
    authors = paper.get("authors", [])
    authors_text = ", ".join(authors) if isinstance(authors, list) else str(authors)
    topics = paper.get("topic_tags") or []
    if isinstance(topics, str):
        topics = [topics]
    tags = " ".join(f'<span class="tag">{esc(TOPIC_CATALOG.get(topic, {}).get("label", topic))}</span>' for topic in topics)
    summary = esc(paper.get("summary") or paper.get("abstract") or "暂无摘要")
    recommendation = esc(paper.get("recommendation", ""))
    deep = paper.get("deep_read") or {}
    deep_html = ""
    if tier == "core":
        deep_html = (
            '<details><summary>查看精读要点</summary>'
            f'<p><b>研究问题：</b>{esc(deep.get("problem", ""))}</p>'
            f'<p><b>方法：</b>{esc(deep.get("method", ""))}</p>'
            f'<p><b>主要发现：</b>{esc(deep.get("findings", ""))}</p>'
            f'<p><b>局限：</b>{esc(deep.get("limitations", ""))}</p>'
            f'<p><b>方向关联：</b>{esc(deep.get("connection", ""))}</p>'
            '</details>'
        )
    doi = f'<a class="minor-link" href="https://doi.org/{esc(paper["doi"])}" target="_blank" rel="noopener">DOI</a>' if paper.get("doi") else ""
    venue_badge = f'<span class="venue-group">{venue_group}</span>' if venue_group else ""
    venue_line = f'<p class="venue">期刊：{venue} {venue_badge}</p>' if venue else ""
    return f'''
    <article class="paper" data-source="{source}" data-sources="{esc(json.dumps(facets['source_ids'], ensure_ascii=False))}" data-topics="{esc(json.dumps(topics, ensure_ascii=False))}" data-venue="{venue_group}" data-journal="{esc(facets['journal'])}" data-search="{esc((paper.get("title", "") + " " + authors_text + " " + paper.get("venue", "") + " " + paper.get("summary", "")).casefold())}">
      <div class="meta"><span class="source">{source}</span><span>{esc(paper.get("published_at", ""))}</span><span>{'核心推荐' if tier == 'core' else '扩展阅读'}</span></div>
      <h3><a href="{url}" target="_blank" rel="noopener">{title}</a></h3>
      <p class="authors">{esc(authors_text)}</p>
      {venue_line}
      <p>{summary}</p>
      <p class="recommendation">{recommendation}</p>
      <div>{tags} {doi}</div>
      {deep_html}
    </article>'''


def status_rows(statuses: dict) -> str:
    rows = []
    for source, status in statuses.items():
        if isinstance(status, dict):
            state = status.get("status", "unknown")
            detail = f'{status.get("count", 0)} 条'
            message = status.get("message", "")
        else:
            state, detail, message = str(status), "", ""
        cls = "ok" if state == "ok" else "warn"
        label = STATE_LABELS.get(state, state)
        rows.append(f'<span class="status {cls}"><b>{esc(source)}</b>：{esc(label)} {esc(detail)} {esc(message)}</span>')
    return "".join(rows) or '<span class="status warn">暂无运行状态</span>'


def render(payload: dict) -> str:
    core = payload.get("core", [])
    extended = payload.get("extended", [])
    all_papers = core + extended
    statuses = payload.get("source_status", {})
    paper_facets_list = [paper_facets(paper) for paper in all_papers]
    source_ids = {item['id'] for item in SOURCE_CATALOG}
    source_ids.update(source for facets in paper_facets_list for source in facets['source_ids'])
    source_options = "".join(
        f'<option value="{esc(item["id"])}">{esc(item["label"])} · {esc(STATE_LABELS.get(source_state(item, statuses)[0], source_state(item, statuses)[0]))}</option>'
        for item in SOURCE_CATALOG if item['id'] in source_ids
    )
    topics = sorted({topic for p in all_papers for topic in (p.get("topic_tags") or [])})
    topics = sorted(set(TOPIC_CATALOG) | set(topics))
    topic_options = "".join(f'<option value="{esc(topic)}">{esc(TOPIC_CATALOG.get(topic, {}).get("label", topic))}</option>' for topic in topics)
    venue_group_ids = [item['id'] for item in VENUE_GROUPS]
    venue_group_options = "".join(f'<option value="{esc(group)}">{esc(group)}</option>' for group in venue_group_ids)
    journal_options = "".join(f'<option value="{esc(item["name"])}">{esc(item["name"])} · {esc(item["group"])}</option>' for item in JOURNALS)
    count_by_source = Counter(source for facets in paper_facets_list for source in facets['source_ids'])
    source_legend = "".join(
        f'<span class="catalog-item"><b>{esc(item["label"])}</b>：{esc(STATE_LABELS.get(source_state(item, statuses)[0], source_state(item, statuses)[0]))}，{count_by_source.get(item["id"], 0)} 篇</span>'
        for item in SOURCE_CATALOG
    )
    journal_legend = "".join(f'<span class="catalog-item">{esc(item["name"])} · {esc(item["group"])}</span>' for item in JOURNALS)
    core_html = "".join(paper_card(paper, "core") for paper in core) or '<p class="empty">暂无核心推荐。</p>'
    extended_html = "".join(paper_card(paper, "extended") for paper in extended) or '<p class="empty">暂无扩展阅读。</p>'
    generated = esc(payload.get("generated_at", date.today().isoformat()))
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>每日论文推荐</title>
<style>
:root{{--bg:#f5f7fb;--ink:#172033;--muted:#667085;--blue:#1463c3;--line:#e5e9f2;--card:#fff;--gold:#9a6700;--goldbg:#fff5d6}}
*{{box-sizing:border-box}}body{{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;max-width:1180px;margin:auto;padding:24px;background:var(--bg);color:var(--ink)}}
header{{padding:20px 0 10px}}h1{{font-size:32px;margin:0 0 8px}}h2{{margin-top:30px;border-bottom:2px solid var(--line);padding-bottom:8px}}h3{{margin:9px 0;font-size:19px;line-height:1.4}}a{{color:var(--blue);text-decoration:none}}a:hover{{text-decoration:underline}}
.subtitle,.meta,.authors,.venue,.status,.minor-link{{color:var(--muted);font-size:13px}}.coverage{{margin:16px 0;padding:13px 16px;background:#fff9e8;border:1px solid #f1d58a;border-radius:10px;color:#6d4d00;line-height:1.9;font-size:14px}}.coverage b{{color:#8a5d00}}.statusbar{{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}}.status{{background:#eef1f6;border-radius:6px;padding:5px 8px}}.status.ok{{color:#146b3a;background:#e9f8ef}}.status.warn{{color:#8b5e00;background:#fff5d6}}.catalog{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}}.catalog details{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:0}}.catalog summary{{font-size:14px}}.catalog-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:6px;margin-top:10px}}.catalog-item{{padding:6px 8px;background:#f8fafc;border-radius:6px;color:var(--muted);font-size:12px;line-height:1.4}}@media(max-width:760px){{body{{padding:14px}}h1{{font-size:28px}}.catalog{{grid-template-columns:1fr}}input,select{{min-width:100%;width:100%}}.meta{{flex-wrap:wrap}}}}
.controls{{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}}input,select{{padding:10px;border:1px solid #ccd3e0;border-radius:8px;background:#fff;min-width:190px}}.paper{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin:12px 0;box-shadow:0 2px 9px #18284a0d}}.paper[hidden]{{display:none}}.meta{{display:flex;gap:10px;align-items:center}}.source{{background:#e8f1ff;color:#1757a6;border-radius:99px;padding:4px 8px}}.venue{{margin:4px 0 10px}}.venue-group{{color:var(--gold);background:var(--goldbg);border-radius:99px;padding:3px 8px;margin-left:6px;font-size:12px;font-weight:600}}.tag{{display:inline-block;font-size:12px;background:#eef0f4;border-radius:99px;padding:3px 8px;margin:3px 5px 3px 0}}.recommendation{{color:#374151;background:#f8fafc;border-left:3px solid #9db9e8;padding:8px 10px}}details{{margin-top:13px;border-top:1px solid var(--line);padding-top:10px}}summary{{cursor:pointer;color:var(--blue);font-weight:600}}.empty{{padding:24px;text-align:center;color:var(--muted)}}.minor-link{{margin-left:8px}}footer{{margin-top:35px;padding-top:15px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}}
</style></head><body>
<header><h1>每日论文推荐</h1><div class="subtitle">面向 AI 智能运维、结构生成式设计、结构疲劳与可靠性设计 · 覆盖 CNS 正刊与子刊 · 更新时间：{generated}</div></header>
<section class="coverage" aria-label="检索范围"><b>来源目录：</b>{len(SOURCE_CATALOG)} 个来源 · 已实现适配器 {sum(item['kind'] == 'adapter' for item in SOURCE_CATALOG)} 个 · 计划接入 {sum(item['kind'] == 'planned' for item in SOURCE_CATALOG)} 个 · 期刊平台筛选 {sum(item['kind'] == 'platform' for item in SOURCE_CATALOG)} 个<br><b>期刊目录：</b>{len(JOURNALS)} 个重点期刊，包含 CNS 正刊与子刊、Elsevier、Springer、Wiley、IEEE、ACM、ASME、ASCE、AIAA、SAGE、Taylor &amp; Francis、SIAM 等</section>
<div class="statusbar">{status_rows(payload.get("source_status", {}))}</div>
<section class="catalog"><details><summary>查看来源接入状态（{len(SOURCE_CATALOG)}）</summary><div class="catalog-grid">{source_legend}</div></details><details><summary>查看重点期刊目录（{len(JOURNALS)}）</summary><div class="catalog-grid">{journal_legend}</div></details></section>
<div class="controls"><input id="search" placeholder="搜索标题、作者、期刊或摘要"><select id="source"><option value="">全部来源</option>{source_options}</select><select id="topic"><option value="">全部主题</option>{topic_options}</select><select id="venue"><option value="">全部期刊组</option>{venue_group_options}</select><select id="journal"><option value="">全部具体期刊</option>{journal_options}</select></div>
<section><h2>核心推荐（{len(core)}）</h2><div id="core">{core_html}</div></section>
<section><h2>扩展阅读（{len(extended)}）</h2><div id="extended">{extended_html}</div></section>
<footer>论文标题、摘要和元数据来自原始来源；请通过原文链接访问完整内容。<a href="archive/">查看历史归档</a></footer>
<script>const q=document.querySelector('#search'),s=document.querySelector('#source'),t=document.querySelector('#topic'),v=document.querySelector('#venue'),j=document.querySelector('#journal');function filter(){{const text=q.value.toLowerCase(),source=s.value,topic=t.value,venue=v.value,journal=j.value;document.querySelectorAll('.paper').forEach(x=>{{const sources=JSON.parse(x.dataset.sources||'[]'),topics=JSON.parse(x.dataset.topics||'[]'),okText=!text||x.dataset.search.includes(text),okSource=!source||sources.includes(source),okTopic=!topic||topics.includes(topic),okVenue=!venue||x.dataset.venue===venue,okJournal=!journal||x.dataset.journal===journal;x.hidden=!(okText&&okSource&&okTopic&&okVenue&&okJournal)}})}}q.oninput=filter;s.onchange=filter;t.onchange=filter;v.onchange=filter;j.onchange=filter;</script>
</body></html>'''


def build_archive() -> None:
    archive_out = OUT / "archive"
    archive_out.mkdir(parents=True, exist_ok=True)
    entries = []
    archive_files = sorted((DATA / "archive").glob("*.json")) if (DATA / "archive").exists() else []
    for source in archive_files:
        target = archive_out / source.name
        shutil.copyfile(source, target)
        payload = read_json(source, {})
        entries.append((source.stem, payload.get("generated_at", ""), len(payload.get("papers", []))))
    links = "".join(f'<li><a href="{esc(name)}.json">{esc(name)}</a>（{count} 篇，{esc(generated)}）</li>' for name, generated, count in reversed(entries))
    (archive_out / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>历史归档</title>'
        '<style>body{font-family:system-ui;max-width:800px;margin:40px auto;padding:0 20px}a{color:#1463c3}</style>'
        '<h1>历史归档</h1><ul>' + (links or "<li>暂无归档</li>") + '</ul><p><a href="../">返回今日推荐</a></p>',
        encoding="utf-8",
    )


def main() -> None:
    payload = read_json(DATA / "daily.json", {})
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "index.html").write_text(render(payload), encoding="utf-8")
    (OUT / "data.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    build_archive()


if __name__ == "__main__":
    main()
