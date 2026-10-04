"""Power to Choose: the state's opt-in list. Used as one source and as a map of where
providers keep their facts labels, never as the full list."""
import csv
import io
import json
import re

from fetch import get
from efl_parser import detect_tdu

CSV_URLS = ["http://www.powertochoose.org/en-us/Plan/ExportToCsv",
            "https://www.powertochoose.org/en-us/Plan/ExportToCsv"]
API_URL = "http://api.powertochoose.org/api/PowerToChoose/plans?zip_code={}"
# One ZIP per delivery area, used only if the statewide CSV export is unavailable.
AREA_ZIPS = {"oncor": "75201", "centerpoint": "77002", "aep_central": "78401",
             "aep_north": "79601", "tnmp": "77590", "lubbock": "79401"}


def _key(k):
    return re.sub(r"[\[\]\s_/]", "", k or "").lower()


def _yes(v):
    return str(v).strip().lower() in ("true", "1", "yes", "y")


def _price(v):
    try:
        x = float(str(v).replace("$", "").replace("¢", ""))
    except ValueError:
        return None
    return round(x * 100, 2) if x < 1 else x  # store in cents per kWh


def _row(r):
    g = {_key(k): v for k, v in r.items()}
    pick = lambda *ks: next((g[k] for k in ks if g.get(k) not in (None, "")), "")
    prices = [_price(pick("kwh500", "pricekwh500")), _price(pick("kwh1000", "pricekwh1000")),
              _price(pick("kwh2000", "pricekwh2000"))]
    lang = pick("language")
    if lang and "english" not in str(lang).lower():
        return None
    rate_type = str(pick("ratetype", "ratetypename")).lower()
    return {
        "ptc_id": str(pick("idkey", "planid")),
        "provider": pick("repcompany", "companyname"),
        "product": pick("product", "planname"),
        "tdu": detect_tdu(str(pick("tducompanyname", "tduname"))),
        "averages": prices if all(prices) else None,
        "type": "fixed" if "fixed" in rate_type or (not rate_type and _yes(pick("fixed"))) else (rate_type or None),
        "prepaid": _yes(pick("prepaid")),
        "tou": _yes(pick("timeofuse")),
        "usage_terms": _yes(pick("minusagefeescredits", "minimumusage")),
        "new_customers_only": _yes(pick("newcustomer")),
        "term": int(float(pick("termvalue") or 0)) or None,
        "renewable": float(pick("renewable") or 0) or None,
        "cancel_fee": pick("cancelfee") or None,
        "efl": pick("factsurl", "factsheet"),
        "enroll": pick("enrollurl", "gotoplan") or pick("website"),
    }


def fetch_plans(report):
    rows = []
    for url in CSV_URLS:
        res = get(url, gap=2)
        if res and res[3] == 200 and res[1][:200].count(b",") > 5:
            text = res[1].decode("utf-8-sig", errors="replace")
            rows = [r for r in (_row(x) for x in csv.DictReader(io.StringIO(text))) if r]
            report["ptc"] = {"method": "csv", "rows": len(rows)}
            break
        report.setdefault("ptc_errors", []).append(f"{url}: {res[3] if res else 'no response'}")
    if not rows:
        for area, z in AREA_ZIPS.items():
            res = get(API_URL.format(z), gap=2)
            if not res or res[3] != 200:
                report.setdefault("ptc_errors", []).append(f"api {z}: {res[3] if res else 'no response'}")
                continue
            try:
                data = json.loads(res[1])
            except ValueError:
                continue
            items = data.get("data", data) if isinstance(data, dict) else data
            for it in items if isinstance(items, list) else []:
                r = _row(it)
                if r:
                    r["tdu"] = r["tdu"] or area
                    rows.append(r)
        report["ptc"] = {"method": "api", "rows": len(rows)}
    seen, out = set(), []
    for r in rows:
        k = r["ptc_id"] or (r["provider"], r["product"], r["tdu"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out
