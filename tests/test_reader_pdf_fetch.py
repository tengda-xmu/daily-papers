"""The PDF reader must show document acquisition progress beside its own button."""
import re
from tests.test_reader_manual_save import browser, reader, note, playwright, P1, P2


def empty_reader(page, context, client, headers):
    page.locator('.chat-close').click()
    data = client.get(f'/api/papers/{P1}', headers=headers).json()
    data.update(document=None, pdf_versions=[], reading=None)
    context.route(f'**/api/papers/{P1}', lambda route: route.fulfill(json=data))
    page.locator(f'.codex-entry[data-paper-id="{P1}"]').click()
    playwright.expect(page.locator('.reader-empty')).to_be_visible()


def test_fetch_from_empty_reader_shows_progress_and_failure(reader):
    page, context, client, headers, *rest = reader
    empty_reader(page, context, client, headers)
    pending = []
    context.route('**/fetch-pdf', lambda route: pending.append(route))
    playwright.expect(page.locator('.chat-settings')).to_be_hidden()
    page.locator('[data-read="fetch-pdf"]').click()
    playwright.expect(page.locator('.reader-status')).to_contain_text('正在获取')
    playwright.expect(page.locator('[data-read="fetch-pdf"]')).to_be_disabled()
    playwright.expect(page.locator('[data-read="upload"]')).to_be_disabled()
    playwright.expect(page.locator('[data-chat-action="fetch-pdf"]')).to_be_disabled()
    page.evaluate("document.querySelector('[data-read=\"fetch-pdf\"]').click()")
    page.locator('[data-read="toggle-toolbar"]').click()
    playwright.expect(page.locator('.reader-status')).to_be_visible()
    assert len(pending) == 1
    pending[0].fulfill(status=400, json={'message': '未取得公开 PDF，请通过学校图书馆下载后上传 PDF。'})
    playwright.expect(page.locator('.reader-status')).to_contain_text('请通过学校图书馆')
    playwright.expect(page.locator('.reader-status')).to_have_class(re.compile(r'\breader-error\b'))
    playwright.expect(page.locator('.reader-empty')).to_be_visible()
    page.locator('[data-read="toggle-toolbar"]').click()
    playwright.expect(page.locator('[data-read="fetch-pdf"]')).to_be_enabled()
    playwright.expect(page.locator('[data-read="fetch-pdf"]')).to_have_text('获取 PDF')


def test_fetch_failure_preserves_existing_pdf_and_cancel_preserves_draft(reader):
    page, context, _, _, original, _, saved, requests, _ = reader
    note(page, 'Keep this draft')
    pending = []
    context.route('**/fetch-pdf', lambda route: pending.append(route))
    page.locator('[data-read="fetch-pdf"]').click()
    page.locator('[data-leave="cancel"]').click()
    playwright.expect(page.locator('.reader-note textarea')).to_have_value('Keep this draft')
    assert pending == []
    page.locator('[data-read="save"]').click()
    playwright.expect(page.locator('[data-read="save"]')).to_be_disabled()
    page.locator('[data-read="fetch-pdf"]').click()
    playwright.expect(page.locator('.reader-status')).to_contain_text('正在获取')
    pending[0].fulfill(status=400, json={'message': '出版社暂不允许下载，请上传 PDF。'})
    playwright.expect(page.locator('.reader-status')).to_contain_text('出版社暂不允许下载')
    playwright.expect(page.locator('.reader-version')).to_have_value(original['hash'])
    playwright.expect(page.locator('.reader-note textarea')).to_have_value('Keep this draft')
    assert saved()['revision'] == 1


def test_fetch_success_loads_pdf_and_mobile_feedback_is_visible(reader):
    page, context, client, headers, original, *_ = reader
    empty_reader(page, context, client, headers)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.locator('[data-tab="pdf"]').click()
    pending = []
    context.route('**/fetch-pdf', lambda route: pending.append(route))
    page.locator('[data-read="fetch-pdf"]').click()
    playwright.expect(page.locator('.reader-status')).to_contain_text('正在获取')
    assert page.locator('.reader-header').evaluate('(el) => el.scrollWidth <= el.clientWidth')
    context.unroute(f'**/api/papers/{P1}')
    pending[0].fulfill(json={'message': '已获取并载入论文 PDF。'})
    playwright.expect(page.locator('.reader-page').first).to_be_visible()
    playwright.expect(page.locator('.reader-status')).to_have_text('已获取并载入论文 PDF。')
    playwright.expect(page.locator('.reader-version')).to_have_value(original['hash'])
    playwright.expect(page.locator('[data-read="fetch-pdf"]')).to_be_enabled()


