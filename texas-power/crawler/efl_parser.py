"""
Electricity Facts Label (EFL) parser.

Every Texas residential plan has an EFL with the same required sections. This module
turns the label's text into a pricing model the website can run against a customer's
real usage, then checks that model against the label's own published averages at
500, 1,000 and 2,000 kWh. A model that can't reproduce those averages is never used.

Three outcomes, best first:
  method "parsed": every price component was read and the averages check out.
  method "linear": the plan is a simple fixed charge plus a per-kWh rate; both are
                   solved exactly from the three published averages.
  method "points": the label has credits, tiers or windows we couldn't read; the site
                   interpolates between the three published points and flags the plan.
"""
import re
from datetime import datetime

PARSER_VERSION = 2  # bump to force every cached label to be read again
N = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
USAGE_POINTS = (500, 1000, 2000)

TDU_PATTERNS = [
    ("oncor", r"oncor"),
    ("centerpoint", r"center\s*point|\bcnp\b"),
    ("aep_central", r"aep\s*(?:texas)?\s*central|aep\s*tcc|\btcc\b"),
    ("aep_north", r"aep\s*(?:texas)?\s*north|aep\s*tnc|\btnc\b"),
    ("tnmp", r"texas[\s-]*new\s*mexico|\btnmp\b"),
    ("lubbock", r"lubbock|lp&l"),
]


def num(s):
    return float(s.replace(",", ""))


def compact(text):
    """Lowercase, strip whitespace and table junk. PDF extraction often drops or adds
    spaces ("perkWh", "ChariotEnergy"), so all matching happens on this form."""
    t = text.lower()
    t = t.replace("\u00a2", "¢").replace("cents", "¢").replace("cent", "¢")
    t = t.replace("≥", ">=").replace("≤", "<=").replace("–", "-").replace("—", "-")
    t = re.sub(r"[\s|*•]+", "", t)
    return t


def detect_tdu(text):
    head = text[:600].lower()
    for code, pat in TDU_PATTERNS:
        if re.search(pat, head):
            return code
    low = text.lower()
    for code, pat in TDU_PATTERNS:
        if re.search(pat, low):
            return code
    return None


def parse_averages(c):
    if "500kwh" not in c or not re.search(r"2,?000kwh", c):
        return None
    for m in re.finditer(r"averageprice(?:per)?kwh:?(?:\(¢\))?" + N + "¢" + N + "¢" + N + "¢", c):
        vals = [num(m.group(i)) for i in (1, 2, 3)]
        if all(0 < v < 100 for v in vals):
            return vals
    # Some labels print the three numbers under the usage row without repeating the heading.
    m = re.search(r"500kwh1,?000kwh2,?000kwh(?:averageprice(?:per)?kwh:?)?" + N + "¢" + N + "¢" + N + "¢", c)
    if m:
        return [num(m.group(i)) for i in (1, 2, 3)]
    return None


def _hour(h, m, ap, is_end):
    h, m = int(h), int(m or 0)
    ap = ap.replace(".", "")
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if is_end and m >= 30:
        h += 1
    return h % 24


TIME_RANGE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?(a\.?m\.?|p\.?m\.?)(?:to|-|until|through)(\d{1,2})(?::(\d{2}))?(a\.?m\.?|p\.?m\.?)"
)
OFF_WORDS = ("night", "free", "offpeak", "off-peak", "weekend", "discount")
ON_WORDS = ("day", "onpeak", "on-peak", "peak", "standard")


def parse_energy(c):
    found = []
    for m in re.finditer(r"energycharge", c):
        after = c[m.end(): m.end() + 45]
        mm = re.match(r"(.{0,25}?)" + N + r"¢", after)
        if not mm:
            continue
        between = mm.group(1)
        if "charge" in between or "$" in between or "n/a" in between:
            continue
        before = c[max(0, m.start() - 40): m.start()]
        label = before[-30:] + between
        if any(w in label for w in ("delivery", "tdu", "tdsp")):
            continue
        rate = num(mm.group(2))
        kind = "flat"
        if any(w in label for w in OFF_WORDS) and not re.search(r"(day|on-?peak)[a-z]*$", before):
            kind = "off"
        elif any(w in label for w in ON_WORDS):
            kind = "on"
        lo, hi = 0, None
        t = re.search(r"(\d[\d,]*)-(\d[\d,]*)kwh", label)
        if t:
            lo, hi = num(t.group(1)), num(t.group(2))
        else:
            t = re.search(r"(?:>|over|above|greaterthan|morethan)(\d[\d,]*)kwh", label)
            if t:
                lo = num(t.group(1))
            t2 = re.search(r"(?:first|upto|<=|<|lessthan)(\d[\d,]*)kwh", label)
            if t2:
                hi = num(t2.group(1))
        found.append({"kind": kind, "rate": rate, "lo": lo, "hi": hi})
    uniq = []
    for f in found:
        if f not in uniq:
            uniq.append(f)
    return uniq


