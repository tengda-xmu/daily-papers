"""Document preparation happens outside the model, with no arbitrary file tools."""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path
import re
import socket
from urllib.parse import urljoin, urlsplit

import requests

MAX_BYTES = 50 * 1024 * 1024
MAX_PAGES = 300
MAX_TEXT = 1_500_000
PUBLIC_HOSTS = {"www.nature.com", "nature.com", "idp.nature.com", "arxiv.org", "export.arxiv.org",
                "pmc.ncbi.nlm.nih.gov", "www.science.org", "www.cell.com",
                "www.sciencedirect.com", "link.springer.com", "www.frontiersin.org",
                "journals.plos.org", "api.elsevier.com"}


def validate_url(url):
    p = urlsplit(url)
    if p.scheme != "https" or p.username or p.password or p.port not in (None, 443) or p.hostname not in PUBLIC_HOSTS:
        raise ValueError("此全文地址暂不支持自动提取，请上传合法取得的 PDF。")
    answers = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
    if not answers or any(not ipaddress.ip_address(a[4][0]).is_global for a in answers):
        raise ValueError("全文地址没有解析到公共网络。")


def download(url):
    session = requests.Session()
    # Nature's public article redirect sets a short-lived first-party cookie.
    # Keep it only for this download; no browser or account login is imported.
    for _ in range(5):
        validate_url(url)
        with session.get(url, timeout=(10, 35), stream=True, allow_redirects=False,
                          headers={"User-Agent": "DailyPapers-Reader/1.0 (personal scholarly reading)"}) as r:
            if r.is_redirect:
                url = urljoin(url, r.headers["Location"])
                continue
            if r.status_code in (401, 403, 429):
                raise ValueError("出版社暂不允许自动读取，请上传 PDF；不会绕过登录或访问限制。")
            r.raise_for_status()
            content = bytearray()
            for chunk in r.iter_content(65536):
                content.extend(chunk)
                if len(content) > MAX_BYTES:
                    raise ValueError(f"全文文件超过 {MAX_BYTES // (1024 * 1024)} MB，请使用较小的 PDF。")
            return bytes(content), url
    raise ValueError("全文地址重定向次数过多。")


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_article = 0
        self.skip = 0
        self.block = None
        self.buffer = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("article", "main"):
            self.in_article += 1
        if tag in ("script", "style", "nav", "footer", "noscript"):
            self.skip += 1
        if self.in_article and not self.skip and tag in ("p", "h1", "h2", "h3", "h4", "figcaption") and not self.block:
            self.block = tag
            self.buffer = []

    def handle_endtag(self, tag):
        if tag == self.block:
            text = re.sub(r"\s+", " ", "".join(self.buffer)).strip()
            if text and text not in self.parts:
                self.parts.append(text)
            self.block = None
        if tag in ("article", "main"):
            self.in_article = max(0, self.in_article - 1)
        if tag in ("script", "style", "nav", "footer", "noscript"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if self.block and not self.skip:
            self.buffer.append(data)


def parse_pdf(content: bytes, directory: Path, name="上传 PDF"):
    from pypdf import PdfReader
    if not content.startswith(b"%PDF-"):
        raise ValueError("文件不是有效的 PDF。")
    digest = hashlib.sha256(content).hexdigest()[:16]
    path = directory / f"{digest}.pdf"
    path.write_bytes(content)
    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF 已加密，请上传可直接打开的版本。")
        if not reader.pages:
            raise ValueError("PDF 没有可读取的页面。")
        if len(reader.pages) > MAX_PAGES:
            raise ValueError(f"PDF 超过 {MAX_PAGES} 页，请按章节拆分上传。")
        pages = []
        for i, page in enumerate(reader.pages):
            text = (page.extract_text() or "").strip()
            pages.append({"label": f"P{i + 1}", "text": text, "scan": len(text) < 60})
        if sum(len(p["text"]) for p in pages) > MAX_TEXT:
            raise ValueError("PDF 文字过多，请按章节拆分上传。")
        return {"kind": "pdf", "name": name[:120], "hash": digest, "file": path.name,
                "pages": pages, "page_count": len(pages), "scan_pages": sum(p["scan"] for p in pages)}
    except Exception:
        path.unlink(missing_ok=True)
        raise


def fetch_fulltext(paper, directory):
    url = paper.get("oa_url")
    if not url:
        raise ValueError("这篇论文没有已知的开放全文地址，请上传 PDF。")
    if urlsplit(url).hostname == "arxiv.org" and "/abs/" in url:
        url = url.replace("/abs/", "/pdf/")
    content, url = download(url)
    if content.startswith(b"%PDF-"):
        result = parse_pdf(content, directory, "开放全文 PDF")
    else:
        parser = ArticleParser()
        parser.feed(content.decode("utf-8", errors="replace"))
        parts = parser.parts
        text = "\n".join(parts)
        # Do not label a login page, a teaser or an abstract alone as full text.
        if len(text) < 6000 or not re.search(r"methods|results|discussion|conclusion|方法|结果", text, re.I):
            raise ValueError("页面未返回可确认的完整正文，请上传 PDF；当前仍按摘要分析。")
        if len(text) > MAX_TEXT:
            raise ValueError("全文过长，请上传所需章节的 PDF。")
        result = {"kind": "html", "name": "开放全文（网页）", "hash": hashlib.sha256(content).hexdigest()[:16],
                  "pages": [{"label": f"S{i + 1}", "text": part, "scan": False} for i, part in enumerate(parts)],
                  "page_count": 0, "scan_pages": 0}
    result["url"] = url
    return result


class PDFLinkParser(HTMLParser):
    """Only publisher-declared article PDF links, not arbitrary PDF attachments."""
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name", "").lower() == "citation_pdf_url":
            if attrs.get("content"):
                self.links.append(attrs["content"])


def pdf_candidates(paper):
    urls = [paper.get(k) for k in ("pdf_url", "oa_url", "landing_url")]
    doi = str(paper.get("doi") or "").lower()
    if re.fullmatch(r"10\.1038/[a-z0-9.-]+", doi):
        urls.append("https://www.nature.com/articles/" + doi.split("/", 1)[1])
    arxiv = re.fullmatch(r"10\.48550/arxiv\.(\d{4}\.\d{4,5}(?:v\d+)?)", doi)
    if arxiv:
        urls.append("https://arxiv.org/abs/" + arxiv[1])
    candidates = []
    for url in urls:
        if not url:
            continue
        p = urlsplit(url)
        if p.scheme != "https" or p.hostname not in PUBLIC_HOSTS or p.username or p.password or p.port not in (None, 443):
            continue
        if p.hostname in ("arxiv.org", "export.arxiv.org") and p.path.startswith(("/abs/", "/html/", "/pdf/")):
            identifier = p.path.split("/", 2)[2].removesuffix(".pdf")
            candidates.append("https://arxiv.org/pdf/" + identifier)
        elif p.hostname in ("nature.com", "www.nature.com") and re.fullmatch(r"/articles/[a-zA-Z0-9.-]+", p.path):
            candidates.append("https://www.nature.com" + p.path + ("" if p.path.endswith(".pdf") else ".pdf"))
        candidates.append(url)
    return list(dict.fromkeys(candidates))


def fetch_pdf(paper, directory):
    candidates = pdf_candidates(paper)
    if not candidates:
        raise ValueError("这篇论文暂无可直接获取的公开 PDF 地址，请点击“上传 PDF”。")
    visited = set()
    while candidates and len(visited) < 4:
        url = candidates.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            content, final_url = download(url)
            if content.startswith(b"%PDF-"):
                result = parse_pdf(content, directory, "论文 PDF")
                result["url"] = final_url
                return result
            parser = PDFLinkParser()
            parser.feed(content.decode("utf-8", errors="replace"))
            # download() validates each discovered URL and every redirect.
            candidates[:0] = [urljoin(final_url, link) for link in parser.links[:2]]
        except (ValueError, requests.RequestException):
            continue
    raise ValueError("未取得可读取的公开 PDF（可能需要机构访问或出版社暂不允许下载）。请上传 PDF；已有资料保持可用。")


def page_selection(value, count):
    if not value.strip():
        return []
    selected = set()
    for part in value.replace("，", ",").split(","):
        match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", part)
        if not match:
            raise ValueError("页码请填写 1-3,5 这样的格式。")
        first, last = int(match[1]), int(match[2] or match[1])
        if not 1 <= first <= last <= count:
            raise ValueError("指定页码超出 PDF 范围。")
        selected.update(range(first, last + 1))
    return sorted(selected)


def render_scan(document, directory, page_numbers, *, detail=False):
    import pypdfium2 as pdfium
    result = []
    with pdfium.PdfDocument(directory / document["file"]) as pdf:
        for number in page_numbers:
            if detail:
                page = pdf[number - 1]
                image = page.render(scale=3).to_pil()
                page.close()
                width, height = image.size
                for part, box in enumerate(((0, 0, width, int(height * .56)), (0, int(height * .44), width, height))):
                    dest = directory / f'{document["hash"]}-p{number}-detail-{part}.png'
                    if not dest.exists():
                        image.crop(box).save(dest)
                    result.append(dest)
                continue
            dest = directory / f'{document["hash"]}-p{number}.png'
            if not dest.exists():
                page = pdf[number - 1]
                image = page.render(scale=1.5).to_pil()
                image.thumbnail((1800, 1800))
                image.save(dest)
                page.close()
            result.append(dest)
    return result


def source_context(paper):
    metadata = {k: paper.get(k) for k in ("title", "title_zh", "authors", "venue", "doi", "published_at", "landing_url")}
    raw = paper
    editorial = False
    for _ in range(6):
        editorial = editorial or "editorial" in str(raw.get("abstract_kind", ""))
        raw = raw.get("raw_metadata") or {}
    label = "检索简介（非出版社原文摘要）" if editorial else "摘要"
    return (json.dumps(metadata, ensure_ascii=False) + f"\n[{label}]\n" + str(paper.get("abstract") or "未取得摘要")
            + "\n[本站解读，不能作为论文原始实验依据]\n" + json.dumps({k: paper.get(k) for k in ("summary", "deep_read")}, ensure_ascii=False))


def reading_batches(document, question, mode, pages=""):
    if not document:
        return [{"text": "尚未获取全文，仅可基于所给摘要及本站解读回答。", "scans": []}]
    selected = page_selection(pages, document["page_count"]) if document["kind"] == "pdf" else []
    parts = document["pages"]
    if selected:
        parts = [p for p in parts if int(p["label"][1:]) in selected]
    elif mode not in ("summary",) and (sum(len(p["text"]) for p in parts) > 90000 or document["scan_pages"] > 4):
        if document["scan_pages"]:
            raise ValueError("扫描 PDF 提问或翻译时，请先指定相关页码；“总结论文”会逐批覆盖所有页。")
        terms = set(re.findall(r"[a-zA-Z]{3,}|[\u4e00-\u9fff]{2,4}", question.lower()))
        ranked = sorted(enumerate(parts), key=lambda x: sum(t in x[1]["text"].lower() for t in terms), reverse=True)[:20]
        parts = [p for _, p in sorted(ranked)]
    batches, text, scans = [], "", []
    for part in parts:
        if (text and len(text) + len(part["text"]) > 60000) or len(scans) >= 4:
            batches.append({"text": text, "scans": scans})
            text, scans = "", []
        if part["scan"]:
            scans.append(int(part["label"][1:]))
            text += f'\n[{part["label"]}] 见按此顺序附上的扫描页图片。\n'
        else:
            text += f'\n[{part["label"]}]\n{part["text"]}\n'
    if text or scans:
        batches.append({"text": text, "scans": scans})
    return batches or [{"text": "资料为空，不能进行全文分析。", "scans": []}]
