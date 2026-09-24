"""Vendor the pinned official PDF.js distribution for private local PDF viewing."""
from pathlib import Path
import hashlib
import io
import json
import base64
import urllib.request
import zipfile

VERSION = '6.3.289'
ARCHIVE_SHA256 = '98c5832ffe7af4edd59853476a478c0d4d4d76dd49c1701f4c86f7182725cdf9'
CSS_SHA256 = '5b21bcb7d703cae5b8eebf9c8579435409aaf351b62e7518327ada60bdd0946f'
URL = f'https://github.com/mozilla/pdf.js/releases/download/v{VERSION}/pdfjs-{VERSION}-dist.zip'
ROOT = Path(__file__).resolve().parents[1]


def main():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(URL, timeout=60) as response:
        content = response.read(40 * 1024 * 1024)
    if hashlib.sha256(content).hexdigest() != ARCHIVE_SHA256:
        raise ValueError('PDF.js archive checksum mismatch')
    target = ROOT/'tools/assets/vendor/pdfjs'
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for name in archive.namelist():
            if name.endswith('/') or not (name in ('build/pdf.mjs', 'build/pdf.worker.mjs', 'web/pdf_viewer.css', 'LICENSE') or name.startswith(('web/cmaps/', 'web/standard_fonts/', 'web/wasm/'))):
                continue
            path = (target/name).resolve()
            if not path.is_relative_to(target.resolve()):
                raise ValueError('Invalid archive path')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(name))
            count += 1
    css_source=f'https://api.github.com/repos/mozilla/pdf.js/contents/web/text_layer_builder.css?ref=v{VERSION}'
    with opener.open(css_source, timeout=30) as response:
        css=base64.b64decode(json.load(response)['content'])
    if hashlib.sha256(css).hexdigest() != CSS_SHA256:
        raise ValueError('PDF.js stylesheet checksum mismatch')
    (target/'text_layer.css').write_bytes(css)
    (target/'version.json').write_text(json.dumps({'version':VERSION, 'source':URL, 'sha256':hashlib.sha256(content).hexdigest(), 'text_layer_source':css_source, 'text_layer_sha256':hashlib.sha256(css).hexdigest()}, indent=2)+'\n', encoding='utf-8')
    print(f'Vendored PDF.js {VERSION}: {count} files')


if __name__ == '__main__':
    main()
