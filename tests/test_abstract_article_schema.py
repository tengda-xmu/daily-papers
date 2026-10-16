from copy import deepcopy
import hashlib
import json

import pytest

from src.auto_reading import fingerprint, public_analysis, public_status, status_label
from src.reading_article import (
    ABSTRACT_SECTIONS, ARTICLE_VERSION, article_claim_targets, article_key,
    article_prompt, public_article,
)
from tests.test_reading_articles import article_fixture
from tests.test_reading_queue import response, sample


def abstract_article():
    return {
        'version': ARTICLE_VERSION, 'basis': 'abstract',
        'lead': '这篇研究关注有限观测条件下的结构响应预测，摘要提出结合物理信息与数据学习的建模思路。',
        'sections': [
            {'id': 'context', 'title': '有限观测为什么限制结构预测',
             'paragraphs': ['摘要把观测不足作为研究出发点，讨论怎样利用已知物理关系辅助结构响应预测。']},
            {'id': 'method', 'title': '物理信息怎样参与模型学习',
             'paragraphs': ['作者在摘要中介绍将物理关系纳入数据模型的方法，但没有说明全部网络配置与训练步骤。']},
            {'id': 'findings', 'title': '摘要报告了什么结果',
             'paragraphs': ['摘要报告了该建模思路在给定任务中的应用，但当前资料没有提供可逐项核对的误差数值。']},
            {'id': 'limitations', 'title': '哪些结论仍需阅读全文',
             'paragraphs': ['独立样本量、训练测试划分和基线条件未在摘要中说明，尚不能判断模型的外推能力。']},
            {'id': 'inspiration', 'title': '阅读启发：关注验证条件',
             'paragraphs': ['阅读启发：后续阅读全文时可重点核对样本独立性与测试工况，这些问题来自当前摘要的信息边界。']},
        ],
    }


def diagram():
    return {
        'kind': 'study', 'basis': 'abstract', 'title': '依据摘要整理的建模思路',
        'description': '图中仅连接摘要明确介绍的物理信息输入与结构响应预测，不代表论文原图或完整实验过程。',
        'nodes': [{'id': 'a', 'stage': 0, 'label': '物理信息', 'detail': '摘要明确介绍的物理关系'},
                  {'id': 'b', 'stage': 1, 'label': '响应预测', 'detail': '摘要明确介绍的建模目标'}],
        'edges': [{'from': 'a', 'to': 'b', 'label': '辅助建模'}],
    }


def analysis_value(article, basis='abstract'):
    paper = sample()
    value = response(paper)
    value.update(paper_id=paper['id'], fingerprint=fingerprint(paper))
    value['analysis'].update(analysis_basis=basis, analyzed_at='2026-10-16T00:00:00+00:00',
                             article=article, article_updated_at='2026-10-16T00:01:00+00:00',
                             article_profile='c' * 64)
    if basis == 'full_text':
        value['material'] = {'version': 'a' * 64, 'total': 1, 'covered': 1,
                             'references': {'findings': ['P1']}, 'excerpt': paper['abstract'][:80]}
    return value


def test_short_abstract_needs_no_fabricated_experiments_routes_or_figures():
    draft = abstract_article()
    draft.update(claim_evidence={'lead': ['abstract:0']}, private_path='C:/private/evidence.json')
    public = public_article(draft)
    assert public['version'] == 3 and public['basis'] == 'abstract'
    assert [section['id'] for section in public['sections']] == list(ABSTRACT_SECTIONS)
    assert public['experiments'] == public['routes'] == public['diagrams'] == public['figure_groups'] == []
    assert 'related' not in public and 'claim_evidence' not in public and 'private_path' not in public
    assert public_analysis(analysis_value(draft))['analysis']['article'] == public
    assert 'diagrams' not in draft  # Normalization does not modify the private draft.


