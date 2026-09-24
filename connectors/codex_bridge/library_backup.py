"""Versioned private-library backups: explicit data allowlist, no auth or threads."""
import hashlib
import json
import math
import re
import tempfile
import time
import zipfile
from pathlib import Path

from .annotations import AnnotationSet
from .library import ID, HASH, FILE, ReadingPosition

MAX_ARCHIVE = 1024 * 1024 * 1024
MAX_EXPANDED = 2 * MAX_ARCHIVE
TABLES = {
    'library_papers': ('id', 'metadata', 'rating', 'revision', 'updated', 'last_version'),
    'library_documents': ('paper', 'hash', 'content'),
    'library_reading': ('paper', 'hash', 'content'),
    'annotations': ('paper', 'document_hash', 'revision', 'items'),
    'translations': ('paper', 'cache_key', 'content'),
}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def export_library(store):
    folder = store.runtime / 'library-transfers'
    folder.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=folder, suffix='.zip', delete=False) as output:
        path = Path(output.name)
    try:
        with store.connect() as db:
            db.execute('BEGIN')
            tables = {name: [dict(r) for r in db.execute(f'SELECT {",".join(columns)} FROM {name}')] for name, columns in TABLES.items()}
        known = {r['id'] for r in tables['library_papers']}
        tables = {name: [r for r in rows if r.get('paper', r.get('id')) in known] for name, rows in tables.items()}
        files, missing = {}, []
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for row in tables['library_documents']:
                doc = json.loads(row['content'])
                source = store.library.path(row['paper'], doc['file'])
                key = row['paper'] + '/' + doc['file']
                if key in files:
                    continue
                if not source.is_file():
                    missing.append(key)
                    continue
                files[key] = {'sha256': digest(source), 'size': source.stat().st_size}
                archive.write(source, 'files/' + key)
            manifest = {'format': 'daily-papers-library', 'version': 1, 'created': time.time(),
                        'tables': tables, 'files': files, 'missing': missing}
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def inspect_backup(store, path):
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.infolist()
            if len(info) > 20000 or len({i.filename for i in info}) != len(info) or sum(i.file_size for i in info) > MAX_EXPANDED:
                raise ValueError('备份文件数量或解压大小超过限制。')
            if archive.getinfo('manifest.json').file_size > 64 * 1024 * 1024:
                raise ValueError('备份目录过大。')
            manifest = json.loads(archive.read('manifest.json'))
            if manifest.get('format') != 'daily-papers-library' or manifest.get('version') != 1:
                raise ValueError('不支持此备份格式。')
            if set(manifest['tables']) != set(TABLES):
                raise ValueError('备份数据表不完整。')
            tables = manifest['tables']
            paper_ids = set()
            for name, columns in TABLES.items():
                rows = tables[name]
                if not isinstance(rows, list) or len(rows) > 100000:
                    raise ValueError('备份记录过多。')
                for row in rows:
                    if set(row) != set(columns) or not ID.fullmatch(row.get('paper', row.get('id', ''))):
                        raise ValueError('备份记录格式无效。')
            for row in tables['library_papers']:
                metadata = json.loads(row['metadata'])
                if metadata.get('id') != row['id'] or not isinstance(metadata.get('title', ''), str):
                    raise ValueError('论文信息无效。')
                if type(row['rating']) is not int or not 0 <= row['rating'] <= 5 or type(row['revision']) is not int or row['revision'] < 0:
                    raise ValueError('星级记录无效。')
                if not math.isfinite(row['updated']) or (row['last_version'] and not HASH.fullmatch(row['last_version'])):
                    raise ValueError('阅读记录无效。')
                paper_ids.add(row['id'])
            documents, referenced = {}, set()
            for row in tables['library_documents']:
                doc = json.loads(row['content'])
                if row['paper'] not in paper_ids or not HASH.fullmatch(row['hash']) or doc['hash'] != row['hash']:
                    raise ValueError('PDF 版本无效。')
                if not HASH.fullmatch(doc['source_hash']) or doc['view'] not in ('original', 'translated', 'bilingual') or doc['kind'] != 'pdf':
                    raise ValueError('PDF 类型无效。')
                if type(doc['page_count']) is not int or not 1 <= doc['page_count'] <= 600:
                    raise ValueError('PDF 页数无效。')
                store.library.path(row['paper'], doc['file'])
                documents[(row['paper'], row['hash'])] = doc
                referenced.add(row['paper'] + '/' + doc['file'])
            for name in ('annotations', 'library_reading'):
                for row in tables[name]:
                    version = row.get('hash', row.get('document_hash'))
                    if (row['paper'], version) not in documents:
                        raise ValueError('阅读记录缺少对应的 PDF。')
                    if name == 'annotations':
                        data = AnnotationSet(document_hash=version, revision=row['revision'], items=json.loads(row['items']))
                        if any(a.page > documents[(row['paper'], version)]['page_count'] for a in data.items):
                            raise ValueError('批注页码无效。')
                    else:
                        data = ReadingPosition.model_validate(json.loads(row['content']))
                        if data.version != version or data.page > documents[(row['paper'], version)]['page_count']:
                            raise ValueError('阅读位置无效。')
                        if data.zoom != 'fit' and not .25 <= float(data.zoom) <= 3:
                            raise ValueError('缩放值无效。')
            for row in tables['translations']:
                if row['paper'] not in paper_ids or not re.fullmatch(r'[a-f0-9]{64}', row['cache_key']):
                    raise ValueError('翻译缓存无效。')
                data = json.loads(row['content'])
                artifact = data.get('artifact')
                if artifact:
                    if not re.fullmatch(r'layout-[a-f0-9]{24}\.pdf', artifact.get('filename', '')) or not HASH.fullmatch(artifact.get('source_hash', '')):
                        raise ValueError('翻译文件无效。')
                    for field in ('filename', 'bilingual_filename'):
                        if artifact.get(field) and row['paper'] + '/' + artifact[field] not in referenced:
                            raise ValueError('翻译文件未登记。')
            expected = {'manifest.json', *('files/' + key for key in manifest['files'])}
            if {i.filename for i in info} != expected or not set(manifest['files']).issubset(referenced):
                raise ValueError('备份含有未登记的文件。')
            new_files = 0
            for key, entry in manifest['files'].items():
                pid, filename = key.split('/')
                destination = store.library.path(pid, filename)
                item = archive.getinfo('files/' + key)
                if item.file_size != entry['size']:
                    raise ValueError('备份文件大小不符。')
                with archive.open(item) as source:
                    checksum = hashlib.file_digest(source, 'sha256').hexdigest()
                if checksum != entry['sha256']:
                    raise ValueError('备份文件校验失败。')
                if destination.exists() and digest(destination) != checksum:
                    raise ValueError('本机同名 PDF 内容不同，已停止恢复以保留原文件。')
                if not destination.exists():
                    new_files += 1
            with store.connect() as db:
                existing = {r[0] for r in db.execute('SELECT id FROM library_papers')}
            return manifest, {'papers': len(paper_ids), 'new_papers': len(paper_ids - existing),
                              'files': len(manifest['files']), 'new_files': new_files,
                              'missing_files': len(referenced - set(manifest['files'])),
                              'annotations': len(tables['annotations']), 'existing_papers': len(paper_ids & existing)}
    except (KeyError, TypeError, zipfile.BadZipFile, json.JSONDecodeError, OverflowError) as exc:
        raise ValueError('备份损坏或格式无效。') from exc


def restore_library(store, path):
    manifest, summary = inspect_backup(store, path)
    created = []
    try:
        with zipfile.ZipFile(path) as archive:
            for key in manifest['files']:
                pid, filename = key.split('/')
                destination = store.library.path(pid, filename)
                if destination.exists():
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open('xb') as target, archive.open('files/' + key) as source:
                    created.append(destination)
                    while chunk := source.read(1024 * 1024):
                        target.write(chunk)
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for name, columns in TABLES.items():
                for row in manifest['tables'][name]:
                    db.execute(f'INSERT OR IGNORE INTO {name} ({",".join(columns)}) VALUES({",".join("?" for _ in columns)})', tuple(row[c] for c in columns))
            for row in manifest['tables']['library_documents']:
                doc = json.loads(row['content'])
                if doc['view'] == 'original':
                    db.execute('INSERT INTO papers(id,document) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET document=COALESCE(papers.document,excluded.document)',
                               (row['paper'], row['content']))
        return summary
    except Exception:
        for item in created:
            item.unlink(missing_ok=True)
        raise