def parse_base(c):
    for m in re.finditer(r"(?:base(?:monthly)?(?:charge|fee)|monthlyservicecharge|monthlybasecharge)", c):
        after = c[m.end(): m.end() + 30]
        if after.startswith(("n/a", ":n/a", "none")):
            return 0.0
        mm = re.match(r"[:\-]?(?:of)?\$" + N, after)
        if mm:
            return num(mm.group(1))
    return 0.0


def parse_tdu_charges(c):
    fixed, rate = None, None
    for m in re.finditer(r"(?:delivery|tdu|tdsp)(?:service)?charges?", c):
        win = c[m.end(): m.end() + 45]
        if fixed is None:
            f = re.match(r"[^$¢]{0,25}?\$" + N + r"(?:per|/)(?:month|billingcycle|bill|mo)", win)
            if not f:
                f = re.search(r"\$" + N + r"(?:per|/)(?:month|billingcycle|mo)", win[:40])
            if f:
                fixed = num(f.group(1))
        if rate is None:
            r = re.match(r"[^$¢]{0,25}?" + N + r"¢(?:per|/)kwh", win)
            if r:
                rate = num(r.group(1))
    return fixed, rate


def parse_credits(c):
    credits = []
    for m in re.finditer(r"credit", c):
        win = c[max(0, m.start() - 60): m.end() + 140]
        amt = re.search(r"\$" + N, win)
        if not amt:
            continue
        a = num(amt.group(1))
        if a <= 0 or a > 500:
            continue
        lo, hi = None, None
        rng = re.search(r"between(\d[\d,]*)(?:kwh)?and(\d[\d,]*)kwh", win) or \
            re.search(r"(\d[\d,]*)(?:kwh)?(?:-|to)(\d[\d,]*)kwh", win)
        if rng:
            lo, hi = num(rng.group(1)), num(rng.group(2))
        else:
            g = re.search(r"(?:>=|atleast|greaterthanorequalto|equaltoorgreaterthan|orgreater|ormore)(\d[\d,]*)kwh", win) or \
                re.search(r"(\d[\d,]*)kwhormore", win) or re.search(r"(\d[\d,]*)kwhorgreater", win)
            if g:
                lo = num(g.group(1))
            else:
                g = re.search(r"(?:>|exceeds|over|above|morethan|greaterthan)(\d[\d,]*)kwh", win)
                if g:
                    lo = num(g.group(1)) + 1
            u = re.search(r"(?:<=|lessthanorequalto|upto)(\d[\d,]*)kwh", win)
            if u and lo is not None:
                hi = num(u.group(1))
        if lo is None:
            continue
        cr = {"amount": a, "min": lo, "max": hi}
        if cr not in credits:
            credits.append(cr)
    return credits


def parse_min_fee(c):
    for m in re.finditer(r"minimum(?:usage)?(?:fee|charge)", c):
        win = c[m.end(): m.end() + 120]
        if win.startswith(("n/a", ":n/a", "none", ":none")):
            return None
        amt = re.match(r"[^$]{0,30}?\$" + N, win)
        thr = re.search(r"(?:<|lessthan|below|under|fewerthan)(\d[\d,]*)kwh", win)
        if amt and thr:
            per_day = "perday" in win[:60] or "daily" in c[max(0, m.start() - 10): m.start()]
            return {"amount": num(amt.group(1)), "under": num(thr.group(1)), "per_day": per_day}
    return None


def parse_window(c):
    for m in TIME_RANGE.finditer(c):
        before = c[max(0, m.start() - 90): m.start()]
        tail = before[-40:]
        if "day" in tail[-15:] and "night" not in tail[-15:]:
            continue
        if any(w in before for w in ("night", "free", "offpeak", "off-peak")):
            start = _hour(m.group(1), m.group(2), m.group(3), False)
            end = _hour(m.group(4), m.group(5), m.group(6), True)
            return start, end
    return None


