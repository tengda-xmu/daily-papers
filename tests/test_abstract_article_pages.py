"""Abstract editorial pages share stable URLs without implying a full-text read."""
from copy import deepcopy
import json

from bs4 import BeautifulSoup
import pytest

from src.editions import append, write
from tests.test_reading_queue import response, sample
from tools.build_site import paper_card
from tools.reading_articles import build_articles, render_article


def abstract_article():
    paragraphs = {
        'context': '这项研究关注有限观测条件下的状态识别问题，摘要明确提出利用物理信息辅助数据建模。理解这一问题需要先区分任务目标与已经完成的验证范围。',
        'method': '摘要介绍将物理约束与学习模型结合的总体思路，但没有公开全部网络结构和训练细节。这里按资料实际给出的关系说明方法，不补写未披露的步骤。',
        'findings': '摘要报告方法能够支持研究设定下的预测任务，但尚未给出可直接核对的误差、独立样本数量和完整基线结果，不能据此判断真实部署收益。',
        'limitations': '当前依据限于公开摘要，样本划分、实验工况和外部验证范围仍待全文确认。没有出现在摘要中的细节不等于作者没有开展相关实验。',
        'inspiration': '建议后续阅读时优先核对独立测试数据、物理假设和方法适用条件，再判断能否迁移至其他场景。这是本站阅读启发，尚不是经过实验验证的方案。',
    }
    return {'version': 3, 'basis': 'abstract',
            'lead': '这篇论文尝试将物理信息引入数据建模，以回应观测有限时的预测需求。以下围绕公开摘要说明研究问题、核心思路和结论边界，并保留需要取得全文后才能回答的问题。',
            'sections': [{'id': key, 'title': title, 'paragraphs': [paragraphs[key]]} for key, title in zip(
                paragraphs, ['为什么需要物理信息', '物理信息怎样进入建模', '摘要实际报告了什么', '还不能判断哪些问题', '下一步阅读应关注什么'])],
            'experiments': [], 'routes': [], 'diagrams': [], 'figure_groups': []}


def abstract_paper():
    paper = sample()
    paper.update(response(paper)['analysis'])
    paper.update(analysis_status='ready', analysis_basis='abstract', article=abstract_article(),
                 analyzed_at='2026-10-15T10:00:00+00:00', article_updated_at='2026-10-15T11:00:00+00:00')
    return paper


def study_diagram():
    return {'kind': 'study', 'basis': 'abstract', 'title': '摘要中的研究思路',
            'description': '根据摘要介绍的问题与方法绘制概念关系，未加入未披露的训练步骤或实验结果。',
            'nodes': [{'id': 'a', 'stage': 0, 'label': '公开观测', 'detail': '摘要介绍的输入信息'},
                      {'id': 'b', 'stage': 1, 'label': '建模任务', 'detail': '摘要说明的方法目标'}],
            'edges': [{'from': 'a', 'to': 'b', 'label': '用于'}]}


def original_figures():
    return [{'figure_id': f'original-{i}', 'asset': f'assets/figures/original-{i}.png',
             'figure_label': f'Fig. {i}', 'title': 'Original method figure', 'caption': 'Original publisher caption.',
             'caption_basis': 'publisher', 'credit': 'Original authors', 'source_url': 'https://example.org/figure',
             'license_url': 'https://creativecommons.org/licenses/by/4.0/', 'license': 'CC BY 4.0'} for i in (1, 2)]


@pytest.mark.parametrize('ready,basis,label', [(True, 'abstract', '摘要解读'), (True, 'full_text', '全文精读'),
                                              (False, '', '摘要与笔记')])
def test_all_card_states_use_one_stable_independent_page(ready, basis, label):
    paper = sample(); paper.update(analysis_status='ready' if ready else 'pending', analysis_basis=basis)
    for root in ('./', '../'):
        card = BeautifulSoup(paper_card(paper, 'core', root=root), 'html.parser')
        link = card.select_one('.reading-link')
        assert link.get_text() == label and link['href'] == root + 'readings/' + paper['id'] + '.html'
        assert not card.select('.paper-toggle, .paper-detail-panel, .reading-notes')
        actions = card.select('.paper-tools > *')
        assert actions.index(link) < next(i for i, n in enumerate(actions) if 'Codex 对话' in n.get_text())


def test_abstract_article_has_its_own_scope_without_fulltext_experiments_or_routes():
    paper = abstract_paper()
    paper['article']['sections'][0]['paragraphs'][0] += '<script>bad()</script> **摘要边界**'
    html = render_article(paper)
    page = BeautifulSoup(html, 'html.parser')
    assert page.select_one('.reading-article')['data-analysis-basis'] == 'abstract'
    assert page.select_one('.article-type').get_text() == '摘要解读'
    assert '依据公开摘要整理，未核验全文细节' in html and '依据论文全文整理' not in html
    assert not page.select('.experiment-evidence, .article-future, .article-diagram, #related')
    assert not page.select('a[href="#future"], a[href="#related"]')
    assert '不是论文已验证的结论' in html and '<script>bad' not in html
    assert '<strong>摘要边界</strong>' in html


