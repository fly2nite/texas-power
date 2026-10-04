"""Finding facts labels without Power to Choose.

1. Site crawl: walk each provider's own website (sitemap + links that look like plan
   pages) and collect every link that looks like a facts label.
2. Neighbor probing: many providers number their labels in sequence
   (...productId=46065, .../products/2046/EFL/...). For every label we know, try the
   nearby numbers. Whatever answers with a valid current label is a plan, listed
   anywhere or not.
"""
import re
from collections import deque
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from fetch import get, html_text_and_links

EFL_HINT = re.compile(r"efl|facts?[-_ ]?label|electricity[-_ ]facts|factsheet|fact[-_]sheet|/facts", re.I)
PAGE_HINT = re.compile(r"plan|rate|electric|residential|home|price|shop|texas|oncor|centerpoint|aep|tnmp|lubbock|zip|sitemap", re.I)
SKIP = re.compile(r"\.(jpg|jpeg|png|gif|svg|webp|mp4|css|js|ico|zip|woff2?)(\?|$)|/(blog|news|careers|press|login|account|signin)/", re.I)


def _domain(url):
    host = urlparse(url).netloc.lower()
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _sitemap_urls(site):
    urls = []
    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        res = get(site.rstrip("/") + path)
        if not res or res[3] != 200:
            continue
        locs = re.findall(rb"<loc>\s*([^<\s]+)\s*</loc>", res[1])
        for loc in locs[:400]:
            u = loc.decode(errors="ignore")
            if u.endswith(".xml") and len(urls) < 800:
                sub = get(u)
                if sub and sub[3] == 200:
                    urls += [x.decode(errors="ignore") for x in re.findall(rb"<loc>\s*([^<\s]+)\s*</loc>", sub[1])[:400]]
            else:
                urls.append(u)
    return urls


def crawl_site(provider, max_pages=60):
    """Returns ([efl_urls], pages_visited)."""
    site = provider["site"]
    dom = _domain(site)
    seeds = [site] + [site.rstrip("/") + p for p in provider.get("seed_paths", [])]
    seeds += [u for u in _sitemap_urls(site) if PAGE_HINT.search(u) or EFL_HINT.search(u)][:150]
    queue, seen, efls, visited = deque(seeds), set(), set(provider.get("efl_urls", [])), 0
    while queue and visited < max_pages:
        url = queue.popleft()
        if url in seen or SKIP.search(url):
            continue
        seen.add(url)
        if EFL_HINT.search(url) or url.lower().endswith(".pdf"):
            if EFL_HINT.search(url):
                efls.add(url)
            continue
        res = get(url)
        visited += 1
        if not res or res[3] >= 400 or "html" not in (res[0] or ""):
            continue
        _, links = html_text_and_links(res[1], res[2])
        for href, text in links:
            if _domain(href) != dom and not EFL_HINT.search(href + " " + text):
                continue
            if EFL_HINT.search(href) or (EFL_HINT.search(text) and ("pdf" in href.lower() or "efl" in href.lower() or "fact" in href.lower())):
                efls.add(href)
            elif PAGE_HINT.search(href) and href not in seen and _domain(href) == dom:
                queue.append(href)
    return sorted(efls), visited


def neighbor_templates(url):
    """Yield functions that rebuild the URL with a different number in one numeric slot."""
    p = urlparse(url)
    qs = parse_qsl(p.query, keep_blank_values=True)
    for i, (k, v) in enumerate(qs):
        if re.fullmatch(r"\d{3,9}", v):
            def mk(n, i=i, k=k):
                q = list(qs)
                q[i] = (k, str(n))
                return urlunparse(p._replace(query=urlencode(q)))
            yield int(v), mk, f"{p.netloc}{p.path}?{k}="
    segs = p.path.split("/")
    for i, s in enumerate(segs):
        if re.fullmatch(r"\d{3,9}", s):
            def mk(n, i=i):
                s2 = list(segs)
                s2[i] = str(n)
                return urlunparse(p._replace(path="/".join(s2)))
            yield int(s), mk, f"{p.netloc}/" + "/".join(segs[:i]) + "/#/" + "/".join(segs[i + 1:])


def probe_candidates(known_urls, radius=25, per_template_cap=120):
    """From every known label address, list the nearby numbered addresses to try."""
    by_tpl = {}
    for u in known_urls:
        for n, mk, key in neighbor_templates(u):
            ent = by_tpl.setdefault(key, {"nums": set(), "mk": mk})
            ent["nums"].add(n)
    out = []
    for key, ent in by_tpl.items():
        nums = ent["nums"]
        cand = set()
        for n in nums:
            for d in range(-radius, radius + 1):
                if n + d > 0 and n + d not in nums:
                    cand.add(n + d)
        # Closest to known numbers first, then cap per address pattern.
        ranked = sorted(cand, key=lambda x: min(abs(x - n) for n in nums))[:per_template_cap]
        out += [(key, ent["mk"](x)) for x in ranked]
    return out
