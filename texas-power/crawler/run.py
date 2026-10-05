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
TIME_LIMIT = 100 * 60   # stop starting new work after 100 minutes, then save what we have
START = time.time()


def log(msg):
    print(f"[{(time.time() - START) / 60:5.1f} min] {msg}", flush=True)


def out_of_time():
    return time.time() - START > TIME_LIMIT


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
        if ent and ent.get("v") == efl_parser.PARSER_VERSION and (today - date.fromisoformat(ent["read"])).days < CACHE_DAYS:
            return ent["result"]
        text, status = document_text(url)
        result = efl_parser.parse(text) if text else None
        cache[url] = {"v": efl_parser.PARSER_VERSION, "read": today.isoformat(), "status": status, "result": result,
                      "hash": hashlib.md5((text or "").encode()).hexdigest()[:10]}
        return result

    # 1. Power to Choose (one source among several)
    log("Reading Power to Choose...")
    ptc_rows = ptc.fetch_plans(report)
    log(f"Power to Choose: {len(ptc_rows)} plans ({report.get('ptc', {}).get('method', 'failed')})")
    candidates = {}  # efl url -> info
    for r in ptc_rows:
        if r["efl"]:
            candidates.setdefault(r["efl"], {"sources": set(), "ptc": None})
            candidates[r["efl"]]["sources"].add("ptc")
            candidates[r["efl"]]["ptc"] = r

    # Learn each label host's provider name from Power to Choose, so plans found elsewhere
    # on the same host get the same clean name.
    host_names = {}
    for r_ in ptc_rows:
        if r_["efl"] and r_["provider"]:
            h = urlparse(r_["efl"]).netloc
            host_names.setdefault(h, {}).setdefault(r_["provider"], 0)
            host_names[h][r_["provider"]] += 1
    host_name = {}
    for h, v in host_names.items():
        name = max(v, key=v.get)
        host_name[h] = name.title() if name.isupper() else name

    # 1b. Facts label links pasted into seed_efls.txt (e.g. broker-only plans)
    seeds = load_seeds()
    for u in seeds:
        candidates.setdefault(u, {"sources": set(), "ptc": None})
        candidates[u]["sources"].add("seed")
    log(f"Seed labels from seed_efls.txt: {len(seeds)}")

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

    log(f"Searching {len(sites)} provider websites...")
    done_sites = 0
    with ThreadPoolExecutor(20) as ex:
        for p, efls, pages in ex.map(crawl, sites.values()):
            done_sites += 1
            log(f"  {done_sites}/{len(sites)} {p['site']}: {pages} pages, {len(efls)} labels")
            report["providers"][p["name"]] = {"site": p["site"], "pages": pages, "labels_on_site": len(efls)}
            for u in efls:
                candidates.setdefault(u, {"sources": set(), "ptc": None, "site": p["site"]})
                candidates[u]["sources"].add("site")
                candidates[u].setdefault("site", p["site"])

    # 3. Read every label found so far
    urls = interleave(list(candidates))
    log(f"Reading {len(urls)} facts labels...")
    results, counter = {}, [0]

    def read_one(u):
        r = None if out_of_time() else _safe(parse_url, u, report)
        counter[0] += 1
        if counter[0] % 50 == 0:
            log(f"  read {counter[0]}/{len(urls)} labels")
        return u, r

    with ThreadPoolExecutor(24) as ex:
        for u, r in ex.map(read_one, urls):
            results[u] = r
    log(f"Labels read: {sum(1 for r in results.values() if r)} usable")

    # 4. Probe nearby label addresses for plans nobody lists
    good = [u for u, r in results.items() if r]
    probes = probe_candidates(good) if time.time() - START < TIME_LIMIT * 0.6 else []
    probes = interleave([(k, u) for k, u in probes if u not in candidates], key=lambda t: t[1])
    report["probe_attempts"] = len(probes)
    log(f"Probing {len(probes)} nearby label addresses for unlisted plans...")
    hits_by_tpl = {}

    def probe(item):
        key, u = item
        if out_of_time():
            return u, None
        return u, _safe(parse_url, u, report)

    with ThreadPoolExecutor(24) as ex:
        for u, r in ex.map(probe, probes):
            if r:
                results[u] = r
                candidates[u] = {"sources": {"probe"}, "ptc": None}
                hits_by_tpl[urlparse(u).netloc] = hits_by_tpl.get(urlparse(u).netloc, 0) + 1
    report["probe_hits"] = hits_by_tpl
    log(f"Probing found {sum(hits_by_tpl.values())} more labels")

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
                known = host_name.get(urlparse(url).netloc)
                if known:
                    plan["provider"] = known
            plan["product"] = clean_product(plan)
            plans.append(plan)
        elif row and row["averages"]:
            g = []
            if row["tou"]: g.append("time of use")
            if row["usage_terms"]: g.append("bill credit or minimum fee")
            pts = row["averages"]
            m = efl_parser.linear_fit(pts) if not g else None
            if row["provider"] and row["provider"].isupper():
                row["provider"] = row["provider"].title()
            row["product"] = clean_product({**row, "model": {}})
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
    log("Saved. Summary:")
    print(json.dumps(summary, indent=1), flush=True)


def interleave(urls, key=lambda u: u):
    """Order work round-robin across websites, so the workers spread out instead of
    all queuing behind one site's one-request-per-second limit."""
    from collections import OrderedDict
    groups = OrderedDict()
    for item in urls:
        groups.setdefault(urlparse(key(item)).netloc, []).append(item)
    out, lists = [], list(groups.values())
    i = 0
    while any(lists):
        for l in lists:
            if i < len(l):
                out.append(l[i])
        i += 1
        lists = [l for l in lists if len(l) > i]
    return out


def clean_product(plan):
    """Replace label text that isn't really a plan name with a plain description."""
    name = (plan.get("product") or "").strip()
    bad = (not name or len(name) > 60 or norm(name) == norm(plan.get("provider"))
           or re.search(r"disclosure|price|average|header|component|facts label", name, re.I))
    if not bad:
        return name
    kind = "time-of-use" if plan.get("model", {}).get("offpeak") else (plan.get("type") or "fixed")
    return f"{plan['term']}-month {kind} plan" if plan.get("term") else f"{kind.capitalize()} plan"


def _safe(fn, u, report):
    try:
        return fn(u)
    except Exception as e:
        if len(report["errors"]) < 200:
            report["errors"].append(f"{u}: {e}")
        return None


def load_seeds():
    path = os.path.join(os.path.dirname(__file__), "seed_efls.txt")
    try:
        with open(path) as f:
            return [l.strip() for l in f if l.strip().startswith("http")]
    except FileNotFoundError:
        return []


def load_registry():
    with open(os.path.join(os.path.dirname(__file__), "providers.json")) as f:
        return json.load(f)["providers"]


if __name__ == "__main__":
    main()