def test_abstract_diagram_and_original_captions_remain_distinct_and_bound():
    draft = abstract_article()
    draft['diagrams'] = [diagram()]
    draft['figure_groups'] = [{'section': 'findings', 'title': '原图中的响应预测',
                              'panels': [{'figure_id': 'fig-original', 'caption': '原图展示所报告工况下的预测响应。'}]}]
    public = public_article(draft)
    assert public['diagrams'][0]['basis'] == 'abstract'
    assert public['figure_groups'] == draft['figure_groups']
    targets = article_claim_targets(public)
    assert targets['diagrams.0']
    assert targets['figure_groups.0.panels.0.caption'] == draft['figure_groups'][0]['panels'][0]['caption']
    assert targets['sections.4.title'].startswith('阅读启发')
    assert targets['sections.4.paragraphs.0'].startswith('阅读启发')
    assert not any(key.startswith(('experiments.', 'routes.')) for key in targets)
    assert article_claim_targets(public, include_proposals=True) == targets


@pytest.mark.parametrize('change', [
    lambda value: value.pop('basis'),
    lambda value: value.update(basis='metadata'),
    lambda value: value.update(version=2),
    lambda value: value['sections'].pop(),
    lambda value: value['sections'][2].update(id='validation'),
    lambda value: value.update(experiments=[{'results': 'unverified'}]),
    lambda value: value.update(routes=[{'title': 'unverified'}]),
    lambda value: value.update(diagrams=[{**diagram(), 'kind': 'proposal'}]),
    lambda value: value.update(diagrams=[{key: item for key, item in diagram().items() if key != 'basis'}]),
    lambda value: value.update(diagrams=[diagram(), diagram()]),
    lambda value: value.update(diagrams=[{**diagram(), 'nodes': diagram()['nodes'][:1]}]),
    lambda value: value.update(diagrams=[{**diagram(), 'edges': [{'from': 'a', 'to': 'c'}]}]),
    lambda value: value.update(diagrams=[{**diagram(), 'nodes': diagram()['nodes'] +
        [{'id': 'c', 'stage': 2, 'label': '无依据节点', 'detail': '不存在的独立研究环节'}]}]),
    lambda value: value.update(figure_groups=[{'section': 'inspiration', 'title': '误标建议',
        'panels': [{'figure_id': 'fig-a', 'caption': '原图不可用于冒充建议实验。'}]}]),
    lambda value: value.update(figure_groups=[{'section': 'method', 'title': '过多图幅',
        'panels': [{'figure_id': f'fig-{i}', 'caption': '原图中的研究过程说明。'} for i in range(5)]}]),
])
def test_invalid_abstract_shapes_cannot_cross_publication(change):
    value = abstract_article()
    change(value)
    with pytest.raises(ValueError):
        public_article(value)


def test_abstract_original_figure_ids_cannot_repeat():
    value = abstract_article()
    panel = {'figure_id': 'fig-a', 'caption': '原图中的研究过程说明。'}
    value['figure_groups'] = [{'section': section, 'title': '重复使用同一原图', 'panels': [deepcopy(panel)]}
                             for section in ('method', 'findings')]
    with pytest.raises(ValueError, match='重复'):
        public_article(value)


@pytest.mark.parametrize('version', [1, 2])
def test_legacy_full_text_articles_stay_readable_without_rewriting(version):
    value = article_fixture()
    value['version'] = version
    value.pop('basis')
    cleaned = public_article(value)
    assert cleaned['version'] == version and cleaned.get('basis', 'full_text') == 'full_text'
    assert 'basis' not in cleaned
    assert public_analysis(analysis_value(value, 'full_text'))['analysis']['article'] == cleaned
    assert 'basis' not in value


@pytest.mark.parametrize('article,basis', [(abstract_article, 'full_text'), (article_fixture, 'abstract')])
def test_public_analysis_rejects_article_and_reading_scope_mismatch(article, basis):
    with pytest.raises(ValueError, match='basis'):
        public_analysis(analysis_value(article(), basis))


