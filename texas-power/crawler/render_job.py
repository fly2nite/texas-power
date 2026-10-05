"""Runs the daily crawl on Render (or any server) and saves the results to GitHub.

Needs three settings (environment variables) on the server:
  GITHUB_TOKEN  a fine-grained token with Contents: read and write on the repository
  GITHUB_REPO   e.g. fly2nite/texas-power
  DATA_PATH     folder in the repository holding plans.json, e.g. texas-power/data

Steps: download yesterday's label cache from GitHub, run the crawler, then upload
plans.json, crawl_report.json and efl_cache.json together as one commit. Renders
static site then republishes automatically.
"""
import base64
import json
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DATA = os.path.join(os.path.dirname(HERE), "data")
FILES = ["plans.json", "crawl_report.json", "efl_cache.json"]

TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
REPO = os.environ.get("GITHUB_REPO", "").strip()
DATA_PATH = os.environ.get("DATA_PATH", "data").strip().strip("/")
BRANCH = os.environ.get("GITHUB_BRANCH", "main").strip()
API = "https://api.github.com"


def gh(method, path, **kw):
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    for attempt in range(4):
        r = requests.request(method, API + path, headers=headers, timeout=60, **kw)
        if r.status_code < 500 and r.status_code != 429:
            break
        time.sleep(5 * (attempt + 1))
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub {method} {path} failed: {r.status_code} {r.text[:300]}")
    return r.json() if r.text else {}


def check_settings():
    missing = [n for n, v in (("GITHUB_TOKEN", TOKEN), ("GITHUB_REPO", REPO)) if not v]
    if missing:
        sys.exit(f"Missing setting(s): {', '.join(missing)}. Add them under Environment on Render.")
    gh("GET", f"/repos/{REPO}")  # fails early with a clear message if the token can't see the repo
    print(f"Connected to GitHub repository {REPO}", flush=True)


def download_cache():
    """Start from the latest saved files so unchanged labels aren't re-read."""
    os.makedirs(DATA, exist_ok=True)
    for name in ("efl_cache.json", "plans.json"):
        try:
            meta = gh("GET", f"/repos/{REPO}/contents/{DATA_PATH}/{name}", params={"ref": BRANCH})
            if meta.get("content"):
                raw = base64.b64decode(meta["content"])
            else:  # files over 1 MB come back without content; fetch the blob instead
                blob = gh("GET", f"/repos/{REPO}/git/blobs/{meta['sha']}")
                raw = base64.b64decode(blob["content"])
            with open(os.path.join(DATA, name), "wb") as f:
                f.write(raw)
            print(f"Downloaded previous {name}", flush=True)
        except RuntimeError as e:
            print(f"No previous {name} ({str(e)[:80]})", flush=True)


def upload_results():
    """Save all result files in a single commit."""
    ref = gh("GET", f"/repos/{REPO}/git/ref/heads/{BRANCH}")
    head_sha = ref["object"]["sha"]
    base_tree = gh("GET", f"/repos/{REPO}/git/commits/{head_sha}")["tree"]["sha"]
    tree = []
    for name in FILES:
        path = os.path.join(DATA, name)
        if not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            blob = gh("POST", f"/repos/{REPO}/git/blobs",
                      json={"content": base64.b64encode(f.read()).decode(), "encoding": "base64"})
        tree.append({"path": f"{DATA_PATH}/{name}", "mode": "100644", "type": "blob", "sha": blob["sha"]})
    new_tree = gh("POST", f"/repos/{REPO}/git/trees", json={"base_tree": base_tree, "tree": tree})
    if new_tree["sha"] == base_tree:
        print("Nothing changed since the last save.", flush=True)
        return
    commit = gh("POST", f"/repos/{REPO}/git/commits", json={
        "message": f"Plan list for {time.strftime('%Y-%m-%d', time.gmtime())}",
        "tree": new_tree["sha"], "parents": [head_sha]})
    gh("PATCH", f"/repos/{REPO}/git/refs/heads/{BRANCH}", json={"sha": commit["sha"]})
    print(f"Saved results to GitHub ({commit['sha'][:7]})", flush=True)


if __name__ == "__main__":
    check_settings()
    download_cache()
    import run
    run.main()
    for attempt in range(3):  # someone may have edited the repository while we ran
        try:
            upload_results()
            break
        except RuntimeError as e:
            if attempt == 2:
                raise
            print(f"Save failed, retrying: {e}", flush=True)
            time.sleep(10)
