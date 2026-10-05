"""Polite fetching: one request per second per website, robots.txt respected, size limits."""
import io
import threading
import time
import urllib.robotparser
from urllib.parse import urlparse, urljoin

import requests

UA = "Mozilla/5.0 (compatible; TXPlanFinder/1.0; electricity plan price comparison)"
MAX_BYTES = 6_000_000
_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept": "text/html,application/pdf,text/csv,application/json,*/*"})
_host_lock = {}
_host_last = {}
_robots = {}
_glock = threading.Lock()


def _throttle(host, gap=1.0):
    with _glock:
        lock = _host_lock.setdefault(host, threading.Lock())
    with lock:
        wait = _host_last.get(host, 0) + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _host_last[host] = time.time()


def allowed(url):
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    with _glock:
        rp = _robots.get(base)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = _session.get(base + "/robots.txt", timeout=10)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:
            rp.parse([])
        with _glock:
            _robots[base] = rp
    return rp.can_fetch(UA, url)


def get(url, gap=1.0, retries=2):
    """Returns (content_type, bytes, final_url, status) or None."""
    if not url or not url.startswith("http"):
        return None
    if not allowed(url):
        return ("blocked-by-robots", b"", url, 0)
    host = urlparse(url).netloc
    for attempt in range(retries + 1):
        _throttle(host, gap)
        try:
            r = _session.get(url, timeout=25, stream=True, allow_redirects=True)
            data = r.raw.read(MAX_BYTES, decode_content=True)
            ctype = r.headers.get("Content-Type", "").lower()
            if r.status_code in (429, 503) and attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            return (ctype, data, r.url, r.status_code)
        except Exception:
            if attempt == retries:
                return None
            time.sleep(2)
    return None


def pdf_text(data):
    import logging
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join((pg.extract_text() or "") for pg in pdf.pages[:4])
    except Exception:
        try:
            from pypdf import PdfReader
            return "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(data)).pages[:4])
        except Exception:
            return ""


def html_text_and_links(data, base_url):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(data, "html.parser")
    links = []
    for tag in soup.find_all(["a", "iframe", "embed", "object"]):
        href = tag.get("href") or tag.get("src") or tag.get("data")
        if href and not href.startswith(("mailto:", "tel:", "javascript:", "#")):
            links.append((urljoin(base_url, href), (tag.get_text(" ") or "").strip()[:80]))
    for s in soup(["script", "style", "noscript"]):
        s.decompose()
    return soup.get_text("\n"), links


def document_text(url):
    """Text of an EFL, whether it's served as a PDF or as a web page (following one embed)."""
    res = get(url)
    if not res or res[3] >= 400 or not res[1]:
        return None, res[3] if res else None
    ctype, data, final, status = res
    if data[:5] == b"%PDF-":
        return pdf_text(data), status
    if data.lstrip()[:1] in (b"{", b"["):
        return None, status  # an error message in JSON, not a label
    if "html" in ctype or data.lstrip()[:1] == b"<":
        text, links = html_text_and_links(data, final)
        if "facts label" in text.lower() and "average" in text.lower():
            return text, status
        for href, _ in links:
            if ".pdf" in href.lower() or "efl" in href.lower():
                sub = get(href)
                if sub and sub[1][:5] == b"%PDF-":
                    return pdf_text(sub[1]), status
        return text, status
    return None, status
