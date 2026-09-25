"""Explicitly labelled editorial diagrams, based on reviewed public evidence.

These are separate from publisher images and never generated from arbitrary
titles at build time. An available original always takes precedence.
"""
from hashlib import sha256
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from src.figures import read_catalog, ROOT
from src.models import normalize_doi


def guides():
    return read_catalog(ROOT / 'data/curated/figure-guides.json').get('entries', {})


def paper_doi(paper):
    doi = normalize_doi(paper.get('doi', ''))
    if doi:
        return doi
    # Verified aliases attach illustrations without changing stable paper IDs
    # or rewriting the original recommendation snapshots.
    title = ' '.join(str(paper.get('title', '')).casefold().split())
    for identifier, row in guides().items():
        if (paper.get('id') in row.get('paper_ids', []) and title
                and title == ' '.join(row.get('paper_title', '').casefold().split())):
            return identifier
    return ''


def guide_for(paper):
    doi = paper_doi(paper)
    row = guides().get(doi)
    if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k].strip()
            for k in ('title', 'caption', 'basis', 'source_url', 'verified_at')):
        return None
    url = urlsplit(row['source_url'])
    if url.scheme != 'https' or not url.hostname:
        return None
    steps = row.get('steps')
    if (not isinstance(steps, list) or len(steps) != 4
            or any(not isinstance(s, list) or len(s) != 3 or any(not isinstance(t, str) or not t for t in s) for s in steps)):
        return None
    return {**row, 'image_path': 'assets/figures/guide-' + sha256(doi.encode()).hexdigest()[:16] + '.svg',
            'width': 680, 'height': 430}


def svg(guide):
    topics = guide.get('mode') == 'topics'
    label = '主题示意' if topics else '方法示意'
    # Keep the non-original label inside the image so downloaded / enlarged
    # copies cannot lose their provenance. Text is escaped, never SVG markup.
    parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="680" height="430" viewBox="0 0 680 430" role="img" aria-labelledby="title description">
<title id="title">{escape(guide['title'])} · {label}，非原文图</title>
<desc id="description">{escape(guide['caption'])}</desc>
<rect width="680" height="430" fill="#ffffff"/>
<g font-family="Microsoft YaHei, PingFang SC, sans-serif">
<text x="24" y="32" fill="#153e64" font-size="20" font-weight="700">{label} · 非原文图</text>
<text x="24" y="61" fill="#576575" font-size="17">依据：{escape(guide['basis'])}</text>
<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 Z" fill="#527995"/></marker></defs>''']
    if not topics:
        parts.append('<g fill="none" stroke="#527995" stroke-width="2.5" marker-end="url(#arrow)"><path d="M310 145 H367"/><path d="M520 208 V253"/><path d="M370 321 H313"/></g>')
    positions = [(24, 84), (370, 84), (370, 260), (24, 260)]
    if topics:
        positions = [(24, 84), (370, 84), (24, 260), (370, 260)]
    for i, ((x, y), (role, heading, detail)) in enumerate(zip(positions, guide['steps'])):
        fill = '#edf5fa' if i < 2 else '#edf6f3'
        number = '' if topics else f'{i+1:02d} / '
        parts.append(f'''<rect x="{x}" y="{y}" width="286" height="124" rx="7" fill="{fill}" stroke="#c5d6e1"/>
<text x="{x+16}" y="{y+29}" fill="#577186" font-size="16">{number}{escape(role)}</text>
<text x="{x+16}" y="{y+66}" fill="#153e64" font-size="22" font-weight="700">{escape(heading)}</text>
<text x="{x+16}" y="{y+98}" fill="#45545d" font-size="16">{escape(detail)}</text>''')
    parts.append('<text x="24" y="416" fill="#61717b" font-size="14">本站依据公开资料整理 · 不包含原文实验图或性能曲线</text></g></svg>')
    return '\n'.join(parts)


def build_guides(output):
    output = Path(output)
    for doi in guides():
        guide = guide_for({'doi': doi})
        if guide:
            path = output / guide['image_path']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(svg(guide), encoding='utf-8')