def test_fetch_timeout_keeps_polling_without_submitting_again(reader):
    page, context, client, headers, original, *_ = reader
    empty_reader(page, context, client, headers)
    data = client.get(f'/api/papers/{P1}', headers=headers).json()
    pending_data = dict(data, document=None, pdf_versions=[], reading=None, preparing=True)
    context.unroute(f'**/api/papers/{P1}')
    context.route(f'**/api/papers/{P1}', lambda route: route.fulfill(json=pending_data))
    page.evaluate('''() => {
      const originalFetch = window.fetch;
      window.pdfFetchAttempts = 0;
      window.fetch = (url, options) => {
        if (String(url).endsWith('/fetch-pdf')) {
          window.pdfFetchAttempts++;
          return Promise.reject(new DOMException('Timed out', 'TimeoutError'));
        }
        return originalFetch(url, options);
      };
    }''')
    page.locator('[data-read="fetch-pdf"]').click()
    playwright.expect(page.locator('.reader-status')).to_have_text('本机正在获取或解析资料，请稍候…')
    playwright.expect(page.locator('[data-read="fetch-pdf"]')).to_be_disabled()
    pending_data.update(data, preparing=False)
    playwright.expect(page.locator('.reader-page').first).to_be_visible(timeout=10000)
    playwright.expect(page.locator('.reader-status')).to_contain_text('已载入 PDF')
    playwright.expect(page.locator('[data-read="fetch-pdf"]')).to_be_enabled()
    assert page.evaluate('window.pdfFetchAttempts') == 1


def test_fetch_network_failure_is_shown_in_reader(reader):
    page, context, *_ = reader
    context.route('**/fetch-pdf', lambda route: route.abort('connectionreset'))
    page.locator('[data-read="fetch-pdf"]').click()
    playwright.expect(page.locator('.reader-status')).to_contain_text('无法连接本机')
    playwright.expect(page.locator('[data-read="fetch-pdf"]')).to_be_enabled()
    playwright.expect(page.locator('.reader-page').first).to_be_visible()


def test_successful_acquisition_does_not_hide_pdf_viewer_failure(reader):
    page, context, client, headers, *_ = reader
    empty_reader(page, context, client, headers)
    context.route('**/pdf?*', lambda route: route.fulfill(status=500, json={'message': 'PDF 文件不可读取'}))
    pending = []
    context.route('**/fetch-pdf', lambda route: pending.append(route))
    page.locator('[data-read="fetch-pdf"]').click()
    playwright.expect(page.locator('.reader-status')).to_contain_text('正在获取')
    context.unroute(f'**/api/papers/{P1}')
    pending[0].fulfill(json={'message': '已获取并载入论文 PDF。'})
    playwright.expect(page.locator('.reader-status')).to_contain_text('PDF 加载失败：PDF 文件不可读取')
    playwright.expect(page.locator('[data-read="fetch-pdf"]')).to_be_enabled()


