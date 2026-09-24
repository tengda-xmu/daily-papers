"""Versioned PDF annotations; coordinates refer to the displayed, rotated page."""
from pathlib import Path
from typing import Annotated, Literal
import json

import pymupdf as fitz
from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

Coordinate = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Point = tuple[Coordinate, Coordinate]
Rect = tuple[Coordinate, Coordinate, Coordinate, Coordinate]


class Annotation(BaseModel):
    id: str = Field(pattern=r'^[a-f0-9-]{36}$')
    page: int = Field(ge=1, le=300)
    kind: Literal['highlight', 'underline', 'strikeout', 'ink', 'line', 'note']
    color: Literal['yellow', 'blue', 'red', 'green'] = 'yellow'
    rects: list[Rect] = Field(default_factory=list, max_length=200)
    points: list[Point] = Field(default_factory=list, max_length=2000)
    text: str = Field(default='', max_length=12000)
    note: str = Field(default='', max_length=4000)

    @model_validator(mode='after')
    def geometry(self):
        if any(r[0] >= r[2] or r[1] >= r[3] for r in self.rects):
            raise ValueError('无效的标记区域。')
        if self.kind in ('highlight', 'underline', 'strikeout') and not self.rects:
            raise ValueError('请先选择原文文字。')
        needed = {'line':2, 'ink':2, 'note':1}.get(self.kind, 0)
        if needed and len(self.points) < needed:
            raise ValueError('标记缺少位置。')
        return self


class AnnotationSet(BaseModel):
    document_hash: str = Field(pattern=r'^[a-f0-9]{16}$')
    revision: int = Field(ge=0)
    items: list[Annotation] = Field(max_length=500)

    @model_validator(mode='after')
    def unique_ids(self):
        if len({a.id for a in self.items}) != len(self.items):
            raise ValueError('标记编号不能重复。')
        if sum(len(a.points) + 4 * len(a.rects) for a in self.items) > 30000:
            raise ValueError('标记过多，请导出当前批注后分批整理。')
        return self


COLORS = {'yellow':(1, .8, .12), 'blue':(.12, .48, .9), 'red':(.86, .19, .16), 'green':(.14, .63, .32)}


def current_pdf(store, paper_id, version):
    store.paper(paper_id)
    doc = store.document(paper_id)
    if not doc or doc['kind'] != 'pdf':
        raise HTTPException(409, '请先上传或获取论文 PDF。')
    if not version or version != doc['hash']:
        raise HTTPException(409, '原文已更换，请重新打开阅读区后再操作。')
    directory = store.directory(paper_id)
    path = (directory / doc['file']).resolve()
    if path.parent != directory.resolve() or not path.is_file():
        raise HTTPException(404, '原 PDF 文件不存在，请重新上传。')
    return doc, path


def read_annotations(store, paper_id, version):
    with store.connect() as db:
        row = db.execute('SELECT revision,items FROM annotations WHERE paper=? AND document_hash=?', (paper_id, version)).fetchone()
    return {'document_hash':version, 'revision':row['revision'] if row else 0, 'items':json.loads(row['items']) if row else []}


def save_annotations(store, paper_id, data):
    doc, _ = current_pdf(store, paper_id, data.document_hash)
    if any(item.page > doc['page_count'] for item in data.items):
        raise HTTPException(400, '标记页码超出 PDF 范围。')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT revision FROM annotations WHERE paper=? AND document_hash=?', (paper_id, data.document_hash)).fetchone()
        revision = row['revision'] if row else 0
        if revision != data.revision:
            raise HTTPException(409, '批注已在其他页面修改。请先导出本页批注备份，再重新载入，避免覆盖。')
        db.execute('INSERT INTO annotations VALUES(?,?,?,?) ON CONFLICT(paper,document_hash) DO UPDATE SET revision=excluded.revision,items=excluded.items',
                   (paper_id, data.document_hash, revision+1, json.dumps([a.model_dump() for a in data.items], ensure_ascii=False)))
    return {'revision':revision+1}


def export_annotated_pdf(path: Path, items):
    with fitz.open(path) as pdf:
        for raw in items:
            item = Annotation.model_validate(raw)
            page = pdf[item.page-1]
            def point(p):
                return fitz.Point(p[0] * page.rect.width, p[1] * page.rect.height) * page.derotation_matrix
            quads = [fitz.Quad(point((r[0],r[1])), point((r[2],r[1])), point((r[0],r[3])), point((r[2],r[3]))) for r in item.rects]
            points = [point(p) for p in item.points]
            if item.kind in ('highlight', 'underline', 'strikeout'):
                annotation = getattr(page, 'add_'+item.kind+'_annot')(quads)
            elif item.kind == 'ink':
                annotation = page.add_ink_annot([[tuple(p) for p in points]])
            elif item.kind == 'line':
                annotation = page.add_line_annot(points[0], points[-1])
            else:
                annotation = page.add_text_annot(points[0], item.note or item.text or '笔记')
            annotation.set_colors(stroke=COLORS[item.color])
            if item.kind in ('ink', 'line'):
                annotation.set_border(width=1.4)
            annotation.set_info(title='论文阅读笔记', content=item.note or item.text)
            annotation.update(opacity=.35 if item.kind == 'highlight' else 1)
        return pdf.tobytes(garbage=3, deflate=True)