def parse_share(c):
    for pat in (r"assumes?(?:that)?(\d{1,2}(?:\.\d+)?)%", r"(\d{1,2}(?:\.\d+)?)%oftheelectricityusageoccurring",
                r"(\d{1,2}(?:\.\d+)?)%of(?:the)?(?:average)?(?:monthly)?(?:use|usage)"):
        m = re.search(pat, c)
        if m:
            return num(m.group(1)) / 100
    return None


def parse_meta(text, c):
    meta = {}
    m = re.search(r"typeofproduct:?(fixed|variable|indexed)", c)
    meta["type"] = m.group(1) if m else None
    m = re.search(r"contractterm:?(\d{1,2})", c)
    meta["term"] = int(m.group(1)) if m else None
    m = re.search(r"pre-?payorpayinadvanceproduct\??:?(yes|no)", c)
    meta["prepaid"] = (m.group(1) == "yes") if m else False
    ren = None
    m = re.search(r"thisproductis(\d{1,3}(?:\.\d+)?)%renewable", c)
    if m:
        ren = num(m.group(1))
    else:
        for m in re.finditer(r"renewablecontent(?:is)?:?(\d{1,3}(?:\.\d+)?)%", c):
            if "statewide" not in c[max(0, m.start() - 30): m.start()]:
                ren = num(m.group(1))
                break
    meta["renewable"] = ren
    meta["cancel_fee"] = None
    m = re.search(r"terminat", c)
    if m:
        win = c[m.start(): m.start() + 260]
        f = re.search(r"\$" + N, win)
        if f:
            amt = num(f.group(1))
            per_month = re.search(r"multipliedby|permonthremaining|foreachmonth|eachremainingmonth|permonthleft|xthenumberofmonths", win)
            meta["cancel_fee"] = f"${amt:g} per remaining month" if per_month else f"${amt:g}"
        elif re.search(r"terminat[a-z]*(?:service)?\??:?no\b", win[:80]):
            meta["cancel_fee"] = "none"
    meta["flat_bill"] = bool(re.search(r"flat(?:bill|amount|price)|fixedbill|samebill", c))
    meta["weekend"] = bool(re.search(r"(free|discount)[a-z]*weekend|weekends?free|weekendenergy", c))
    # Names: first meaningful lines after the label title.
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    names = []
    for l in lines[:10]:
        low = l.lower()
        if "facts label" in low or "electricity" == low:
            continue
        if re.search(r"service area|delivery|^date|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}-[a-z]{3}-\d{4}|^[a-z]+ \d{1,2}, \d{4}$|^price|average|header|disclosure|component|electricity price|^page \d", low):
            continue
        if any(re.fullmatch(r"\s*(" + p + r")[\s\w]*", low) for _, p in TDU_PATTERNS) and len(low) < 25:
            continue
        names.append(l)
        if len(names) == 2:
            break
    prov = names[0] if names else None
    if prov:
        if " dba " in prov.lower():
            prov = re.split(r"(?i)\s+d/?b/?a\s+", prov)[-1]
        prov = re.split(r"\s*[•|]\s*|\s+PUCT", prov)[0]
        prov = re.sub(r",?\s*(LLC|LP|L\.P\.|Inc\.?)$", "", prov).strip()
        prov = re.sub(r"([a-z])([A-Z][a-z])", r"\1 \2", prov)  # "ChariotEnergy" -> "Chariot Energy"
    meta["provider"] = prov
    meta["product"] = names[1] if len(names) > 1 else None
    meta["date"] = None
    for pat, fmt in ((r"(\d{1,2}/\d{1,2}/\d{4})", "%m/%d/%Y"), (r"(\d{1,2}-[A-Za-z]{3}-\d{4})", "%d-%b-%Y"),
                     (r"([A-Z][a-z]+ \d{1,2}, \d{4})", "%B %d, %Y")):
        m = re.search(pat, text[:800])
        if m:
            try:
                meta["date"] = datetime.strptime(m.group(1), fmt).date().isoformat()
                break
            except ValueError:
                pass
    return meta


