import asyncio
from io import BytesIO
import json
from types import SimpleNamespace

from PIL import Image
import pymupdf as fitz
import pytest

from connectors.codex_bridge.documents import parse_pdf
from connectors.codex_bridge.pdf_translation import extract_layout, layout_batches, parse_translations, render_pdf, translate_pdf
from tests.test_codex_bridge import bridge, login, ask, P1, P2


def paper_pdf():
    pdf = fitz.open()
    for i in range(2):
        page = pdf.new_page(width=600, height=780)
        page.insert_text((40, 52), 'Scientific model validation', fontsize=18, fontname='hebo')
        for x in (40, 315):
            page.insert_textbox((x, 80, x+240, 260),
                ('We validate the prediction model using independent observations. '
                 'Measured uncertainty is compared with experimental evidence. ') * 4, fontsize=10)
        page.draw_rect((40, 300, 280, 405), fill=(.8, .9, 1), color=(.1, .2, .5))
        page.insert_text((65, 350), 'E = mc2', fontsize=16)
        buf = BytesIO(); Image.new('RGB', (100, 60), '#17496f').save(buf, 'PNG')
        page.insert_image((315, 300, 555, 444), stream=buf.getvalue())
        page.insert_text((40, 475), 'Figure 1. Independent measurement and prediction results.', fontsize=10)
        page.insert_link({'kind':fitz.LINK_URI, 'from':fitz.Rect(40, 465, 300, 480), 'uri':'https://example.org/paper'})
    return pdf.tobytes()


def test_layout_preserves_pages_artwork_equations_links_and_complete_translated_text(tmp_path):
    original = tmp_path / 'original.pdf'; original.write_bytes(paper_pdf())
    work = tmp_path / 'work'; layout = extract_layout(original, work)
    translated = {r['id']: '科学模型验证' if r['bold'] else '本研究使用独立测量验证预测结果，比较实验数据与预测误差。' * (5 if len(r['source']) > 100 else 1)
                  for page in layout['pages'] for r in page}
    result = tmp_path / 'translated.pdf'
    stats = render_pdf(original, result, layout, translated, work)
    assert stats['pages'] == 2
    with fitz.open(original) as source, fitz.open(result) as target:
        assert len(source) == len(target)
        for a, b in zip(source, target):
            assert a.rect == b.rect
            assert '科学模型验证' in b.get_text()
            assert 'Scientific model validation' not in b.get_text()
            assert 'E = mc2' in b.get_text()
            assert any(link.get('uri') == 'https://example.org/paper' for link in b.get_links())
            for rect in ((40, 300, 280, 405), (315, 300, 555, 444)):
                assert a.get_pixmap(clip=rect).samples == b.get_pixmap(clip=rect).samples


def test_overflow_never_creates_a_clipped_pdf_and_json_must_cover_every_region(tmp_path):
    original = tmp_path / 'original.pdf'; original.write_bytes(paper_pdf())
    layout = extract_layout(original, tmp_path / 'work')
    batch = next(layout_batches(layout))
    with pytest.raises(ValueError, match='缺少'):
        parse_translations('{}', batch)
    formula = [{'id':'P1B1', 'source':'Variable ⟦M0⟧ is measured.'}]
    with pytest.raises(ValueError, match='公式'):
        parse_translations('{"P1B1":"变量发生了变化。"}', formula)
    translated = {r['id']:'冗长的翻译文字' * 1200 for p in layout['pages'] for r in p}
    with pytest.raises(ValueError, match='无法在原位置'):
        render_pdf(original, tmp_path/'fail.pdf', layout, translated, tmp_path/'work')
    assert not (tmp_path/'fail.pdf').exists()


def test_scanned_pages_are_identified_before_any_model_call(tmp_path):
    pdf = fitz.open(); page = pdf.new_page()
    buf = BytesIO(); Image.new('RGB', (600,800), 'white').save(buf, 'PNG')
    page.insert_image(page.rect, stream=buf.getvalue())
    pdf.save(tmp_path/'scan.pdf')
    with pytest.raises(ValueError, match='第 1 页为扫描'):
        extract_layout(tmp_path/'scan.pdf', tmp_path/'work')


def test_fragmented_justified_line_is_not_left_under_translation(tmp_path):
    pdf = fitz.open(); page = pdf.new_page()
    x = 40
    for word in ('Heuristics,', 'and', 'subsequently', 'evaluated', 'through', 'simulation'):
        page.insert_text((x,100), word, fontsize=9)
        x += fitz.get_text_length(word, fontsize=9) + 10
    page.insert_text((40,111), 'or experiments with independent measurement evidence and validation.', fontsize=9)
    path=tmp_path/'fragments.pdf'; pdf.save(path)
    layout=extract_layout(path,tmp_path/'work')
    translated={r['id']:'通过模拟和实验验证结果。' for p in layout['pages'] for r in p}
    render_pdf(path,tmp_path/'translated.pdf',layout,translated,tmp_path/'work')
    with fitz.open(tmp_path/'translated.pdf') as output:
        text=output[0].get_text()
        assert '通过模拟' in text
        for word in ('Heuristics','subsequently','evaluated','simulation','experiments'):
            assert word not in text


def translated_turn(rpc):
    async def turn(thread, text, images=(), *, model=None):
        rpc.inputs.append((thread, text, images))
        inputs = json.loads(text.split('待译区域 JSON：\n')[1].split('\n上次输出')[0])
        yield {'type':'delta', 'text':json.dumps({k:'科学研究通过测量验证模型的可靠性。' for k in inputs}, ensure_ascii=False)}
        yield {'type':'completed', 'status':'completed'}
    return turn


def test_layout_translation_generates_scoped_download_and_reuses_checkpoints(bridge):
    c, app, rpc = bridge; h = login(c, app)
    store = app.state.store
    store.set_document(P1, parse_pdf(paper_pdf(), store.directory(P1)))
    rpc.turn = translated_turn(rpc)
    result = ask(c, h, mode='translate', translation_source='layout')
    assert '"status": "completed"' in result.text, result.text
    row = store.history(P1)[-1]
    assert row['artifact']['kind'] == 'layout-pdf' and row['artifact']['pages'] == 2
    url = f'/api/papers/{P1}/export-pdf?message_id={row["id"]}'
    assert c.get(url).status_code == 401
    assert c.get(f'/api/papers/{P2}/export-pdf?message_id={row["id"]}', headers=h).status_code == 404
    response = c.get(url, headers=h)
    assert response.status_code == 200 and response.content.startswith(b'%PDF-')
    assert len(fitz.open(stream=response.content)) == 2
    count = len(rpc.inputs)
    ask(c, h, mode='translate', translation_source='layout')
    assert len(rpc.inputs) == count
    store.set_document(P1, {'kind':'html', 'hash':'new-document', 'pages':[]})
    assert c.get(url, headers=h).content == response.content  # download keeps its exact source version
    assert '原 PDF' in ask(c, h, mode='translate', translation_source='layout').text


def test_failed_layout_job_is_not_exported_as_a_fake_translated_pdf(bridge):
    c, app, rpc = bridge; h = login(c, app)
    app.state.store.set_document(P1, parse_pdf(paper_pdf(), app.state.store.directory(P1)))
    result = ask(c, h, mode='translate', translation_source='layout')  # default mock returns prose, not structured translation
    assert '"status": "failed"' in result.text
    row = app.state.store.history(P1)[-1]
    assert not row['artifact']
    assert c.get(f'/api/papers/{P1}/export-pdf?message_id={row["id"]}', headers=h).status_code == 409