def test_keys_separate_abstract_scope_without_invalidating_completed_full_text_cache():
    material, directions = 'a' * 64, [{'id': 'robotics'}]
    figures = [{'figure_id': 'fig-image-sha256-version-1', 'caption': 'Publisher original caption'}]
    legacy = hashlib.sha256(json.dumps([2, material, directions,
        [('fig-image-sha256-version-1', 'Publisher original caption', 'publisher')]],
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert article_key(material, directions, figures) == legacy
    assert article_key(material, directions, figures, basis='full_text') == legacy
    abstract_key = article_key(material, directions, figures, basis='abstract')
    assert abstract_key != legacy
    assert article_key('b' * 64, directions, figures, basis='abstract') != abstract_key
    assert article_key(material, [], figures, basis='abstract') != abstract_key
    assert article_key(material, directions, [{**figures[0], 'figure_id': 'fig-image-sha256-version-2'}], basis='abstract') != abstract_key
    assert article_key(material, directions, [{**figures[0], 'caption': 'Corrected original caption'}], basis='abstract') != abstract_key


def test_abstract_prompt_uses_actual_abstract_and_original_caption_ids():
    prompt = article_prompt({'title': 'Fixture'}, [], evidence={
        'abstract': 'ACTUAL COMPLETE ABSTRACT', 'supports': {'abstract:0': {'quote': 'ACTUAL COMPLETE ABSTRACT'}}},
        figures=[{'figure_id': 'fig-a', 'caption': 'ACTUAL ORIGINAL CAPTION'}], basis='abstract')
    assert 'ACTUAL COMPLETE ABSTRACT' in prompt and 'ACTUAL ORIGINAL CAPTION' in prompt
    assert 'abstract:0' in prompt and 'figure:原图ID' in prompt
    assert '600–1200' in prompt and '1800–2500' not in prompt
    assert '不得声称已经阅读全文' in prompt and 'sections.i.paragraphs.j（包括inspiration）' in prompt
    assert '"basis": "abstract"' in prompt


def test_abstract_prompt_diagram_example_passes_schema_and_explains_short_ids():
    prompt = article_prompt({'title': 'Fixture'}, [], evidence={'abstract': 'Source'}, basis='abstract')
    marker = '图结构示例仅说明格式，不得把示例中的内容当作研究事实：'
    example, _ = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])
    draft = abstract_article()
    draft['diagrams'] = [example]
    assert public_article(draft)['diagrams'] == [example]
    assert '节点id使用a到h中的单个小写字母且不重复' in prompt
    assert '连线from和to严格引用这些id' in prompt
    draft['diagrams'][0]['nodes'][0]['id'] = 'requirements'
    with pytest.raises(ValueError, match='节点标识'):
        public_article(draft)


def test_abstract_prompt_handles_truncated_caption_without_inventing_numeric_support():
    prompt = article_prompt({'title': 'Fixture'}, [], evidence={'abstract': 'Source'}, basis='abstract')
    assert '只在图片可见、尚无绑定文字证据的精确数字不转写' in prompt
    assert '不得将数字改写成汉字绕过证据核验' in prompt
    assert '所有数值和单位必须在绑定的摘要或原始图注中实际出现' in prompt


def test_legacy_full_text_prompt_ignores_stale_abstract_assessment_in_paper_metadata():
    prompt = article_prompt({'title': 'Fixture', 'analysis_basis': 'abstract'}, [], {'verified_full_text': True},
                            {'status': 'ok', 'checked_at': '2026-10-16T00:00:00+00:00', 'papers': []})
    assert '中文全文精读' in prompt and '1800–2500' in prompt
    assert '"basis": "full_text"' in prompt
    assert '本次完整摘要与核验支持目录' not in prompt


@pytest.mark.parametrize('state', ['published', 'awaiting_fulltext'])
def test_abstract_status_never_claims_full_text_is_complete(state):
    value = public_status({'paper_id': '123456abcdef', 'state': state, 'basis': 'abstract',
        'updated_at': '2026-10-16T00:00:00+00:00', 'article_state': 'complete',
        'article_basis': 'abstract', 'article_error': 'private evidence location'})
    assert value['article_basis'] == 'abstract'
    assert 'private' not in json.dumps(value)
    assert '摘要图文已完成' in status_label(value)
    assert '全文精读已完成' not in status_label(value)
    if state == 'awaiting_fulltext':
        assert '全文待补充' in status_label(value)
