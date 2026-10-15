"""Independent source I/O overlaps; decisions and public checkpoints stay serial."""
from collections import Counter
from threading import Barrier, Event, Lock
from urllib.parse import urlsplit

import pytest

from src.coverage_source_reader import SourceReader


def test_independent_hosts_overlap_and_every_source_is_read_once():
    barrier = Barrier(2)
    calls = Counter()
    def load(kind, url):
        calls[url] += 1
        barrier.wait(timeout=5)
        return url
    reader = SourceReader(load)
    keys = [('copy', 'https://alpha.org/a'), ('copy', 'https://beta.org/b')]
    reader.prepare(keys + keys)
    assert [reader.get(*key) for key in keys] == [key[1] for key in keys]
    reader.prepare(keys)
    assert set(calls.values()) == {1}


def test_same_host_parallelism_is_bounded_but_all_sources_complete():
    active, peak, calls = Counter(), Counter(), Counter()
    lock = Lock()
    release = Event()
    def load(kind, url):
        host = urlsplit(url).hostname
        with lock:
            active[host] += 1
            peak[host] = max(peak[host], active[host])
            calls[url] += 1
            if active[host] == 2:
                release.set()
        assert release.wait(timeout=5)
        with lock:
            active[host] -= 1
        return url
    keys = [('copy', f'https://alpha.org/{i}') for i in range(30)]
    reader = SourceReader(load)
    reader.prepare(keys)
    assert peak['alpha.org'] == 2
    assert len(calls) == 30 and all(count == 1 for count in calls.values())
    assert all(reader.get(*key) == key[1] for key in keys)


def test_failed_source_does_not_discard_other_read_results():
    def load(kind, url):
        if url.endswith('/broken'):
            raise OSError('unavailable')
        return url
    reader = SourceReader(load)
    reader.prepare([('copy', 'https://alpha.org/broken'), ('copy', 'https://beta.org/readable')])
    assert reader.get('copy', 'https://beta.org/readable') == 'https://beta.org/readable'
    with pytest.raises(OSError):
        reader.get('copy', 'https://alpha.org/broken')


def test_deadline_mode_does_not_start_speculative_reads():
    calls = []
    reader = SourceReader(lambda kind, url: calls.append(url) or url, concurrent=False)
    keys = [('copy', 'https://alpha.org/a'), ('copy', 'https://beta.org/b')]
    reader.prepare(keys)
    assert calls == []
    assert reader.get(*keys[0]) == keys[0][1]
    assert calls == [keys[0][1]]


def test_collector_reads_all_candidates_without_duplicate_forced_requests(tmp_path):
    from src.paper_coverage import collect, DATA
    from src.public_sources import write
    from tests.test_paper_coverage import setup, PAPER, NOW, URL, page
    setup(tmp_path)
    urls = [URL + str(i) for i in range(20)]
    write(tmp_path / DATA, {'papers': {PAPER['id']: {'state': 'pending_match',
        'articles': [], 'candidates': [{'url': u, 'title': PAPER['title'], 'reason': 'identity_unconfirmed'} for u in urls]}}})
    calls = Counter()
    lock = Lock()
    def fetch(url, source):
        with lock:
            calls[url] += 1
        return page()
    result = collect(tmp_path, NOW, perform_search=False, force_refresh=True,
                     fetcher=fetch, summarizer=lambda *_: None)
    assert len(result['papers'][PAPER['id']]['articles']) == 20
    assert set(calls) == set(urls) and set(calls.values()) == {1}
