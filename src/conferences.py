"""Verified conference identities; plain venue mentions are not acceptance proof."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

SOURCE = 'AI 顶会论文'
GROUP = 'AI 顶会主会论文'
CONFIG = Path(__file__).resolve().parents[1] / 'config/conferences.json'
CONFERENCES = json.loads(CONFIG.read_text(encoding='utf-8'))['conferences']
BY_ID = {c['id']: c for c in CONFERENCES}
HOSTS = {'doi.org', 'proceedings.neurips.cc', 'papers.nips.cc', 'proceedings.mlr.press', 'openreview.net',
         'api2.openreview.net', 'iclr.cc', 'openaccess.thecvf.com', 'www.ecva.net', 'ecva.net',
         'ojs.aaai.org', 'www.ijcai.org', 'ijcai.org', 'kdd.org', 'www.kdd.org', 'kdd2026.kdd.org', 'aclanthology.org', 'dl.acm.org'}
PROOF_HOSTS = {
    'neurips': {'proceedings.neurips.cc','papers.nips.cc','openreview.net'},
    'icml': {'proceedings.mlr.press','openreview.net'}, 'iclr': {'openreview.net','iclr.cc'},
    'aaai': {'ojs.aaai.org'}, 'ijcai': {'www.ijcai.org','ijcai.org'},
    'kdd': {'kdd.org','www.kdd.org','kdd2026.kdd.org'}, 'cvpr': {'openaccess.thecvf.com'},
    'iccv': {'openaccess.thecvf.com'}, 'eccv': {'www.ecva.net','ecva.net'},
    'acl': {'aclanthology.org'}, 'emnlp': {'aclanthology.org'},
}


def public_url(url):
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
            return False
        if p.hostname in HOSTS:
            return True
        # Only the official proceedings repositories, never arbitrary GitHub files.
        if '%' in p.path or '\\' in p.path or any(part in ('.', '..') for part in p.path.split('/')):
            return False
        return p.hostname == 'raw.githubusercontent.com' and bool(re.fullmatch(
            r'/(?:mlresearch/v\d+/(?:main|gh-pages)/.*\.pdf|acl-org/acl-anthology/(?:master|main)/data/xml/[\w.-]+\.xml)', p.path))
    except (TypeError, ValueError):
        return False


def info(paper):
    value = paper
    for _ in range(8):
        if not isinstance(value, dict):
            break
        c = value.get('conference')
        if isinstance(c, dict) and c.get('id') in BY_ID and c.get('verified') is True and c.get('track') == 'main':
            if (type(c.get('year')) is int and 1980 <= c['year'] <= 2200 and public_url(c.get('proof_url', ''))
                    and urlsplit(c['proof_url']).hostname in PROOF_HOSTS[c['id']]):
                return {**c, 'name': BY_ID[c['id']]['name']}
        value = value.get('raw_metadata')
    return None


def label(c):
    return f"{c['name']} {c['year']} · 主会论文"
