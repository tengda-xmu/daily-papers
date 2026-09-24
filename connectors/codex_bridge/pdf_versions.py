"""Expose saved PDF versions independently of the selected source or conversation."""
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
    messages = store.history(paper_id, all_versions=True)
    versions = []
    for doc in store.library.documents(paper_id):
        descriptor = {k:v for k,v in doc.items() if k not in ('pages', 'file', 'artifact')}
        name = doc.get('artifact', {}).get('filename')
        ids = [r['id'] for r in reversed(messages) if name and r.get('artifact', {}).get('filename') == name]
        descriptor.update(message_id=ids[0] if ids else 0, message_ids=ids)
        versions.append(descriptor)
    return versions


def translated_pdf(store, paper_id, version):
    return store.library.resolve(paper_id, version)
