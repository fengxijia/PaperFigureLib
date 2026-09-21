"""Generate seed lists of the most cited recent papers of a venue from Semantic Scholar.

    figlib seedgen --area networks --out seeds/top-cited-networks.txt

Uses the bulk search endpoint (no key needed, ~1 request/s) with a venue filter, sorted by
citation count. Keeps papers that have an arXiv id or an open PDF on a host that serves
scripts (USENIX, RSS, PMLR, OpenReview, ACL Anthology, CVF); ACM and IEEE links are skipped
because they answer 403. Papers already in the library are left out."""

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
FIELDS = "title,year,venue,citationCount,openAccessPdf,externalIds"
OPEN_HOSTS = ("arxiv.org", "usenix.org", "roboticsproceedings.org", "proceedings.mlr.press", "openreview.net",
              "aclanthology.org", "openaccess.thecvf.com", "proceedings.neurips.cc", "eprint.iacr.org")

# area -> [(S2 venue query, short venue name, how many to keep)]
VENUES = {
    "networks": [("NSDI", "NSDI", 70), ("SIGCOMM", "SIGCOMM", 60), ("INFOCOM", "INFOCOM", 40),
                 ("Internet Measurement Conference", "IMC", 25), ("CoNEXT", "CoNEXT", 20)],
    "mobile": [("MobiCom", "MobiCom", 50), ("MobiSys", "MobiSys", 40), ("SenSys", "SenSys", 30),
               ("Proceedings of the ACM on Interactive, Mobile, Wearable and Ubiquitous Technologies", "IMWUT", 40),
               ("IPSN", "IPSN", 15)],
    "hci": [("CHI", "CHI", 150), ("UIST", "UIST", 50), ("Proceedings of the ACM on Human-Computer Interaction", "CSCW", 40),
            ("Designing Interactive Systems", "DIS", 20)],
    "hpc": [("SC", "SC", 80), ("HPDC", "HPDC", 40), ("ICS", "ICS", 40), ("IPDPS", "IPDPS", 40),
            ("PPoPP", "PPoPP", 40), ("Euro-Par", "Euro-Par", 20)],
    "robotics": [("Robotics: Science and Systems", "RSS", 60), ("Conference on Robot Learning", "CoRL", 70),
                 ("ICRA", "ICRA", 80), ("IROS", "IROS", 50), ("Human-Robot Interaction", "HRI", 25),
                 ("IEEE Transactions on Robotics", "T-RO", 25), ("Science Robotics", "Science Robotics", 25)],
}


def _get(params, retries=4):
    url = API + "?" + urllib.parse.urlencode(params)
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "figlib-seedgen"}), timeout=60) as r:
                return json.load(r)
        except Exception as e:                      # rate limit or hiccup: back off
            if i == retries - 1:
                raise
            time.sleep(8 * (i + 1))


def _norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()[:80]


def known_papers(data: Path):
    """arXiv ids and normalised titles already in the library (index + fetched sidecars)."""
    ids, titles = set(), set()
    for p in (data / "index").glob("*.json"):
        try:
            rec = json.loads(p.read_text())
        except Exception:
            continue
        meta = rec.get("meta", rec)
        if meta.get("arxiv_id"):
            ids.add(meta["arxiv_id"])
        titles.add(_norm(meta.get("title")))
        ids.add(p.stem)
    for p in (data / "pdf").glob("*.json"):
        ids.add(p.stem)
        try:
            titles.add(_norm(json.loads(p.read_text()).get("title")))
        except Exception:
            pass
    return ids, titles


def pick(venue_query, short, want, years, known_ids, known_titles, log=print):
    """Top cited open papers of one venue, as seed lines."""
    lines, seen = [], set()
    token = None
    fetched = 0
    while len(lines) < want and fetched < 3000:
        params = {"query": "", "venue": venue_query, "year": years, "fields": FIELDS, "sort": "citationCount:desc", "limit": 1000}
        if token:
            params["token"] = token
        d = _get(params)
        rows = d.get("data") or []
        fetched += len(rows)
        for p in rows:
            title = (p.get("title") or "").strip()
            if not title or _norm(title) in known_titles or _norm(title) in seen:
                continue
            ex = p.get("externalIds") or {}
            arx = ex.get("ArXiv")
            oa = ((p.get("openAccessPdf") or {}).get("url") or "").strip()
            body = None
            if arx:
                if arx in known_ids:
                    continue
                body = arx
            elif oa and any(h in oa for h in OPEN_HOSTS):
                body = oa
            if not body:
                continue
            seen.add(_norm(title))
            note = f"{short} {p.get('year')} High-impact ({p.get('citationCount', 0)} citations): {title}"
            lines.append(f"{body}   # {note}")
            if len(lines) >= want:
                break
        token = d.get("token")
        if not token or not rows:
            break
        time.sleep(3)
    log(f"  {short}: {len(lines)} open papers kept (scanned {fetched})")
    return lines


def run(data: Path, area: str, out: Path, years="2021-2026", log=print):
    known_ids, known_titles = known_papers(data)
    log(f"seedgen {area}: {len(known_ids)} known ids, {len(known_titles)} known titles")
    lines = [f"# Most cited {years} papers of the {area} venues with an open PDF, from Semantic Scholar (figlib seedgen, {time.strftime('%Y-%m-%d')}).",
             "# Skips papers already in the library; ACM / IEEE links are left out because they refuse scripts."]
    for venue_query, short, want in VENUES[area]:
        lines += pick(venue_query, short, want, years, known_ids, known_titles, log)
        time.sleep(4)
    out.write_text("\n".join(lines) + "\n")
    n = sum(1 for l in lines if not l.startswith("#"))
    log(f"seedgen {area}: {n} papers -> {out}")
    return n
