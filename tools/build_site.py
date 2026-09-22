"""Build the standalone static GitHub Pages site from ``data/daily.json``."""
from __future__ import annotations

import html
import json
import shutil
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
    title = esc(paper.get("title", "未命名论文"))
    url = esc(paper.get("landing_url") or paper.get("url") or
              (f"https://doi.org/{paper['doi']}" if paper.get("doi") else "#"))
    source = esc(paper.get("source", "unknown"))
    venue = esc(paper.get("venue", ""))
    venue_group = esc(paper.get("venue_group", ""))
    authors = paper.get("authors", [])
    authors_text = ", ".join(authors) if isinstance(authors, list) else str(authors)
    topics = paper.get("topic_tags") or []
    if isinstance(topics, str):
        topics = [topics]
    tags = " ".join(f'<span class="tag">{esc(topic)}</span>' for topic in topics)
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
    <article class="paper" data-source="{source}" data-topics="{esc(" ".join(topics))}" data-venue="{venue_group}" data-search="{esc((paper.get("title", "") + " " + authors_text + " " + paper.get("venue", "") + " " + paper.get("summary", "")).casefold())}">
      <div class="meta"><span class="source">{source}</span><span>{esc(paper.get("published_at", ""))}</span><span>{esc(tier)}</span></div>
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
        rows.append(f'<span class="status {cls}"><b>{esc(source)}</b>：{esc(state)} {esc(detail)} {esc(message)}</span>')
    return "".join(rows) or '<span class="status warn">暂无运行状态</span>'


def render(payload: dict) -> str:
    core = payload.get("core", [])
    extended = payload.get("extended", [])
    all_papers = core + extended
    sources = sorted({"Elsevier", "Google Scholar", "ResearchGate", "微信公众号"} |
                     {str(p.get("source", "unknown")) for p in all_papers})
    topics = sorted({topic for p in all_papers for topic in (p.get("topic_tags") or [])})
    venue_groups = sorted({"CNS 正刊", "CNS 子刊"} |
                          {str(p.get("venue_group", "")) for p in all_papers if p.get("venue_group")})
    source_options = "".join(f'<option value="{esc(source)}">{esc(source)}</option>' for source in sources)
    topic_options = "".join(f'<option value="{esc(topic)}">{esc(topic)}</option>' for topic in topics)
    venue_options = "".join(f'<option value="{esc(venue)}">{esc(venue)}</option>' for venue in venue_groups)
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
.subtitle,.meta,.authors,.venue,.status,.minor-link{{color:var(--muted);font-size:13px}}.coverage{{margin:16px 0;padding:13px 16px;background:#fff9e8;border:1px solid #f1d58a;border-radius:10px;color:#6d4d00;line-height:1.9;font-size:14px}}.coverage b{{color:#8a5d00}}.statusbar{{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}}.status{{background:#eef1f6;border-radius:6px;padding:5px 8px}}.status.ok{{color:#146b3a;background:#e9f8ef}}.status.warn{{color:#8b5e00;background:#fff5d6}}
.controls{{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}}input,select{{padding:10px;border:1px solid #ccd3e0;border-radius:8px;background:#fff;min-width:190px}}.paper{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin:12px 0;box-shadow:0 2px 9px #18284a0d}}.paper[hidden]{{display:none}}.meta{{display:flex;gap:10px;align-items:center}}.source{{background:#e8f1ff;color:#1757a6;border-radius:99px;padding:4px 8px}}.venue{{margin:4px 0 10px}}.venue-group{{color:var(--gold);background:var(--goldbg);border-radius:99px;padding:3px 8px;margin-left:6px;font-size:12px;font-weight:600}}.tag{{display:inline-block;font-size:12px;background:#eef0f4;border-radius:99px;padding:3px 8px;margin:3px 5px 3px 0}}.recommendation{{color:#374151;background:#f8fafc;border-left:3px solid #9db9e8;padding:8px 10px}}details{{margin-top:13px;border-top:1px solid var(--line);padding-top:10px}}summary{{cursor:pointer;color:var(--blue);font-weight:600}}.empty{{padding:24px;text-align:center;color:var(--muted)}}.minor-link{{margin-left:8px}}footer{{margin-top:35px;padding-top:15px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}}
</style></head><body>
<header><h1>每日论文推荐</h1><div class="subtitle">面向 AI 智能运维、结构生成式设计、结构疲劳与可靠性设计 · 覆盖 CNS 正刊与子刊 · 更新时间：{generated}</div></header>
<section class="coverage" aria-label="CNS专项检索范围"><b>CNS 正刊专项检索：</b>Nature、Science、Cell<br><b>CNS 子刊专项检索：</b>Nature Communications、Science Advances、Cell Reports、Nature Machine Intelligence、Nature Computational Science、Communications Engineering、Science Robotics、Cell Systems、Cell Reports Physical Science、Nature Biomedical Engineering、Nature Electronics、Nature Materials、npj Computational Materials、npj Artificial Intelligence、iScience</section>
<div class="statusbar">{status_rows(payload.get("source_status", {}))}</div>
<div class="controls"><input id="search" placeholder="搜索标题、作者、期刊或摘要"><select id="source"><option value="">全部来源</option>{source_options}</select><select id="topic"><option value="">全部主题</option>{topic_options}</select><select id="venue"><option value="">全部期刊组</option>{venue_options}</select></div>
<section><h2>核心推荐（{len(core)}）</h2><div id="core">{core_html}</div></section>
<section><h2>扩展阅读（{len(extended)}）</h2><div id="extended">{extended_html}</div></section>
<footer>论文标题、摘要和元数据来自原始来源；请通过原文链接访问完整内容。<a href="archive/">查看历史归档</a></footer>
<script>const q=document.querySelector('#search'),s=document.querySelector('#source'),t=document.querySelector('#topic'),v=document.querySelector('#venue');function filter(){{const text=q.value.toLowerCase(),source=s.value,topic=t.value,venue=v.value;document.querySelectorAll('.paper').forEach(x=>{{const okText=!text||x.dataset.search.includes(text),okSource=!source||x.dataset.source===source,okTopic=!topic||x.dataset.topics.includes(topic),okVenue=!venue||x.dataset.venue===venue;x.hidden=!(okText&&okSource&&okTopic&&okVenue)}})}}q.oninput=filter;s.onchange=filter;t.onchange=filter;v.onchange=filter;</script>
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
