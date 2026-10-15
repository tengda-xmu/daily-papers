from copy import deepcopy
from datetime import datetime, timedelta, timezone

from src.coverage_candidates import (
    REJECTED, build_discovery_index, canonical_candidate_url, triage_candidates,
)
from src.coverage_discovery import SEARCH_STRATEGY_VERSION
from src.public_sources import read, write
from src.wechat_metadata import public_index_url


NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)
PAPER = {
    'id': '0e496acb319d', 'doi': '10.1016/j.engfracmech.2026.112500',
    'title': 'A solution-guided machine learning framework for multiaxial fatigue life prediction',
    'authors': ['Yang Deng', 'Jiantao Leng', 'Ruijin Wang', 'Tianquan Ying', 'Tienchong Chang'],
}


def candidate(title, account='来源公众号'):
    return {'title': title, 'account': account, 'url': public_index_url(account, title),
            'match_status': 'pending', 'reason': 'index_only'}


def search_cache(root, rows, name='example', **extra):
    write(root / 'data/cache/paper-coverage/searches' / (name + '.json'), {'rows': rows, **extra})


def test_screenshot_six_unrelated_index_rows_are_privately_rejected(tmp_path):
    rows = [candidate(title, account) for title, account in [
        ('疲劳分析小白文:What is a SN-Curve?', '疲劳研究'),
        ('最新一期:Special Issue in Machine Learning in Structural Engineering', 'ES工程结构'),
        ('机器学习求解组合优化最新SOTA方案汇总,涉及图匹配、图着色、因果发现...', '算法研究'),
        ('人工智能,计算机视觉,机器学习, 图像和视频处理论文速递--(2026-03-04)', '阿武的大学秘籍'),
        ('ai4protein论文推荐 | 2026-03-25', '雨明'),
        ('ai4protein论文推荐 | 2026-03-24', '雨明'),
    ]]
    snippets = ['', 'A machine learning approach to predicting mechanical behaviour ... for estimating fatigue life in composite materials of the aerospace ...',
                '', '机器学习(cs.LG:Machine Learning) SynthCharge: a popular model design choice for MRI restoration.',
                'The effectiveness of machine learning models critically depends on broader applications in science and engineering.',
                'The formation and development of geologic environments. We propose a multimodal retrieval-augmented ...']
    search_cache(tmp_path, [{**row, '_discovery_text': snippet} for row, snippet in zip(rows, snippets)])
    before = deepcopy(rows)
    assert triage_candidates(tmp_path, PAPER, rows, NOW, build_discovery_index(tmp_path)) == []
    assert rows == before
    rejected = [read(path) for path in (tmp_path / REJECTED).glob('*.json')]
    assert len(rejected) == 6
    assert all(row['paper_id'] == PAPER['id'] and row['reason'] == 'index_candidate_not_relevant' for row in rejected)
    assert all(row['search_strategy_version'] == SEARCH_STRATEGY_VERSION and row['rejected_at'] == NOW.isoformat() for row in rejected)
    assert not (tmp_path / 'data/paper-coverage.json').exists()


def test_generic_title_with_cached_exact_doi_stays_pending(tmp_path):
    row = candidate('本周论文介绍')
    search_cache(tmp_path, [{**row, '_discovery_text': '本篇报道的 DOI 为 ' + PAPER['doi']}])
    before = deepcopy(row)
    result = triage_candidates(tmp_path, PAPER, [row], NOW, build_discovery_index(tmp_path))
    assert result == [before] and result[0] is row
    assert result[0]['match_status'] == 'pending' and result[0]['reason'] == 'index_only'
    assert 'match_basis' not in result[0] and 'summary' not in result[0]
    assert not (tmp_path / REJECTED).exists()


def test_search_query_and_unselected_raw_fields_cannot_supply_identity(tmp_path):
    row = candidate('本周论文介绍')
    search_cache(tmp_path, [{**row, 'raw_body': PAPER['doi'], 'query': PAPER['title']}], query=PAPER['doi'])
    assert triage_candidates(tmp_path, PAPER, [row], NOW, build_discovery_index(tmp_path)) == []
    index = build_discovery_index(tmp_path)
    assert all(set(text) == {'title', 'summary'} for texts in index.values() for text in texts)


def test_credible_chinese_title_without_private_cache_is_retained(tmp_path):
    paper = {**PAPER, 'title_zh': '解引导机器学习框架预测多轴疲劳寿命'}
    row = candidate('解引导机器学习框架：多轴疲劳寿命预测新进展')
    assert triage_candidates(tmp_path, paper, [row], NOW, {}) == [row]


def test_body_image_direct_and_verified_candidates_pass_through_unchanged(tmp_path):
    rows = [{**candidate('主题不相近'), 'reason': reason} for reason in
            ('visual_pending', 'article_challenge', 'article_unavailable', 'identity_unconfirmed')]
    rows += [{**candidate('已核实的论文'), 'match_status': 'verified', 'match_basis': 'image_doi'},
             {**candidate('有原文链接'), 'url': 'https://mp.weixin.qq.com/s/actual'}]
    before = deepcopy(rows)
    result = triage_candidates(tmp_path, PAPER, rows, NOW, {})
    assert result == before and all(a is b for a, b in zip(result, rows))
    assert not (tmp_path / REJECTED).exists()


def test_new_evidence_readmits_a_previously_rejected_url_without_a_blacklist(tmp_path):
    row = candidate('本周论文介绍')
    assert triage_candidates(tmp_path, PAPER, [row], NOW, {}) == []
    search_cache(tmp_path, [{**row, 'snippet': PAPER['title'] + ' Yang Deng'}])
    result = triage_candidates(tmp_path, PAPER, [row], NOW + timedelta(minutes=1), build_discovery_index(tmp_path))
    assert result == [row] and result[0]['match_status'] == 'pending'
    # The rejection remains a dated audit event, not an admission decision.
    assert len(list((tmp_path / REJECTED).glob('*.json'))) == 1


def test_discovery_index_canonicalizes_urls_deduplicates_and_limits_text_fields(tmp_path):
    row = candidate('文章标题', '学术账号')
    alternate = row['url'].replace('https:', 'http:').replace('%20', '+') + '&page=2&token=PRIVATE'
    search_cache(tmp_path, [{**row, 'url': alternate, '_discovery_text': 'x' * 6000},
                            {**row, '_discovery_text': 'x' * 6000},
                            {'url': 'https://evil.example/a', 'title': PAPER['title']},
                            {'url': row['url'], 'title': {'bad': PAPER['doi']}}, 'invalid'])
    index = build_discovery_index(tmp_path)
    key = canonical_candidate_url(row['url'])
    assert list(index) == [key]
    assert index[key] == [{'title': row['title'], 'summary': 'x' * 4000}]
    assert canonical_candidate_url(alternate) == key
    assert canonical_candidate_url('https://user:password@weixin.sogou.com/weixin?type=2&query=foo') == ''


def test_rejections_are_scoped_to_paper_and_tolerate_bad_cache_files(tmp_path):
    row = candidate('本周论文介绍')
    search_cache(tmp_path, [], name='empty')
    write(tmp_path / 'data/cache/paper-coverage/searches/list.json', [])
    (tmp_path / 'data/cache/paper-coverage/searches/broken.json').write_text('{', encoding='utf-8')
    assert build_discovery_index(tmp_path) == {}
    triage_candidates(tmp_path, PAPER, [row], NOW, {})
    triage_candidates(tmp_path, {**PAPER, 'id': 'another-paper'}, [row], NOW, {})
    assert len(list((tmp_path / REJECTED).glob('*.json'))) == 2
