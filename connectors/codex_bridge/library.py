"""Private, durable paper library. Its lifetime is independent of conversations."""
import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing

from fastapi import HTTPException
from pydantic import BaseModel, Field

ID = re.compile(r'^[a-f0-9]{12}$')
HASH = re.compile(r'^[a-f0-9]{16}$')
FILE = re.compile(r'^(?:[a-f0-9]{16}|layout-[a-f0-9]{24}(?:-bilingual)?)\.pdf$')


class RatingChange(BaseModel):
    rating: int = Field(ge=0, le=5, strict=True)
    revision: int = Field(ge=0)


class ReadingPosition(BaseModel):
    version: str = Field(pattern=r'^[a-f0-9]{16}$')
    page: int = Field(ge=1, le=600)
    rx: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    ry: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    zoom: str = Field(default='fit', max_length=16)


class Library:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            existed = db.execute("SELECT 1 FROM sqlite_master WHERE name='library_papers'").fetchone()
            if not existed and db.execute('SELECT 1 FROM papers LIMIT 1').fetchone():
                backup = store.runtime / 'before-library.sqlite3'
                if not backup.exists():
                    with closing(sqlite3.connect(backup)) as copy:
                        db.backup(copy)
            db.executescript('''
                CREATE TABLE IF NOT EXISTS library_papers (
                    id TEXT PRIMARY KEY, metadata TEXT NOT NULL, rating INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL, last_version TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS library_documents (
                    paper TEXT NOT NULL, hash TEXT NOT NULL, content TEXT NOT NULL, PRIMARY KEY(paper,hash));
                CREATE TABLE IF NOT EXISTS library_reading (
                    paper TEXT NOT NULL, hash TEXT NOT NULL, content TEXT NOT NULL, PRIMARY KEY(paper,hash));
                CREATE TABLE IF NOT EXISTS library_migrations (name TEXT PRIMARY KEY);
            ''')

    def remember(self, paper_id):
        paper = self.store.paper(paper_id)
        with self.store.connect() as db:
            db.execute('INSERT INTO library_papers(id,metadata,updated) VALUES(?,?,?) '
                       'ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata',
                       (paper_id, json.dumps(paper, ensure_ascii=False), time.time()))
        return paper

    def metadata(self, paper_id):
        with self.store.connect() as db:
            row = db.execute('SELECT metadata FROM library_papers WHERE id=?', (paper_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def rating(self, paper_id):
        with self.store.connect() as db:
            row = db.execute('SELECT rating,revision FROM library_papers WHERE id=?', (paper_id,)).fetchone()
        return dict(row) if row else {'rating': 0, 'revision': 0}

    def rate(self, paper_id, change):
        self.remember(paper_id)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision FROM library_papers WHERE id=?', (paper_id,)).fetchone()
            if row['revision'] != change.revision:
                raise HTTPException(409, '星级已在其他页面修改，请刷新后再设置。')
            db.execute('UPDATE library_papers SET rating=?,revision=revision+1,updated=? WHERE id=?',
                       (change.rating, time.time(), paper_id))
        return self.rating(paper_id)

    def documents(self, paper_id):
        with self.store.connect() as db:
            rows = db.execute('SELECT content FROM library_documents WHERE paper=? ORDER BY rowid', (paper_id,)).fetchall()
        result = []
        for row in rows:
            doc = json.loads(row[0])
            doc['available'] = self.path(paper_id, doc['file']).is_file()
            result.append(doc)
        return result

    def path(self, paper_id, filename):
        if not ID.fullmatch(paper_id) or not FILE.fullmatch(filename):
            raise ValueError('无效的文献文件。')
        parent = (self.store.runtime / 'documents' / paper_id).resolve()
        path = (parent / filename).resolve()
        if path.parent != parent or parent.parent != (self.store.runtime / 'documents').resolve():
            raise ValueError('文献文件路径无效。')
        return path

    def put_document(self, paper_id, doc):
        self.remember(paper_id)
        self.path(paper_id, doc['file'])
        with self.store.connect() as db:
            db.execute('INSERT INTO library_documents VALUES(?,?,?) ON CONFLICT(paper,hash) DO UPDATE SET content=excluded.content',
                       (paper_id, doc['hash'], json.dumps(doc, ensure_ascii=False)))

    def source(self, paper_id, document):
        if document.get('kind') != 'pdf' or not HASH.fullmatch(document.get('hash', '')) or not FILE.fullmatch(document.get('file', '')):
            return  # Non-PDF documents continue using the existing document store.
        old = next((d for d in self.documents(paper_id) if d['hash'] == document['hash']), {})
        self.put_document(paper_id, {**document, 'view': 'original', 'source_hash': document['hash'],
                                    'label': '原文 PDF', 'created': old.get('created', time.time())})

    def artifact(self, paper_id, artifact, *, model='', instructions='', cache_key='', created=None):
        if artifact.get('kind') != 'layout-pdf':
            return
        source_hash, filename = artifact.get('source_hash', ''), artifact.get('filename', '')
        if not HASH.fullmatch(source_hash) or not re.fullmatch(r'layout-[a-f0-9]{24}\.pdf', filename):
            return
        old = {d['hash']: d for d in self.documents(paper_id)}
        for view in ('translated', 'bilingual'):
            name = filename if view == 'translated' else artifact.get('bilingual_filename')
            if not name:
                continue
            identity = hashlib.sha256(f'{source_hash}:{filename}:{view}'.encode()).hexdigest()[:16]
            label = ('中文译文 PDF' if artifact.get('target') != 'en' else '英文译文 PDF') if view == 'translated' else '中英对照 PDF'
            previous = old.get(identity, {})
            doc = {'kind': 'pdf', 'hash': identity, 'source_hash': source_hash, 'view': view,
                   'file': name, 'label': label, 'name': label, 'target': artifact.get('target', 'zh'),
                   'page_count': artifact['pages'] * (2 if view == 'bilingual' else 1),
                   'created': previous.get('created', created or time.time()),
                   'model': model or previous.get('model', ''), 'instructions': instructions or previous.get('instructions', ''),
                   'cache_key': cache_key or previous.get('cache_key', ''), 'artifact': artifact}
            self.put_document(paper_id, doc)
        with self.store.connect() as db:
            db.execute('UPDATE library_papers SET updated=? WHERE id=?', (time.time(), paper_id))

    def resolve(self, paper_id, version):
        doc = next((d for d in self.documents(paper_id) if d['hash'] == version), None)
        if not doc:
            raise HTTPException(409, '找不到此 PDF 版本，请重新载入文献。')
        if not doc['available']:
            raise HTTPException(404, 'PDF 文件已缺失，可从文献库备份恢复。')
        return doc, self.path(paper_id, doc['file'])

    def reading(self, paper_id):
        with self.store.connect() as db:
            row = db.execute('SELECT last_version FROM library_papers WHERE id=?', (paper_id,)).fetchone()
            positions = {r['hash']: json.loads(r['content']) for r in db.execute('SELECT * FROM library_reading WHERE paper=?', (paper_id,))}
        return {'last_version': row[0] if row else '', 'positions': positions}

    def save_reading(self, paper_id, position):
        doc, _ = self.resolve(paper_id, position.version)
        if position.page > doc['page_count']:
            raise ValueError('阅读页码超出范围。')
        if position.zoom != 'fit':
            try:
                if not .25 <= float(position.zoom) <= 3:
                    raise ValueError()
            except ValueError:
                raise ValueError('缩放范围应为 25%–300%。') from None
        data = {**position.model_dump(), 'updated': time.time()}
        with self.store.connect() as db:
            db.execute('INSERT INTO library_reading VALUES(?,?,?) ON CONFLICT(paper,hash) DO UPDATE SET content=excluded.content',
                       (paper_id, position.version, json.dumps(data)))
            db.execute('UPDATE library_papers SET last_version=?,updated=? WHERE id=?', (position.version, data['updated'], paper_id))
        return data

    def listing(self, *, q='', min_rating=0, translated=False, topic='', sort='recent', page=1, page_size=20):
        with self.store.connect() as db:
            papers = [dict(r) for r in db.execute('SELECT * FROM library_papers')]
            pending = set()
            for row in db.execute('SELECT paper,content FROM translations'):
                data = json.loads(row[1])
                if ('regions' in data and not data.get('artifact', {}).get('bilingual_filename')) or ('parts' in data and len(data['parts']) < data.get('total', 0)):
                    pending.add(row[0])
        rows, topics = [], set()
        for row in papers:
            meta = json.loads(row.pop('metadata'))
            documents = self.documents(row['id'])
            if not row['rating'] and not documents and row['id'] not in pending:
                continue
            directions = list(dict.fromkeys([*meta.get('topic_tags', []), *([meta['recommended_direction']] if meta.get('recommended_direction') else [])]))
            topics.update(directions)
            ready = any(d['view'] == 'translated' and d['available'] for d in documents)
            text = ' '.join(str(meta.get(k, '')) for k in ('title', 'title_zh', 'authors', 'doi', 'venue')).lower()
            if (q.lower() not in text or row['rating'] < min_rating or (translated and not ready) or (topic and topic not in directions)):
                continue
            rows.append({**meta, **row, 'topics': directions, 'has_translation': ready,
                         'incomplete_translation': row['id'] in pending,
                         'pdf_count': len(documents), 'missing_files': sum(not d['available'] for d in documents)})
        rows.sort(key=lambda r: (r['rating'] if sort == 'importance' else 0, r['updated'], r['id']), reverse=True)
        total = len(rows)
        page = min(max(1, page), max(1, (total + page_size - 1) // page_size))
        from src.research_directions import load_profile
        labels = {d['id']: d['name'] for d in load_profile(self.store.root)['directions']}
        return {'papers': rows[(page-1)*page_size:page*page_size], 'total': total, 'page': page,
                'page_size': page_size, 'topics': sorted(topics), 'topic_labels': labels}

    def migrate(self):
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM library_migrations WHERE name='initial'").fetchone():
                return
            papers = [dict(r) for r in db.execute('SELECT * FROM papers')]
        for paper in papers:
            pid = paper['id']
            try:
                self.remember(pid)
            except ValueError:
                with self.store.connect() as db:
                    db.execute('INSERT OR IGNORE INTO library_papers(id,metadata,updated) VALUES(?,?,?)',
                               (pid, json.dumps({'id': pid, 'title': '论文 ' + pid}), time.time()))
            if paper['document']:
                self.source(pid, json.loads(paper['document']))
            # Older original PDFs remain on disk even when the selected source changes.
            directory = self.store.runtime / 'documents' / pid
            for path in directory.glob('*.pdf'):
                if not re.fullmatch(r'[a-f0-9]{16}\.pdf', path.name):
                    continue
                if any(d['hash'] == path.stem for d in self.documents(pid)):
                    continue
                try:
                    import pymupdf as fitz
                    if hashlib.sha256(path.read_bytes()).hexdigest()[:16] != path.stem:
                        continue
                    with fitz.open(path) as pdf:
                        pages = [{'label': f'P{i+1}', 'text': p.get_text(), 'scan': len(p.get_text().strip()) < 60} for i, p in enumerate(pdf)]
                    self.source(pid, {'kind': 'pdf', 'hash': path.stem, 'file': path.name, 'name': '原文 PDF',
                                      'pages': pages, 'page_count': len(pages), 'scan_pages': sum(p['scan'] for p in pages)})
                except (ValueError, RuntimeError, OSError):
                    continue
            for message in self.store.history(pid, all_versions=True):
                self.artifact(pid, message.get('artifact') or {}, model=message.get('model', ''),
                              instructions=message.get('request', {}).get('message', ''), created=message['created'])
            with self.store.connect() as db:
                saved = db.execute('SELECT cache_key,content FROM translations WHERE paper=?', (pid,)).fetchall()
            for row in saved:
                value = json.loads(row['content'])
                self.artifact(pid, value.get('artifact', {}), cache_key=row['cache_key'],
                              model=value.get('signature', {}).get('model', ''), instructions=value.get('signature', {}).get('instructions', ''))
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO library_migrations VALUES('initial')")
