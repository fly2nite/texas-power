"""Daily crawl. Writes data/plans.json (what the website reads) and data/crawl_report.json
(what happened, so broken sources are easy to spot).

Run locally:  python crawler/run.py
"""
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(__file__))
import efl_parser
import ptc
from discover import crawl_site, probe_candidates, _domain
from fetch import document_text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CACHE_DAYS = 3          # re-read a label at most this often
PROBE_MAX_AGE = 150     # labels found only by probing must be at most this many days old
TIME_LIMIT = 50 * 60    # stop discovering new things after 50 minutes
START = time.time()


def load(name, default):
    try:
        with open(os.path.join(DATA, name)) as f:
            return json.load(f)
    except Exception:
        return default


def save(name, obj):
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, name), "w") as f:
        json.dump(obj, f, indent=1 if name.startswith("crawl") else None, separators=None if name.startswith("crawl") else (",", ":"))


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def main():
    report = {"started": datetime.now(timezone.utc).isoformat(), "providers": {}, "errors": []}
    cache = load("efl_cache.json", {})
    today = date.today()

    def parse_url(url):
        ent = cache.get(url)
        if ent and (today - date.fromisoformat(ent["read"])).days < CACHE_DAYS:
            return ent["result"]
        text, status = document_text(url)
        result = efl_parser.parse(text) if text else None
        cache[url] = {"read": today.isoformat(), "status": status, "result": result,
                      "hash": hashlib.md5((text or "").encode()).hexdigest()[:10]}
        return result

    # 1. Power to Choose (one source among several)
    ptc_rows = ptc.fetch_plans(report)
    candidates = {}  # efl url -> info
    for r in ptc_rows:
        if r["efl"]:
            candidates.setdefault(r["efl"], {"sources": set(), "ptc": None})
            candidates[r["efl"]]["sources"].add("ptc")
            candidates[r["efl"]]["ptc"] = r

    # 2. Provider websites: the registry plus every provider site seen on Power to Choose
    reg = load_registry()
    sites = {_domain(p["site"]): p for p in reg}
    for r in ptc_rows:
        for u in (r["enroll"], r["efl"]):
            if u and u.startswith("http"):
                d = _domain(u)
                if d not in sites and "powertochoose" not in d:
                    sites[d] = {"name": r["provider"], "site": f"https://{urlparse(u).netloc}", "seed_paths": [], "efl_urls": []}
                break

    def crawl(p):
        if time.time() - START > TIME_LIMIT * 0.4:
            return p, [], 0
        try:
            return (p,) + crawl_site(p)
        except Exception as e:
            report["errors"].append(f"crawl {p['site']}: {e}")
            return p, [], 0

    with ThreadPoolExecutor(10) as ex:
        for p, efls, pages in ex.map(crawl, sites.values()):
            report["providers"][p["name"]] = {"site": p["site"], "pages": pages, "labels_on_site": len(efls)}
            for u in efls:
                candidates.setdefault(u, {"sources": set(), "ptc": None, "site": p["site"]})
                candidates[u]["sources"].add("site")
                candidates[u].setdefault("site", p["site"])

    # 3. Read every label found so far
    urls = list(candidates)
    with ThreadPoolExecutor(12) as ex:
        results = dict(zip(urls, ex.map(lambda u: _safe(parse_url, u, report), urls)))

    # 4. Probe nearby label addresses for plans nobody lists
    good = [u for u, r in results.items() if r]
    probes = probe_candidates(good) if time.time() - START < TIME_LIMIT * 0.6 else []
    probes = [(k, u) for k, u in probes if u not in candidates]
    report["probe_attempts"] = len(probes)
    hits_by_tpl = {}

    def probe(item):
        key, u = item
        if time.time() - START > TIME_LIMIT:
            return u, None
        return u, _safe(parse_url, u, report)

    with ThreadPoolExecutor(12) as ex:
        for u, r in ex.map(probe, probes):
            if r:
                results[u] = r
                candidates[u] = {"sources": {"probe"}, "ptc": None}
                hits_by_tpl[urlparse(u).netloc] = hits_by_tpl.get(urlparse(u).netloc, 0) + 1
    report["probe_hits"] = hits_by_tpl

    # 5. Build plan records
    plans = []
    for url, info in candidates.items():
        res, row = results.get(url), info.get("ptc")
        if res:
            if "probe" in info["sources"]:
                d = res.get("efl_date")
                if not d or (today - date.fromisoformat(d)).days > PROBE_MAX_AGE:
                    continue
            plan = dict(res)
            plan["efl"] = url
            plan["sources"] = sorted(info["sources"])
            if row:
                if row["provider"] and not (row["provider"].isupper() and plan["provider"]):
                    plan["provider"] = row["provider"]
                plan["product"] = row["product"] or plan["product"]
                plan["tdu"] = plan["tdu"] or row["tdu"]
                plan["enroll"] = row["enroll"]
                plan["new_customers_only"] = row["new_customers_only"]
                plan["prepaid"] = plan["prepaid"] or row["prepaid"]
                plan["type"] = plan["type"] or row["type"]
                plan["term"] = plan["term"] or row["term"]
                if row["tou"] and "time of use" not in plan["gimmicks"]:
                    plan["gimmicks"].append("time of use")
            else:
                plan["enroll"] = info.get("site") or f"https://{urlparse(url).netloc}"
            plans.append(plan)
        elif row and row["averages"]:
            g = []
            if row["tou"]: g.append("time of use")
            if row["usage_terms"]: g.append("bill credit or minimum fee")
            pts = row["averages"]
            m = efl_parser.linear_fit(pts) if not g else None
            if row["provider"] and row["provider"].isupper():
                row["provider"] = row["provider"].title()
            plans.append({**{k: row[k] for k in ("provider", "product", "tdu", "type", "term", "prepaid", "renewable",
                                                 "cancel_fee", "enroll", "new_customers_only")},
                          "efl": url, "sources": ["ptc"], "averages": pts, "efl_date": None,
                          "model": m or {"points": pts}, "method": "linear" if m else "points",
                          "gimmicks": g or ([] if m else ["non-standard pricing"])})

    # 6. Merge duplicates (same plan found several ways): keep the best-read, newest version
    rank = {"parsed": 0, "linear": 1, "points": 2}
    best = {}
    for p in plans:
        if not p.get("tdu") or not p.get("provider"):
            continue
        k = (norm(p["provider"])[:12], norm(p["product"]), p["tdu"])
        cur = best.get(k)
        if cur is None or (rank[p["method"]], -(int((p.get("efl_date") or "0").replace("-", "")))) < \
                (rank[cur["method"]], -(int((cur.get("efl_date") or "0").replace("-", "")))):
            if cur:
                p["sources"] = sorted(set(p["sources"]) | set(cur["sources"]))
                p["enroll"] = p.get("enroll") if "ptc" in p["sources"] and p.get("enroll") else cur.get("enroll") or p.get("enroll")
            best[k] = p
        else:
            cur["sources"] = sorted(set(cur["sources"]) | set(p["sources"]))
    final = sorted(best.values(), key=lambda p: (p["tdu"], p["provider"] or "", p["product"] or ""))
    for i, p in enumerate(final):
        p["id"] = i

    summary = {
        "plans": len(final),
        "by_area": {}, "by_method": {}, "not_on_power_to_choose": sum(1 for p in final if "ptc" not in p["sources"]),
        "ptc_rows": len(ptc_rows),
    }
    for p in final:
        summary["by_area"][p["tdu"]] = summary["by_area"].get(p["tdu"], 0) + 1
        summary["by_method"][p["method"]] = summary["by_method"].get(p["method"], 0) + 1
    report["summary"] = summary
    report["finished"] = datetime.now(timezone.utc).isoformat()
    report["minutes"] = round((time.time() - START) / 60, 1)

    # Trim cache entries older than 30 days
    cutoff = (today - timedelta(days=30)).isoformat()
    cache = {u: e for u, e in cache.items() if e["read"] >= cutoff}

    if final or not load("plans.json", {}).get("plans"):
        save("plans.json", {"updated": datetime.now(timezone.utc).isoformat(), "plans": final})
    else:
        report["errors"].append("No plans found this run; kept yesterday's list.")
    save("efl_cache.json", cache)
    save("crawl_report.json", report)
    print(json.dumps(summary, indent=1))


def _safe(fn, u, report):
    try:
        return fn(u)
    except Exception as e:
        if len(report["errors"]) < 200:
            report["errors"].append(f"{u}: {e}")
        return None


def load_registry():
    with open(os.path.join(os.path.dirname(__file__), "providers.json")) as f:
        return json.load(f)["providers"]


if __name__ == "__main__":
    main()
