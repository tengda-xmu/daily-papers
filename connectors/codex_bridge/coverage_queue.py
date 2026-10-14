"""Independent, resumable image identity review using the local Codex client."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
import time

from PIL import Image

from src.coverage_evidence import (PRIVATE, PUBLIC, enqueue_review, extraction_prompt,
                                   load_reviews, match_evidence, public_review, review_key, save_review)
from src.public_sources import fetch, read, write
from src.wechat_metadata import article_url


class CoverageQueue:
    def __init__(self, root, runtime, client, generation_lock, *, fetcher=None, publisher=None):
        self.root, self.runtime = Path(root), Path(runtime) / 'coverage-review'
        self.client, self.lock = client, generation_lock
        self.fetcher, self.publisher = fetcher, publisher
        self.runner = self.active = None
        self.last_sync = self.last_publish = self.quiet_until = 0

    def sync(self):
        """Recover durable candidates produced in CI, not only local cache tasks."""
        from src.paper_coverage import selected_papers, parse_article, fetch_coverage
        from tools.recommendation_data import fetch as public_fetch
        get = self.fetcher or public_fetch
        papers = {p['id']: p for p in selected_papers(self.root)}
        coverage = read(self.root / 'data/paper-coverage.json')
        try:
            remote = get('paper-coverage.json')
            # Published candidates supplement local ones, preserving new local
            # input that may not have reached the deployment yet.
            combined = dict(coverage.get('papers', {}))
            for paper_id, row in remote.get('papers', {}).items():
                old = combined.get(paper_id, {})
                candidates = {c.get('url'): c for c in old.get('candidates', [])}
                candidates.update({c.get('url'): c for c in row.get('candidates', [])})
                combined[paper_id] = {**old, **row, 'candidates': list(candidates.values())}
            coverage = {'papers': combined}
            payload = get('data.json')
            papers.update({p['id']: p for p in payload.get('core', []) + payload.get('extended', [])})
            missing = set(combined) - set(papers)
            if missing:
                from src.editions import relative_path, selected
                manifest = get('editions/index.json')
                for entry in reversed(manifest.get('editions', [])):
                    cache = self.runtime / 'editions' / (entry['id'] + '.json')
                    payload = read(cache)
                    if not payload:
                        payload = get(relative_path(entry))
                        if payload.get('edition') != entry:
                            continue
                        write(cache, payload)
                    for paper in selected(payload):
                        papers.setdefault(paper['id'], paper)
                        missing.discard(paper['id'])
                    if not missing:
                        break
        except Exception:
            pass
        for paper_id, record in coverage.get('papers', {}).items():
            paper = papers.get(paper_id)
            if not paper:
                continue
            approved = {row['url'] for row in load_reviews(self.root, paper)}
            for candidate in record.get('candidates', []):
                url = article_url(candidate.get('url'))
                if not url or url in approved or candidate.get('reason') not in ('image_pending', 'pending_image', 'visual_pending', 'identity_unconfirmed'):
                    continue
                attempts = self.root / PRIVATE / 'discovery' / (hashlib.sha256(f'{paper_id}\n{url}'.encode()).hexdigest() + '.json')
                previous = read(attempts)
                if (previous.get('source_version') == candidate.get('source_version')
                        and previous.get('next_attempt', 0) > time.time()):
                    continue
                try:
                    body = fetch_coverage(url, {'hosts': ['mp.weixin.qq.com']})
                    article = parse_article(body, url)
                    if any(b.get('type') == 'image' for b in article.get('blocks', [])):
                        enqueue_review(self.root, paper, article)
                    write(attempts, {'source_version': candidate.get('source_version'), 'next_attempt': time.time() + 86400})
                except Exception:
                    # Access challenges remain pending; there is no attempt cap.
                    write(attempts, {'source_version': candidate.get('source_version'), 'next_attempt': time.time() + 86400})

    def next_task(self):
        for path in sorted((self.root / PRIVATE / 'tasks').glob('*.json')):
            task = read(path)
            if (task.get('state') in ('pending', 'retry', 'generating', 'unconfirmed', 'ready')
                    and task.get('next_attempt', 0) <= time.time()):
                return task
        return None

    def checkpoint(self, task, **changes):
        task.update(changes)
        write(self.root / PRIVATE / 'tasks' / (task['id'] + '.json'), task)

    def image_file(self, task, image):
        raw = fetch(image['remote_url'], {'hosts': ['mmbiz.qpic.cn', 'mmbiz.qlogo.cn']})
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in ('PNG', 'JPEG', 'WEBP') or source.width * source.height > 30_000_000:
                raise ValueError('Unsupported coverage image')
            extension = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}[source.format]
            source.verify()
        digest = hashlib.sha256(raw).hexdigest()
        directory = self.root / PRIVATE / 'images'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (digest + extension)
        if not path.exists():
            path.write_bytes(raw)
        return path, digest

    async def process(self, task):
        self.checkpoint(task, state='generating')
        try:
            images = []
            for image in task['article'].get('blocks', []):
                if read(self.root / PRIVATE / 'tasks' / (task['id'] + '.json')).get('state') == 'superseded':
                    return
                if image.get('type') != 'image' or image.get('reference_section'):
                    continue
                path, digest = await asyncio.to_thread(self.image_file, task, image)
                images.append((image, path, digest))
            # HTML alone is insufficient: qpic can serve changed image bytes at
            # the same URL. The discovery probe rechecks bytes, while the actual
            # visual review identity and receipt include their ordered hashes.
            material = {'html_version': task['article']['source_version'],
                        'images': [{'image': image, 'sha256': digest} for image, _, digest in images]}
            material_version = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
            key = review_key(task['paper']['id'], task['article']['url'], material_version)
            review_path = self.root / PRIVATE / 'materials' / (key + '.json')
            completed = read(review_path)
            if completed.get('state') in ('ready', 'unconfirmed'):
                self.checkpoint(task, state=completed['state'], material_version=material_version,
                                review_key=key, receipt_id=completed.get('receipt_id', ''),
                                next_attempt=time.time() + (604800 if completed['state'] == 'ready' else 86400))
                return
            write(review_path, {'id': key, 'source_version': material_version, 'material': material, 'state': 'pending'})
            proofs = []
            for image, path, digest in images:
                if read(self.root / PRIVATE / 'tasks' / (task['id'] + '.json')).get('state') == 'superseded':
                    return
                # Source version + image content + position/context prevents
                # stale transcriptions being reused for a different location.
                context_hash = hashlib.sha256(json.dumps(image, sort_keys=True).encode()).hexdigest()
                extraction_key = hashlib.sha256((digest + context_hash).encode()).hexdigest()
                checkpoint = self.root / PRIVATE / 'extractions' / key[:24] / (extraction_key[:32] + '.json')
                evidence = read(checkpoint)
                if not evidence:
                    async with self.lock:
                        await self.client.start()
                        thread = await self.client.thread(purpose='article')
                        output = ''
                        async for event in self.client.turn(thread, extraction_prompt(image), [path]):
                            if event['type'] == 'delta':
                                output += event.get('text', '')
                                if len(output) > 30000:
                                    raise ValueError('Coverage transcription too large')
                            elif event['type'] == 'completed' and event.get('status') != 'completed':
                                raise ValueError('Coverage transcription interrupted')
                        output = output.strip()
                        if output.startswith('```'):
                            output = output.split('\n', 1)[1].rsplit('```', 1)[0]
                        evidence = json.loads(output)
                        if not isinstance(evidence, dict):
                            raise ValueError('Invalid coverage transcription')
                        evidence.update(image_hash=digest, position=image.get('position'), source_version=material_version)
                        write(checkpoint, evidence)
                if read(self.root / PRIVATE / 'tasks' / (task['id'] + '.json')).get('state') == 'superseded':
                    return
                if match_evidence(task['paper'], evidence):
                    proofs.append(evidence)
            if proofs:
                receipt = save_review(self.root, task['paper'], {**task['article'], 'source_version': material_version}, proofs[0])
                write(review_path, {'id': key, 'source_version': material_version, 'material': material,
                                    'state': 'ready', 'receipt_id': receipt['id']})
                self.checkpoint(task, state='ready', material_version=material_version, review_key=key,
                                receipt_id=receipt['id'], next_attempt=time.time() + 604800)
            else:
                # Recheck bytes next day, retaining completed transcriptions if
                # neither HTML nor image content changed. This is not a negative.
                write(review_path, {'id': key, 'source_version': material_version, 'material': material, 'state': 'unconfirmed'})
                self.checkpoint(task, state='unconfirmed', material_version=material_version,
                                review_key=key, next_attempt=time.time() + 86400)
        except asyncio.CancelledError:
            self.checkpoint(task, state='pending')
            raise
        except Exception:
            self.checkpoint(task, state='retry', next_attempt=time.time() + 86400)

    def publish_ready(self):
        from tools.publish_reading import publish_values
        receipts = []
        state_path = self.root / PRIVATE / 'publication.json'
        published = read(state_path)
        for path in sorted((self.root / PUBLIC).glob('*.json')):
            try:
                value = public_review(read(path))
            except (ValueError, TypeError, AttributeError):
                continue
            digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
            if published.get(value['id']) != digest:
                receipts.append(value)
        if not receipts:
            return
        (self.publisher or publish_values)(self.root, receipts, 'coverage-reviews', 'id')
        for value in receipts:
            published[value['id']] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        write(state_path, published)

    async def run(self):
        while True:
            await asyncio.sleep(10)
            if self.lock.locked() or time.time() < self.quiet_until:
                continue
            if time.time() - self.last_sync >= 600:
                self.last_sync = time.time()
                try:
                    await asyncio.to_thread(self.sync)
                except Exception:
                    pass
            if time.time() - self.last_publish >= 300:
                self.last_publish = time.time()
                try:
                    await asyncio.to_thread(self.publish_ready)
                except Exception:
                    pass
            if self.lock.locked() or time.time() < self.quiet_until:
                continue
            task = self.next_task()
            if task:
                self.active = asyncio.create_task(self.process(task))
                try:
                    await self.active
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                finally:
                    self.active = None

    async def preempt(self):
        self.quiet_until = time.time() + 30
        if self.active:
            self.active.cancel()
            await asyncio.gather(self.active, return_exceptions=True)

    def start(self):
        self.runner = asyncio.create_task(self.run())

    async def close(self):
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
