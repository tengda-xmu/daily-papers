"""Only expose completed PDF artifacts belonging to the selected paper/source."""
import hashlib
import re

from fastapi import HTTPException

from .pdf_translation import bilingual_file, source_path


def artifact_path(store, paper_id, artifact, view='translated'):
    directory = store.directory(paper_id)
    name = artifact.get('filename', '')
    if not re.fullmatch(r'layout-[a-f0-9]{24}\.pdf', name) or not (directory / name).is_file():
        raise HTTPException(409, '译文 PDF 文件尚未生成或已不存在，请重新开始翻译。')
    if view == 'bilingual':
        source = source_path({'kind':'pdf', 'file':artifact.get('source_hash', '') + '.pdf'}, directory)
        return bilingual_file(source, directory, artifact)
    return directory / name


def pdf_versions(store, paper_id):
    doc = store.document(paper_id)
    if not doc or doc.get('kind') != 'pdf':
        return []
    versions = [{**{k:v for k,v in doc.items() if k not in ('pages', 'file')},
                 'view':'original', 'source_hash':doc['hash'], 'label':'原文 PDF', 'message_id':0}]
    seen = {}
    directory = store.directory(paper_id)
    for row in reversed(store.history(paper_id, all_versions=True)):
        artifact = row.get('artifact') or {}
        name = artifact.get('filename', '')
        if (row['status'] != 'completed' or artifact.get('kind') != 'layout-pdf'
                or artifact.get('source_hash') != doc['hash']
                or not re.fullmatch(r'layout-[a-f0-9]{24}\.pdf', name) or not (directory / name).is_file()):
            continue
        if name in seen:
            for descriptor in seen[name]:
                descriptor['message_ids'].append(row['id'])
            continue
        seen[name] = []
        for view in ('translated', 'bilingual'):
            identity = hashlib.sha256(f'{doc["hash"]}:{name}:{view}'.encode()).hexdigest()[:16]
            label = ('中文译文 PDF' if artifact.get('target') != 'en' else '英文译文 PDF') if view == 'translated' else '中英对照 PDF'
            descriptor = {'kind':'pdf', 'hash':identity, 'source_hash':doc['hash'], 'view':view,
                             'name':label, 'label':label, 'page_count':doc['page_count'] * (2 if view == 'bilingual' else 1),
                             'message_id':row['id'], 'message_ids':[row['id']], 'created':row['created'], 'model':row.get('model', '')}
            versions.append(descriptor)
            seen[name].append(descriptor)
    return versions


def translated_pdf(store, paper_id, version):
    descriptor = next((v for v in pdf_versions(store, paper_id) if v['hash'] == version and v['view'] != 'original'), None)
    if not descriptor:
        raise HTTPException(409, 'PDF 版本已更换或不存在，请重新打开阅读区。')
    row = next(r for r in store.history(paper_id, all_versions=True) if r['id'] == descriptor['message_id'])
    return descriptor, artifact_path(store, paper_id, row['artifact'], descriptor['view'])
