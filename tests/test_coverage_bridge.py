"""Local public routes enforce the same live reprint permissions as builds."""
from copy import deepcopy
import hashlib
import io

from PIL import Image

from tests.test_codex_bridge import bridge
from tests.test_paper_coverage_ui import coverage_fixture
from tools.coverage_pages import article_name


def test_local_reprint_routes_recheck_revocation(bridge, monkeypatch, tmp_path):
    client, app, _ = bridge
    coverage = coverage_fixture()
    article = coverage['articles'][0]
    image = io.BytesIO(); Image.new('RGB', (30,30), 'blue').save(image,format='PNG')
    body=image.getvalue(); digest=hashlib.sha256(body).hexdigest()
    src='assets/wechat/'+digest+'.png'
    article.update(display_mode='reprint', blocks=[{'type':'paragraph','text':'获准转载的论文介绍正文'},
                                                  {'type':'image','src':src,'alt':'原文图'}])
    directory=tmp_path/'site/assets/wechat';directory.mkdir(parents=True)
    (directory/(digest+'.png')).write_bytes(body)
    monkeypatch.setattr('src.paper_coverage.load_public', lambda root: {'123456789abc':deepcopy(coverage)})
    path='/wechat/'+article_name(article)+'.html'
    assert client.get(path).status_code==200
    assert client.get('/'+src).content==body
    article['display_mode']='source';article.pop('blocks')
    assert client.get(path).status_code==404
    assert client.get('/'+src).status_code==404
    assert (directory/(digest+'.png')).is_file()  # Presence on disk does not grant access.


def test_local_card_introduction_is_before_conversation(bridge, monkeypatch):
    client,_,_=bridge
    monkeypatch.setattr('src.paper_coverage.load_public', lambda root: {'123456789abc':coverage_fixture()})
    response=client.get('/recommendations.html')
    assert response.status_code==200
    from bs4 import BeautifulSoup
    soup=BeautifulSoup(response.text,'html.parser')
    tools=soup.select_one('.paper-tools')
    entry = tools.select_one('.coverage-link')
    assert entry.name == 'a' and entry['href'] == coverage_fixture()['articles'][0]['url']
    assert '结构研究' in entry['title']
    assert entry.find_next_sibling('button')['class'] == ['text-button', 'codex-entry']
    assert not soup.select('.paper-coverage-panel')
