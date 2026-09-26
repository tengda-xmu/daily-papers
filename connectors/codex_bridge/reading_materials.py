"""Private, resumable source acquisition for automatic paper reading."""
import hashlib
import json
import re
from pathlib import Path
import time

import requests

from src.editions import read, write
from src.paper_sources import PublicFetcher, discover, inspect_html, inspect_xml, matches, clean_abstract
from .documents import parse_pdf


def local_signature(documents):
    return '|'.join(sorted(str(doc.get('hash', '')) for doc, path in documents if Path(path).is_file()))


def bundle(paper, document, directory, *, abstract='', url=''):
    if document:
        source = document.get('url') or paper.get('landing_url') or ('https://doi.org/' + paper.get('doi', ''))
        content = '\n'.join(p['text'] for p in document['pages'])
        version = hashlib.sha256((document['hash'] + content).encode()).hexdigest()
        return {'basis': 'full_text', 'version': version, 'document': document,
                'directory': str(directory), 'source_url': source, 'text': content,
                'checked_at': time.time()}
    if abstract:
        return {'basis': 'abstract', 'version': hashlib.sha256(abstract.encode()).hexdigest(),
                'source_url': url, 'text': abstract, 'checked_at': time.time()}
    return {'basis': 'missing', 'checked_at': time.time()}


class MaterialResolver:
    def __init__(self, runtime, *, fetch=None, documents=None):
        self.root = Path(runtime) / 'reading-materials'
        self.fetch = fetch or PublicFetcher()
        self.documents = documents or (lambda identifier: [])

    def signature(self, identifier):
        return local_signature(self.documents(identifier))

    def __call__(self, paper, *, force=False):
        if not re.fullmatch(r'[a-f0-9]{12}', str(paper.get('id', ''))):
            raise ValueError('Invalid paper identifier')
        directory = self.root / paper['id']
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'material.json'
        old = read(path)
        signature = self.signature(paper['id'])
        if (not force and old and old.get('local_signature') == signature
                and (old.get('basis') == 'full_text' or time.time() - old.get('checked_at', 0) < 86400)):
            if old.get('basis') != 'full_text' or old['document']['kind'] != 'pdf' or (directory / old['document']['file']).is_file():
                return old
        for doc, source in self.documents(paper['id']):
            if doc.get('view', 'original') != 'original' or not Path(source).is_file():
                continue
            try:
                result = self.pdf(Path(source).read_bytes(), directory, paper, local=True)
                return self.save(path, bundle(paper, result, directory), signature)
            except (OSError, ValueError):
                continue
        discovered = discover(paper, self.fetch)
        abstract, abstract_url = discovered['abstract'], discovered['abstract_url']
        urls = sorted(discovered['urls'], key=lambda url: not re.search(r'(?i)(\.pdf(?:[?#]|$)|/pdf(?:[/?#]|$))', url))
        visited, failures, full_html = set(), [], None
        while urls and len(visited) < 8:
            url = urls.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                body, final = self.fetch(url)
                if body.startswith(b'%PDF-'):
                    document = self.pdf(body, directory, paper)
                    document['url'] = final
                    return self.save(path, bundle(paper, document, directory), signature)
                if body.lstrip().startswith(b'<?xml') or b'<full-text-retrieval-response' in body[:1000]:
                    found, document = inspect_xml(body, final, paper)
                    links = []
                else:
                    found, document, links = inspect_html(body, final, paper)
                if found:
                    abstract, abstract_url = found, final
                if document:
                    full_html = full_html or document
                urls[:0] = [link for link in links[:2] if link not in visited]
            except requests.HTTPError as exc:
                failures.append('restricted' if exc.response is not None and exc.response.status_code in (401, 403, 429) else 'not_found')
            except (OSError, requests.RequestException):
                failures.append('network')
            except (ValueError, KeyError):
                failures.append('unverified')
                continue
        if full_html:
            return self.save(path, bundle(paper, full_html, directory), signature)
        # Known literature APIs return abstracts; web-search snippets never qualify.
        if not abstract and paper.get('source') in ('Crossref', 'OpenAlex', 'PubMed', 'arXiv', 'Semantic Scholar', 'Nature Portfolio（CNS）'):
            abstract = clean_abstract(paper.get('abstract'))
            abstract_url = paper.get('landing_url') or ('https://doi.org/' + discovered['doi'])
        if not abstract and old.get('basis') == 'abstract':
            abstract, abstract_url = old['text'], old['source_url']
        value = bundle(paper, None, directory, abstract=abstract, url=abstract_url)
        value['reason_code'] = 'restricted' if 'restricted' in failures else 'network' if 'network' in failures else 'not_found'
        value['reason'] = '全文暂未取得，将自动重试。' if abstract else '暂未取得可核实的完整摘要或全文，将自动重试。'
        if value['reason_code'] == 'restricted':
            value['reason'] = '部分来源限制自动访问，资料尚未取全，将自动重试。'
        return self.save(path, value, signature)

    @staticmethod
    def save(path, value, signature):
        value['local_signature'] = signature
        write(path, value)
        return value

    @staticmethod
    def pdf(body, directory, paper, local=False):
        import fitz
        with fitz.open(stream=body, filetype='pdf') as pdf:
            text = '\n'.join(page.get_text() for page in list(pdf)[:2])
            # An explicitly assigned local scan can be checked visually by Codex.
            if not matches(text, paper) and not (local and len(text.strip()) < 120):
                raise ValueError('PDF identity mismatch')
        document = parse_pdf(body, directory, '原文 PDF')
        if sum(len(p['text']) for p in document['pages']) < 6000 and document['page_count'] < 3:
            raise ValueError('PDF is only a short excerpt')
        document['identity_checked'] = matches(text, paper)
        return document
