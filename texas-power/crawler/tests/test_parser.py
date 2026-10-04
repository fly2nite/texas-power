"""Run: python -m pytest crawler/tests  (or python crawler/tests/test_parser.py)"""
import os, sys
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.dirname(HERE))
import efl_parser as p

def load(name):
    return p.parse(open(os.path.join(HERE, "fixtures", name)).read())

def test_simple_fixed():
    r = load("goodcharlie.txt")
    assert r["method"] == "parsed" and r["gimmicks"] == [] and r["tdu"] == "oncor"
    assert r["model"]["tiers"][0]["rate"] == 7.2 and r["term"] == 12

def test_bill_credit_found_and_flagged():
    r = load("chariot_gridplus.txt")
    assert r["method"] == "parsed" and "bill credit" in r["gimmicks"]
    assert r["model"]["credits"] == [{"amount": 125.0, "min": 1000.0, "max": None}]
    assert abs(p.bill(r["model"], 999) - p.bill(r["model"], 1000) - 125 + 0.1342*1 + 0.060295) < 0.5

def test_free_nights_window():
    r = load("chariot_nights.txt")
    assert r["method"] == "parsed" and r["model"]["offpeak"]["start"] == 23 and r["model"]["offpeak"]["end"] == 6

def test_unlabeled_delivery_falls_back_to_exact_line():
    r = load("think.txt")
    assert r["method"] == "linear"

def test_flat_bill_is_flagged_not_trusted():
    r = load("cirro_flat.txt")
    assert r["method"] == "points" and "flat bill" in r["gimmicks"]

def test_names():
    assert load("gexa.txt")["provider"] == "Gexa Energy"

if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)