def test_uploaded_pdf_title_is_used_in_reader_and_download(reader):
    import json
    from tests.test_pdf_annotations import source_pdf
    page, _, _, _, original, alternative, _, _, root = reader
    title = ('Prediction of residual life and critical crack length using the forward/inverse '
             'machine learning based on the configurational force fatigue model')
    path = root / 'data/daily.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    data['core'][0]['title'] = title
    path.write_text(json.dumps(data), encoding='utf-8')
    with page.expect_file_chooser() as chooser:
        page.locator('[data-read="upload"]').click()
    chooser.value.set_files({'name': '1-s2.0-paper-main (1).pdf', 'mimeType': 'application/pdf', 'buffer': source_pdf()})
    playwright.expect(page.locator('.reader-name')).to_have_text(title)
    playwright.expect(page.locator('[data-read="download"]')).to_be_enabled()
    with page.expect_download() as download:
        page.locator('[data-read="download"]').click()
    assert download.value.suggested_filename == title.replace('/', '_') + '.pdf'
    # Internal version changes and metadata refreshes retain the unsanitized title.
    for version in (alternative['hash'], original['hash']):
        page.locator('.reader-version').select_option(version)
        playwright.expect(page.locator('.reader-version')).to_be_enabled()
        playwright.expect(page.locator('.reader-name')).to_have_text(title)
    for width in (1920, 1440, 1024, 390, 320):
        page.set_viewport_size({'width': width, 'height': 1000})
        if width < 860:
            page.locator('[data-tab="pdf"]').click()
        for _ in range(2):
            page.locator('[data-read="toggle-toolbar"]').click()
            playwright.expect(page.locator('.reader-name')).to_be_visible()
            assert page.locator('.reader-title-row').evaluate('(el) => el.scrollWidth <= el.clientWidth')
            assert page.locator('.reader-name').evaluate('''el => {
                const range=document.createRange();range.selectNodeContents(el);
                const box=el.getBoundingClientRect();
                return [...range.getClientRects()].every(r => r.left>=box.left-1 && r.right<=box.right+1 && r.bottom<=box.bottom+1);
            }''')
            for action in ('toggle-toolbar', 'hide'):
                box = page.locator(f'[data-read="{action}"]').bounding_box()
                assert 0 <= box['x'] and box['x'] + box['width'] <= width
        if width <= 1440:
            assert page.locator('.reader-name').evaluate('el => el.clientHeight > parseFloat(getComputedStyle(el).lineHeight)')
    # The cached title must never leak into another paper's reader.
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.locator('.chat-close').click()
    page.locator(f'.codex-entry[data-paper-id="{P2}"]').click()
    playwright.expect(page.locator('.reader-name')).to_have_text('Paper B')
    page.locator('.chat-close').click()
    page.locator(f'.codex-entry[data-paper-id="{P1}"]').click()
    playwright.expect(page.locator('.reader-name')).to_have_text(title)


def test_uploaded_original_figure_refreshes_core_card_and_opens_large_view(reader):
    import json
    from tests.test_figure_collection import DOI, pdf
    page, _, _, _, _, _, _, _, root = reader
    data = json.loads((root / 'data/daily.json').read_text(encoding='utf-8'))
    data['core'][0]['doi'] = DOI
    (root / 'data/daily.json').write_text(json.dumps(data), encoding='utf-8')
    with page.expect_file_chooser() as chooser:
        page.locator('[data-read="upload"]').click()
    chooser.value.set_files({'name': 'original.pdf', 'mimeType': 'application/pdf', 'buffer': pdf()})
    playwright.expect(page.locator('.reader-status')).to_contain_text('PDF 已就绪')
    page.locator('.chat-close').click()
    figure = page.locator(f'.paper[data-paper-id="{P1}"] .local-paper-figure')
    playwright.expect(figure).to_be_visible()
    playwright.expect(figure.locator('.figure-credit')).to_contain_text('仅本机连接可见')
    figure.locator('img').evaluate('(img) => img.decode()')
    assert figure.locator('img').get_attribute('src').startswith('blob:')
    assert page.locator('#extended .paper-figure').count() == 0
    figure.locator('.figure-preview').click()
    playwright.expect(page.locator('.figure-dialog')).to_be_visible()
    playwright.expect(page.locator('.figure-dialog-image')).to_be_visible()
    dimensions = figure.locator('img').evaluate('(img) => [img.naturalWidth,img.naturalHeight]')
    page.locator('.figure-dialog-rotate').click()
    page.locator('.figure-dialog-image').evaluate('(img) => img.decode()')
    assert page.locator('.figure-dialog-image').evaluate('(img) => [img.naturalWidth,img.naturalHeight]') == dimensions[::-1]
    page.keyboard.press('Escape')
    page.set_viewport_size({'width': 390, 'height': 844})
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