def test_optional_abstract_diagram_and_originals_keep_separate_attribution():
    paper = abstract_paper(); paper['article']['diagrams'] = [study_diagram()]
    paper['article']['figure_groups'] = [{'section': 'context', 'title': '研究对象与方法原图',
        'panels': [{'figure_id': f'original-{i}', 'caption': '依据原始图注说明此面板展示的方法关系。'} for i in (1, 2)]}]
    page = BeautifulSoup(render_article(paper, figures=original_figures()), 'html.parser')
    assert len(page.select('#context .article-original-figure')) == 2
    assert '依据摘要整理的研究思路示意图（非论文原图）' in page.get_text()
    assert all('Original publisher caption.' in n.get_text() for n in page.select('.article-original-figure'))


@pytest.mark.parametrize('abstract,label', [('', '尚未取得可核实的完整摘要或全文'),
                                           ('Some search fragment ...', '当前只有检索片段')])
def test_missing_or_fragment_material_does_not_publish_old_unverified_notes(abstract, label):
    paper = sample(); paper.update(abstract=abstract, analysis_status='ready', analysis_basis='abstract',
        summary='不能公开的未核验总结', deep_read={'method': '不能公开的未核验方法'}, article=abstract_article())
    html = render_article(paper)
    assert label in html and '不能公开的未核验' not in html
    assert '摘要解读已完成' not in html and '依据论文全文整理' not in html
    assert 'article-toc' not in html
    assert ('检索片段（非完整摘要）' if abstract else '论文资料') in html


def test_legacy_abstract_notes_remain_readable_while_article_is_pending():
    paper = abstract_paper(); paper.pop('article'); paper.pop('article_updated_at')
    page = BeautifulSoup(render_article(paper, figures=original_figures()), 'html.parser')
    assert '尚未按图文稿逐项复核' in page.get_text()
    assert paper['deep_read']['method'] in page.get_text()
    assert len(page.select('.article-original-figure')) == 2
    assert not page.select('.article-diagram')


def test_build_covers_abstract_pending_and_history_and_never_downgrades_fulltext(tmp_path, monkeypatch):
    data = tmp_path/'data'; out = tmp_path/'site'
    paper = abstract_paper()
    full = deepcopy(paper); full.update(analysis_basis='full_text'); full.pop('article')
    full['summary'] = '保留已经核验过的全文内容。'
    append(data, {'generated_at': '2026-10-14T10:00:00+00:00', 'update_run_id': 'old-full', 'core': [full], 'extended': []})
    pending = sample(); pending.update(id='abcdef123456', abstract='', analysis_status='pending')
    abstract = abstract_paper(); abstract['id'] = '012345abcdef'
    write(data/'daily.json', {'core': [paper, pending], 'extended': [abstract]})
    monkeypatch.setattr('src.paper_titles.DATA', data)
    build_articles(data, out, {})
    assert {path.stem for path in (out/'readings').glob('*.html')} == {paper['id'], pending['id'], abstract['id']}
    manifest = json.loads((out/'article-figures.json').read_text(encoding='utf-8'))
    assert set(manifest['papers']) == {paper['id'], pending['id'], abstract['id']}
    full_html = (out/'readings'/f"{paper['id']}.html").read_text(encoding='utf-8')
    assert '保留已经核验过的全文内容' in full_html and '依据论文全文整理' in full_html
    assert '尚未取得可核实' in (out/'readings'/f"{pending['id']}.html").read_text(encoding='utf-8')
    assert '依据公开摘要整理' in (out/'readings'/f"{abstract['id']}.html").read_text(encoding='utf-8')


@pytest.mark.parametrize('with_manifest', [False, True])
def test_retained_legacy_archive_links_get_pages_without_reviving_deleted_batches(tmp_path, monkeypatch, with_manifest):
    data = tmp_path/'data'; out = tmp_path/'site'
    kept = abstract_paper(); kept['id'] = '111111111111'
    write(data/'archive/2026-09-10.json', {'update_run_id': 'legacy-kept', 'core': [kept]})
    expected = {kept['id']}
    if with_manifest:
        current = abstract_paper(); current['id'] = '222222222222'
        payload = append(data, {'generated_at': '2026-10-14T10:00:00+00:00', 'update_run_id': 'current-batch',
                               'core': [current], 'extended': []})
        write(data/'daily.json', payload)
        expected.add(current['id'])
        index = json.loads((data/'editions/index.json').read_text(encoding='utf-8'))
        index['deleted'] = [{'id': 'deleted-batch', 'date': '2026-09-11'}]
        write(data/'editions/index.json', index)
        for day, run_id, identifier in [('2026-10-14', 'old-date-alias', '333333333333'),
                                        ('2026-09-11', 'another-id-on-deleted-day', '444444444444'),
                                        ('2026-09-12', 'deleted-batch', '555555555555')]:
            paper = {**abstract_paper(), 'id': identifier}
            write(data/'archive'/f'{day}.json', {'update_run_id': run_id, 'core': [paper]})
            # A previous build must not keep stale pages for the removed rows.
            path = out/'readings'/f'{identifier}.html'; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Old reading page', encoding='utf-8')
    monkeypatch.setattr('src.paper_titles.DATA', data)
    build_articles(data, out, {})
    assert {path.stem for path in (out/'readings').glob('*.html')} == expected
    manifest = json.loads((out/'article-figures.json').read_text(encoding='utf-8'))
    assert set(manifest['papers']) == expected
    link = BeautifulSoup(paper_card(kept, 'core', root='../'), 'html.parser').select_one('.reading-link')
    assert (out/'archive'/link['href']).resolve() == (out/'readings'/f"{kept['id']}.html").resolve()
    assert (out/'archive'/link['href']).resolve().is_file()
