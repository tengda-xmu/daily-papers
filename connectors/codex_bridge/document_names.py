"""Paper-based download names, independent of content-addressed storage paths."""
import re
import unicodedata


def paper_pdf_name(paper):
    title = str(paper.get('title') or '').strip() or str(paper.get('title_zh') or '').strip()
    title = unicodedata.normalize('NFC', title)
    title = re.sub(r'\s+', ' ', title)
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', title).strip(' .')
    title = re.sub(r'\.pdf$', '', title, flags=re.I).rstrip(' .')
    # Leave room for the extension and the annotated-export suffix on common filesystems.
    while len(title.encode('utf-8')) > 230:
        title = title[:-1]
    title = title.rstrip(' .') or ('论文-' + str(paper.get('id') or '未命名'))
    if re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]', title.split('.')[0], re.I):
        title = '论文-' + title
    return title + '.pdf'


def name_original_pdf(document, paper):
    if not document or document.get('kind') != 'pdf' or document.get('view', 'original') != 'original':
        return document
    result = dict(document)
    result.setdefault('original_name', document.get('name', ''))
    result['name'] = paper_pdf_name(paper)
    return result