def bill(model, kwh, off_share=0.0):
    """Monthly bill in dollars for one month's kWh. off_share = fraction used in the off-peak window."""
    b = model.get("fixed", 0.0) + kwh * model.get("tdu_rate", 0.0) / 100
    tiers = model.get("tiers") or []
    off = model.get("offpeak")
    if off:
        b += kwh * (1 - off_share) * tiers[0]["rate"] / 100 + kwh * off_share * off["rate"] / 100
    else:
        for t in tiers:
            hi = t["hi"] if t["hi"] is not None else float("inf")
            used = max(0.0, min(kwh, hi) - t["lo"])
            b += used * t["rate"] / 100
    for cr in model.get("credits", []):
        if kwh >= cr["min"] and (cr["max"] is None or kwh <= cr["max"]):
            b -= cr["amount"]
    mf = model.get("min_fee")
    if mf:
        if mf.get("per_day"):
            if kwh / 30 < mf["under"]:
                b += mf["amount"] * 30
        elif kwh < mf["under"]:
            b += mf["amount"]
    return b


def verify(model, averages, off_share=0.0):
    for k, avg in zip(USAGE_POINTS, averages):
        got = bill(model, k, off_share) / k * 100
        if abs(got - avg) > 0.1:  # labels round to 0.1¢
            return False
    return True


def linear_fit(averages):
    pts = [(k, k * a / 100) for k, a in zip(USAGE_POINTS, averages)]
    n = len(pts)
    sx = sum(p[0] for p in pts); sy = sum(p[1] for p in pts)
    sxx = sum(p[0] ** 2 for p in pts); sxy = sum(p[0] * p[1] for p in pts)
    slope = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    icpt = (sy - slope * sx) / n
    model = {"fixed": round(icpt, 4), "tdu_rate": 0.0, "tiers": [{"lo": 0, "hi": None, "rate": round(slope * 100, 4)}]}
    return model if verify(model, averages) and icpt > -1 else None


def parse(text):
    """Return a plan dict, or None if this isn't a residential EFL we can use."""
    c = compact(text)
    if "facts" not in c[:400] and "efl" not in c[:400]:
        return None
    averages = parse_averages(c)
    if not averages:
        return None
    meta = parse_meta(text, c)
    energy = parse_energy(c)
    base = parse_base(c)
    tdu_fixed, tdu_rate = parse_tdu_charges(c)
    credits = parse_credits(c)
    min_fee = parse_min_fee(c)
    window = parse_window(c)
    share = parse_share(c)

    gimmicks = []
    if credits:
        gimmicks.append("bill credit")
    if min_fee:
        gimmicks.append("minimum usage fee")
    if meta["flat_bill"]:
        gimmicks.append("flat bill")

    model, method = None, "points"
    ons = [e for e in energy if e["kind"] in ("on", "flat")]
    offs = [e for e in energy if e["kind"] == "off"]
    if offs and ons:
        gimmicks.append("time of use")
        model = {"fixed": base + (tdu_fixed or 0), "tdu_rate": tdu_rate or 0,
                 "tiers": [{"lo": 0, "hi": None, "rate": ons[0]["rate"]}],
                 "offpeak": {"rate": offs[0]["rate"], "start": window[0] if window else None,
                             "end": window[1] if window else None, "weekend": meta["weekend"]},
                 "credits": credits, "min_fee": min_fee}
        ok = window is not None or meta["weekend"]
        if ok and share is not None and verify(model, averages, share):
            method = "parsed"
        else:
            model = None
    elif ons:
        tiers = sorted(ons, key=lambda e: e["lo"])
        if len(tiers) > 1:
            gimmicks.append("tiered rate")
        model = {"fixed": base + (tdu_fixed or 0), "tdu_rate": tdu_rate or 0,
                 "tiers": [{"lo": t["lo"], "hi": t["hi"], "rate": t["rate"]} for t in tiers],
                 "credits": credits, "min_fee": min_fee}
        if len(tiers) > 1 and any(t["hi"] is None for t in tiers[:-1]):
            model = None
        elif verify(model, averages):
            method = "parsed"
        else:
            model = None
    if model is None and not meta["flat_bill"] and not (offs and ons):
        model = linear_fit(averages)
        if model:
            method = "linear"
            gimmicks = [g for g in gimmicks if g not in ("bill credit", "minimum usage fee")]
    if model is None:
        model = {"points": averages}
        method = "points"
        if not gimmicks:
            gimmicks.append("non-standard pricing")

    return {
        "provider": meta["provider"], "product": meta["product"], "tdu": detect_tdu(text),
        "type": meta["type"], "term": meta["term"], "prepaid": meta["prepaid"],
        "renewable": meta["renewable"], "cancel_fee": meta["cancel_fee"], "efl_date": meta["date"],
        "averages": averages, "model": model, "method": method, "gimmicks": gimmicks,
    }
