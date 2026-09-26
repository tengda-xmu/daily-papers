"""Extract original figures from private PDFs without publishing the uploaded files."""
import asyncio
import hashlib
import json
import threading
import time
from urllib.parse import urlsplit

from src.paper_identity import paper_doi
from src.paper_sources import matches
from tools.collect_figures import Unavailable, image_info, pdf_figure, read, write


class LocalFigures:
    VERSION = 1

    def __init__(self, store, busy=lambda: False):
        self.store, self.busy = store, busy
        self.directory = store.runtime / 'original-figures'
        self.lock = threading.Lock()
        self.runner = None

    def ensure(self, identifier):
        paper = self.store.paper(identifier)
        document = self.store.document(identifier)
        if not document or document.get('kind') != 'pdf':
            return {'state': 'missing_pdf'}
        source_hash = document['hash']
        # Resolve through the library's checked paths; original PDFs only.
        descriptor, source = self.store.library.resolve(identifier, source_hash)
        if descriptor.get('view') != 'original':
            return {'state': 'missing_pdf'}
        identity = hashlib.sha256(json.dumps([paper.get('title'), paper_doi(paper)], ensure_ascii=False).encode()).hexdigest()
        stem = identifier + '-' + source_hash
        path = self.directory / (stem + '.json')
        image = self.directory / (stem + '.png')
        with self.lock:
            cached = read(path)
            if not isinstance(cached, dict):
                cached = {}
            if (cached.get('cache_version') == self.VERSION and cached.get('identity') == identity
                    and (cached.get('state') != 'unavailable' or time.time() - cached.get('checked_at', 0) < 86400)
                    and (cached.get('state') != 'ready' or image.is_file())):
                return cached
            result = {'cache_version': self.VERSION, 'identity': identity, 'source_hash': source_hash,
                      'visibility': 'local', 'state': 'unavailable', 'checked_at': time.time()}
            try:
                import fitz
                data = source.read_bytes()
                if hashlib.sha256(data).hexdigest()[:16] != source_hash:
                    raise Unavailable('source_changed')
                with fitz.open(stream=data, filetype='pdf') as pdf:
                    text = '\n'.join(pdf[i].get_text() for i in range(min(len(pdf), 2)))
                if not matches(text, {**paper, 'doi': paper_doi(paper)}):
                    raise Unavailable('identity_unconfirmed')
                url = paper.get('landing_url') or paper.get('url') or ''
                parsed = urlsplit(url)
                if parsed.scheme != 'https' or not parsed.netloc or parsed.username:
                    url = 'https://doi.org/' + paper_doi(paper) if paper_doi(paper) else ''
                authors = paper.get('authors') or []
                credit = ', '.join(map(str, authors[:3])) if isinstance(authors, list) else str(authors)
                metadata, extracted = pdf_figure(paper_doi(paper), url, {'credit': credit}, data)
                width, height, _ = image_info(extracted)
                self.directory.mkdir(parents=True, exist_ok=True)
                temporary = image.with_suffix('.tmp')
                temporary.write_bytes(extracted)
                temporary.replace(image)
                result.update(state='ready', figure_label=metadata['figure_label'],
                              caption=metadata['caption'], pdf_page=metadata['pdf_page'],
                              region=metadata['region'], title='原文代表图', credit=credit,
                              source_url=url, width=width, height=height,
                              sha256=hashlib.sha256(extracted).hexdigest())
            except Unavailable as error:
                result['state'] = error.state
            except Exception:
                # Extraction failure never cancels a successful PDF upload.
                result['state'] = 'unavailable'
            write(path, result)
            return result

    def descriptor(self, identifier):
        value = self.ensure(identifier)
        return {k: v for k, v in value.items() if k not in ('cache_version', 'identity', 'region')}

    def image(self, identifier, version):
        from fastapi import HTTPException
        value = self.ensure(identifier)
        if value.get('state') != 'ready' or version != value.get('source_hash'):
            raise HTTPException(409, '原文已更换或原图尚未提取，请刷新后重试。')
        return self.directory / (identifier + '-' + version + '.png')

    def remove(self, identifier):
        self.store.paper(identifier)  # validates the identifier before forming any paths
        with self.lock:
            for path in self.directory.glob(identifier + '-*'):
                if path.is_file():
                    path.unlink()

    async def run(self):
        while True:
            if not self.busy():
                with self.store.connect() as db:
                    identifiers = [row[0] for row in db.execute('SELECT id FROM papers WHERE document IS NOT NULL')]
                for identifier in identifiers:
                    if self.busy():
                        break
                    try:
                        await asyncio.to_thread(self.ensure, identifier)
                    except Exception:
                        continue
            await asyncio.sleep(60)

    def start(self):
        self.runner = asyncio.create_task(self.run())

    async def close(self):
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
