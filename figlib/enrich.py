"""Fill in authors / canonical title / citation count from Semantic Scholar.

Papers with an arXiv id go through the batch endpoint (500 ids per request);
the rest are matched by title through the search endpoint (one request per
paper, throttled). Results land in the sidecar pdf/<key>.json so `build` picks
them up; nothing is overwritten once present unless --force.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

S2 = "https://api.semanticscholar.org/graph/v1"
FIELDS = "title,authors,year,venue,citationCount,externalIds"


def _norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def run(data: Path, log=print, force=False, api_key=None):
    pdf_dir, idx_dir = data / "pdf", data / "index"
    pdf_dir.mkdir(exist_ok=True)
    headers = {"User-Agent": "paper-fig-library/0.1"}
    if api_key:
        headers["x-api-key"] = api_key
    client = httpx.Client(timeout=60, trust_env=False, headers=headers)

    # collect papers that still need authors
    need = []
    for ip in sorted(idx_dir.glob("*.json")):
        rec = json.loads(ip.read_text())
        if "error" in rec or not rec.get("figures"):
            continue
        key = ip.stem
        sp = pdf_dir / f"{key}.json"
        side = json.loads(sp.read_text()) if sp.exists() else {}
        if side.get("authors") and not force:
            continue
        arxiv = side.get("arxiv_id") or rec["meta"].get("arxiv_id") or ""
        title = side.get("title") or rec["meta"].get("title") or ""
        need.append((key, sp, side, arxiv, title))
    log(f"enrich: {len(need)} papers without authors")

    def save(sp, side, p):
        side = dict(side)
        side["authors"] = [a.get("name", "") for a in p.get("authors", []) if a.get("name")]
        if p.get("title") and (not side.get("title") or len(side["title"]) < 8):
            side["title"] = p["title"]
        side["s2_title"] = p.get("title", "")
        side["citations"] = p.get("citationCount", 0)
        if p.get("year") and not side.get("year_hint"):
            side["s2_year"] = str(p["year"])
        if p.get("venue"):
            side["s2_venue"] = p["venue"]
        sp.write_text(json.dumps(side, ensure_ascii=False, indent=1))

    # 1. arXiv batch
    with_arxiv = [x for x in need if x[3]]
    done = 0
    for i in range(0, len(with_arxiv), 400):
        chunk = with_arxiv[i:i + 400]
        ids = [f"ARXIV:{x[3]}" for x in chunk]
        for attempt in range(4):
            r = client.post(f"{S2}/paper/batch", params={"fields": FIELDS}, json={"ids": ids})
            if r.status_code == 429:
                time.sleep(20 * (attempt + 1))
                continue
            break
        if r.status_code != 200:
            log(f"  batch {i}: HTTP {r.status_code} {r.text[:120]}")
            continue
        for x, p in zip(chunk, r.json()):
            if p:
                save(x[1], x[2], p)
                done += 1
        log(f"  arXiv batch {i // 400 + 1}: {done} filled so far")
        time.sleep(3)

    # 2. title search for the rest
    rest = [x for x in need if not x[3] and len(x[4]) > 12]
    hit = 0
    for n, (key, sp, side, _, title) in enumerate(rest, 1):
        q = re.sub(r"\s+", " ", title)[:200]
        for attempt in range(4):
            r = client.get(f"{S2}/paper/search", params={"query": q, "limit": 1, "fields": FIELDS})
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            break
        if r.status_code == 200:
            items = r.json().get("data") or []
            if items and _norm(items[0].get("title"))[:60] == _norm(title)[:60]:
                save(sp, side, items[0])
                hit += 1
        if n % 50 == 0:
            log(f"  title search {n}/{len(rest)}: {hit} matched")
        time.sleep(1.1)
    log(f"enrich: done, {done} by arXiv id, {hit} by title, {len(need) - done - hit} still without authors")
