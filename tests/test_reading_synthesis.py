"""Synthesis quotes must come from the same material validated on publication."""
import asyncio
from copy import deepcopy
import json

import pytest

from connectors.codex_bridge.reading_queue import ReadingQueue
from src.auto_reading import READER_VERSION, evidence_passages, prompt, public_status, status_label
from src.paper_sources import evidence_in_text
from tests.test_fulltext_reading import FullClient, material
from tests.test_reading_queue import sample, task


def current_material(p):
    m = material(p)
    actual = 'The current PDF evaluates physics-informed neural operators on six controlled benchmarks.'
    for page in m['document']['pages']:
        page['text'] = actual * 500
    m['text'] = '\n'.join(page['text'] for page in m['document']['pages'])
    return m, actual


def test_prompt_uses_acquired_source_instead_of_indexed_abstract():
    p = sample(); p['abstract'] = 'Old indexed summary that must not be a verbatim PDF quote.'
    m, actual = current_material(p)
    text = prompt(p, m, 'Completed reading notes [P1].')
    assert p['abstract'] not in text
    assert actual in text and 'Completed reading notes [P1].' in text
    abstract = prompt(p, {**m, 'basis': 'abstract', 'text': actual})
    assert p['abstract'] not in abstract and actual in abstract
    assert p['abstract'] in prompt(p)  # Legacy abstract-only callers remain supported.


def test_evidence_passages_are_bounded_and_verbatim_for_long_papers():
    p = sample(); m, _ = current_material(p)
    m['document']['pages'] = [{'label': f'P{i}', 'text': f'Original page {i}. ' * 4000} for i in range(1, 301)]
    text = evidence_passages(m)
    assert len(text) < 12500
    assert '[P1]' in text and '[P300]' in text
    for block in text.split('\n\n'):
        label, quote = block.split('\n', 1)
        assert quote in m['document']['pages'][int(label[2:-1])-1]['text']


def test_invalid_summary_retries_only_synthesis_with_explicit_feedback(tmp_path):
    p = sample(); m, actual = current_material(p); client = FullClient(p)
    q = ReadingQueue(tmp_path, tmp_path/'run', client, asyncio.Lock(), resolver=lambda p, **kw: m)
    q.quiet_until = 0; q.enqueue(p)
    asyncio.run(q.process(task(q)))
    failed = task(q)
    assert failed['state'] == 'retry' and failed['result'] is None
    assert failed['error_code'] == 'assessment_evidence'
    assert len(json.loads(failed['checkpoint'])['pages']) == 2
    client.value['evaluation']['evidence'] = actual
    client.calls.clear()
    asyncio.run(q.process(task(q)))
    ready = task(q)
    assert ready['state'] == 'ready' and ready['attempts'] == 2
    assert len(client.calls) == 1  # No page re-reading or additional automatic attempts.
    assert '上次汇总的引文在当前资料中无法核对' in client.calls[0][0]
    result = json.loads(ready['result'])
    assert result['material']['covered'] == 2
    assert evidence_in_text(result['evaluation']['evidence'], m['text'])
    assert json.loads(ready['checkpoint']) == json.loads(failed['checkpoint'])


@pytest.mark.parametrize('complete,same_version,enabled,recovered', [
    (True, True, 1, True), (False, True, 1, False),
    (True, False, 1, False), (True, True, 0, False),
])
def test_known_failure_migrates_once_without_losing_notes_or_reopening_other_failures(tmp_path, complete, same_version, enabled, recovered):
    p = sample(); m, _ = current_material(p); runtime = tmp_path/'run'
    q = ReadingQueue(tmp_path, runtime, FullClient(p), asyncio.Lock()); q.enqueue(p)
    ledger = {'version': m['version'] if same_version else 'f'*64, 'reader_version': READER_VERSION,
              'pages': {'P1': {'status': 'read', 'notes': 'Saved note', 'issues': []},
                        'P2': {'status': 'read' if complete else 'unreadable', 'notes': 'Saved note', 'issues': []}},
              'notes': ['Saved note']}
    with q.db() as db:
        db.execute("DELETE FROM reading_migrations WHERE name='synthesis-evidence-source-v1'")
        db.execute("""UPDATE tasks SET state='failed',attempts=3,thread_id='old-thread',error=?,
            material=?,checkpoint=?,draft='invalid prior synthesis',enabled=?""",
            ('Assessment evidence is not in the provided material', json.dumps(m), json.dumps(ledger), enabled))
    q = ReadingQueue(tmp_path, runtime, FullClient(p), asyncio.Lock())
    row = task(q)
    assert row['state'] == ('pending' if recovered else 'failed')
    assert row['attempts'] == (0 if recovered else 3)
    assert json.loads(row['checkpoint']) == ledger and row['draft'] == 'invalid prior synthesis'
    assert row['result'] is None and row['enabled'] == enabled
    if recovered:
        assert row['thread_id'] is None and row['error_code'] == 'assessment_evidence'
        with q.db() as db:
            db.execute("UPDATE tasks SET state='failed',attempts=3")
        again = ReadingQueue(tmp_path, runtime, FullClient(p), asyncio.Lock())
        assert task(again)['state'] == 'failed' and task(again)['attempts'] == 3


def test_public_progress_distinguishes_reading_from_synthesis_without_private_errors():
    value = public_status({'paper_id': '123456abcdef', 'state': 'failed', 'basis': 'full_text',
                          'updated_at': '2026-10-03T03:00:00+00:00', 'total': 12, 'covered': 12,
                          'error_code': 'assessment_evidence', 'error': 'private detail', 'draft': 'private draft'})
    label = status_label(value)
    assert '逐页阅读已完成' in label and '汇总引文未通过原文校验' in label
    assert '全文精读已完成' not in label and '补充资料' not in label
    assert 'private' not in json.dumps(value)
    assert '正在汇总精读' in status_label({**value, 'state': 'generating'})
    assert '全文精读未完成' in status_label({**value, 'covered': 11})
