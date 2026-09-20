"""paper-fig-library command line.

    figlib fetch   --seeds seeds/top-papers.txt [--ids 2308.04079 ...]
    figlib extract [--pdf-dir ./my-pdfs ...]
    figlib classify [--model gpt-4.1-mini] [--limit N]
    figlib split   (cut multi-panel data figures into single panels; run classify again after)
    figlib build   [--prune]
    figlib all     (fetch + extract + build with the defaults)

Every command takes --data DIR (default $FIGLIB_DATA or ./data).
Layout of DIR:
    pdf/<arxiv_id>.pdf, pdf/<arxiv_id>.json     downloaded papers + arXiv metadata
    index/<paper_key>.json                       per-paper extraction result (cache)
    figs/<fig_id>.png, <fig_id>.thumb.png        rendered figures
    index.json, gallery.html                     merged index + browsable page
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = Path(os.environ.get("FIGLIB_DATA", "data"))
ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
UA = "paper-fig-library/0.1 (personal research tool)"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ fetch

def key_for_url(url: str) -> str:
    """Stable paper key for a direct PDF url: host tag + last path piece."""
    from urllib.parse import urlparse, parse_qs
    u = urlparse(url)
    host = u.netloc.lower()
    if "openreview" in host:
        return "openreview-" + re.sub(r"[^A-Za-z0-9_-]", "", parse_qs(u.query).get("id", ["x"])[0])
    tag = {"aclanthology.org": "acl", "www.usenix.org": "usenix", "openaccess.thecvf.com": "cvf",
           "proceedings.mlr.press": "pmlr", "www.ndss-symposium.org": "ndss", "dl.acm.org": "acm",
           "www.vldb.org": "vldb", "proceedings.neurips.cc": "neurips", "www.roboticsproceedings.org": "rss"}.get(host, host.split(".")[-2] if "." in host else host)
    last = u.path.rstrip("/").rsplit("/", 1)[-1]
    last = re.sub(r"\.pdf$", "", last, flags=re.I)
    last = re.sub(r"[^A-Za-z0-9._-]+", "_", last)[:60] or "paper"
    return f"{tag}-{last}"


def parse_seeds(path: Path):
    """One paper per line: `<arxiv id | https URL> # <VENUE> <YEAR> <note>`.
    Lines whose id is NOPDF (no open PDF found) are skipped."""
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        body, _, note = line.partition("#")
        body = body.strip()
        note = note.strip()
        venue, year = "", ""
        vm = re.match(r"([A-Za-z][A-Za-z&/ -]*?)\s*(20\d\d)", note)
        if vm:
            venue, year = vm.group(1).strip(), vm.group(2)
        else:
            ym = re.match(r"(20\d\d)", note)
            if ym:
                year = ym.group(1)
        if body.upper().startswith("NOPDF"):
            continue
        if body.lower().startswith("http"):
            url = body.split()[0]
            out.append({"arxiv_id": "", "url": url, "key": key_for_url(url),
                        "venue_hint": venue, "year_hint": year, "note": note})
            continue
        m = ARXIV_ID.search(body)
        if not m:
            continue
        out.append({"arxiv_id": m.group(1), "url": f"https://arxiv.org/pdf/{m.group(1)}", "key": m.group(1),
                    "venue_hint": venue, "year_hint": year, "note": note})
    return out


def arxiv_api(ids):
    """Title / authors / published year for a batch of ids."""
    meta = {}
    for i in range(0, len(ids), 40):
        batch = ids[i:i + 40]
        url = "https://export.arxiv.org/api/query?max_results=100&id_list=" + ",".join(batch)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                xml = r.read()
        except Exception as e:
            log("arxiv api failed:", e, "(retrying once after 20s)")
            time.sleep(20)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    xml = r.read()
            except Exception as e2:
                log("arxiv api failed again:", e2)
                continue
        ns = {"a": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(xml)
        for ent in root.findall("a:entry", ns):
            eid = ent.findtext("a:id", "", ns)
            m = ARXIV_ID.search(eid)
            if not m:
                continue
            meta[m.group(1)] = {
                "title": re.sub(r"\s+", " ", ent.findtext("a:title", "", ns)).strip(),
                "authors": [a.findtext("a:name", "", ns) for a in ent.findall("a:author", ns)],
                "published": ent.findtext("a:published", "", ns)[:10],
                "abs_url": f"https://arxiv.org/abs/{m.group(1)}",
            }
        time.sleep(3)
    return meta


def cmd_fetch(args):
    data = args.data
    pdf_dir = data / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    seeds = []
    for sp in args.seeds or []:
        seeds += parse_seeds(Path(sp))
    for i in args.ids or []:
        m = ARXIV_ID.search(i)
        if m:
            seeds.append({"arxiv_id": m.group(1), "url": f"https://arxiv.org/pdf/{m.group(1)}", "key": m.group(1),
                          "venue_hint": "", "year_hint": "", "note": ""})
    # first occurrence of a key wins (the curated top-papers list comes first)
    byk = {}
    for sd in seeds:
        byk.setdefault(sd["key"], sd)
    seeds = list(byk.values())
    log(f"fetch: {len(seeds)} papers")

    def _has_title(k):
        jp = pdf_dir / f"{k}.json"
        try:
            return bool(json.loads(jp.read_text()).get("title"))
        except Exception:
            return False

    need_meta = [sd["arxiv_id"] for sd in seeds if sd["arxiv_id"] and not _has_title(sd["key"])]
    api = arxiv_api(need_meta) if (need_meta and not args.no_api) else {}
    n_new = n_fail = 0
    for sd in seeds:
        k = sd["key"]
        jp = pdf_dir / f"{k}.json"
        if not _has_title(k):
            m = dict(api.get(sd["arxiv_id"], {}))
            m.update({kk: v for kk, v in sd.items() if kk != "key"})
            jp.write_text(json.dumps(m, ensure_ascii=False, indent=1))
        pp = pdf_dir / f"{k}.pdf"
        if pp.exists() and pp.stat().st_size > 10_000:
            continue
        if not args.refetch:
            try:
                if json.loads(jp.read_text()).get("pdf_pruned"):
                    continue            # figures already extracted; PDF deleted to save disk
            except Exception:
                pass
        free_gb = shutil.disk_usage(pdf_dir).free / 1e9
        if free_gb < args.min_free_gb:
            log(f"fetch: stopping, only {free_gb:.1f} GB free (< {args.min_free_gb} GB)")
            break
        req = urllib.request.Request(sd["url"], headers={"User-Agent": UA, "Accept": "application/pdf,*/*"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                blob = r.read()
            if not blob.startswith(b"%PDF"):
                log(f"  {k}: not a pdf ({len(blob)} bytes) from {sd['url']}")
                n_fail += 1
                continue
            pp.write_bytes(blob)
            n_new += 1
            log(f"  {k}: {len(blob)//1024} KB")
        except Exception as e:
            log(f"  {k}: download failed: {e}")
            n_fail += 1
        time.sleep(args.delay)
    log(f"fetch: {n_new} downloaded, {n_fail} failed")


# ---------------------------------------------------------------- extract

def paper_key_for(pdf: Path):
    if pdf.with_suffix(".json").exists():
        return pdf.stem          # fetched by us: the stem already is the key
    m = ARXIV_ID.search(pdf.stem)
    if m:
        return m.group(1)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", pdf.stem)[:80]


EXTRACTOR_VERSION = 5      # bump when extract.py changes what gets cropped


def _extract_one(job):
    pdf, key, figs_dir, dpi = job
    from figlib.extract import extract_pdf
    try:
        meta, figs = extract_pdf(str(pdf), figs_dir, key, dpi=dpi)
    except Exception as e:
        return key, {"error": repr(e), "pdf": str(pdf)}
    rec = {
        "paper_key": key, "pdf": str(pdf), "pdf_mtime": pdf.stat().st_mtime,
        "extractor_version": EXTRACTOR_VERSION, "dpi": dpi,
        "meta": meta, "figures": [f.to_dict() for f in figs],
    }
    return key, rec


def _carry_labels(old: dict, rec: dict):
    """Keep vision labels (and panel splits) across a re-extract: match by (page, figure number)."""
    if not old or "figures" not in old:
        return 0
    prev = {(f["page"], f["num"]): f for f in old["figures"] if not f.get("parent")}
    panels = {}
    for f in old["figures"]:
        if f.get("parent"):
            panels.setdefault(f["parent"], []).append(f)
    n = 0
    for f in list(rec["figures"]):
        o = prev.get((f["page"], f["num"]))
        if not o:
            continue
        if o.get("vision"):
            f["vision"] = o["vision"]
            n += 1
        if o.get("vision2"):
            f["vision2"] = o["vision2"]
        if o.get("panels") is not None and o["fig_id"] == f["fig_id"]:
            f["panels"] = o["panels"]
            rec["figures"].extend(panels.get(o["fig_id"], []))
    return n


def iter_pdfs(data: Path, extra_dirs):
    seen = set()
    dirs = [data / "pdf"] + [Path(d) for d in extra_dirs]
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.pdf")):
            if ".zh." in p.name or p.name.endswith(".zh.pdf"):
                continue        # translated copies from paper-reader
            key = paper_key_for(p)
            if key in seen:
                continue
            seen.add(key)
            yield p, key


def cmd_extract(args):
    data = args.data
    figs_dir = data / "figs"
    idx_dir = data / "index"
    figs_dir.mkdir(parents=True, exist_ok=True)
    idx_dir.mkdir(parents=True, exist_ok=True)
    jobs, olds = [], {}
    for pdf, key in iter_pdfs(data, args.pdf_dir or []):
        jp = idx_dir / f"{key}.json"
        old = None
        if jp.exists():
            try:
                old = json.loads(jp.read_text())
            except Exception:
                old = None
        if old and not args.force and "error" not in old:
            same_pdf = abs(old.get("pdf_mtime", -1) - pdf.stat().st_mtime) < 1
            same_code = old.get("extractor_version") == EXTRACTOR_VERSION and old.get("dpi") == args.dpi
            if same_pdf and same_code:
                continue
        olds[key] = old
        jobs.append((pdf, key, figs_dir, args.dpi))
    log(f"extract: {len(jobs)} papers to process ({args.jobs} workers)")
    from figlib.classify import atomic_write_json
    done = 0
    with cf.ProcessPoolExecutor(max_workers=args.jobs) as ex:
        for key, rec in ex.map(_extract_one, jobs):
            kept = _carry_labels(olds.get(key), rec) if "error" not in rec else 0
            atomic_write_json(idx_dir / f"{key}.json", rec)
            done += 1
            n = len(rec.get("figures", []))
            log(f"  [{done}/{len(jobs)}] {key}: {n} figures, {kept} labels kept" + (f"  ERROR {rec['error']}" if "error" in rec else ""))


def cmd_remeta(args):
    """Recompute paper metadata for every index entry without re-extracting figures."""
    from figlib.extract import paper_meta
    import pymupdf
    n = 0
    for jp in sorted((args.data / "index").glob("*.json")):
        rec = json.loads(jp.read_text())
        pdf = Path(rec.get("pdf", ""))
        if "error" in rec or not pdf.exists():
            continue
        doc = pymupdf.open(str(pdf))
        rec["meta"] = paper_meta(doc, fallback_title=rec["paper_key"])
        doc.close()
        from figlib.classify import atomic_write_json
        atomic_write_json(jp, rec)
        n += 1
    log(f"remeta: {n} papers")


# ----------------------------------------------------------- convert-webp

def cmd_convert_webp(args):
    """One-off migration: re-encode every PNG in figs/ as WebP and delete the PNG."""
    from PIL import Image
    from figlib.extract import FIG_EXT, WEBP_QUALITY
    figs = args.data / "figs"
    n = before = after = 0
    for png in sorted(figs.glob("*.png")):
        out = png.with_suffix(FIG_EXT)
        if not out.exists():
            with Image.open(png) as im:
                im.convert("RGB").save(out, quality=WEBP_QUALITY, method=4)
        before += png.stat().st_size
        after += out.stat().st_size
        png.unlink()
        n += 1
        if n % 500 == 0:
            log(f"  {n} converted")
    log(f"convert-webp: {n} files, {before / 1e6:.0f} MB -> {after / 1e6:.0f} MB")


# ----------------------------------------------------------------- verify

def cmd_verify(args):
    from figlib.classify import verify
    cats = set(args.cats.split(","))
    usage = verify(args.data, args.model, cats, jobs=args.jobs, limit=args.limit, include_unlabeled=not args.labeled_only, log=log)
    log(f"verify: done, usage {usage}")


# ----------------------------------------------------------------- enrich

def cmd_enrich(args):
    from figlib.enrich import run
    run(args.data, log=log, force=args.force, api_key=os.environ.get("S2_API_KEY"))


# ---------------------------------------------------------------- rethumb

def cmd_rethumb(args):
    """Regenerate thumbnails from the full-size images (no PDF needed)."""
    from PIL import Image
    from figlib.extract import FIG_EXT, WEBP_QUALITY
    figs = args.data / "figs"
    n = 0
    for full in sorted(figs.glob(f"*{FIG_EXT}")):
        if full.name.endswith(f".thumb{FIG_EXT}"):
            continue
        th = full.with_name(full.name[:-len(FIG_EXT)] + f".thumb{FIG_EXT}")
        try:
            with Image.open(full) as im:
                if not args.force and th.exists():
                    with Image.open(th) as t0:
                        if t0.width >= min(args.px, im.width):
                            continue
                im = im.convert("RGB")
                im.thumbnail((args.px, args.px * 3))
                im.save(th, quality=WEBP_QUALITY, method=4)
                n += 1
        except Exception as e:
            log(f"  ! {full.name}: {e}")
    log(f"rethumb: {n} thumbnails regenerated at {args.px}px")


# ------------------------------------------------------------- prune-pdfs

def cmd_prune_pdfs(args):
    """Delete downloaded PDFs whose figures are extracted with the current extractor.
    The sidecar keeps the URL, so `fetch --refetch` brings a PDF back when needed."""
    data = args.data
    freed = n = 0
    for pp in sorted((data / "pdf").glob("*.pdf")):
        key = pp.stem
        jp, ip = data / "pdf" / f"{key}.json", data / "index" / f"{key}.json"
        if not (jp.exists() and ip.exists()):
            continue
        try:
            rec = json.loads(ip.read_text())
            side = json.loads(jp.read_text())
        except Exception:
            continue
        if "error" in rec or rec.get("extractor_version") != EXTRACTOR_VERSION:
            continue
        if abs(rec.get("pdf_mtime", -1) - pp.stat().st_mtime) >= 1:
            continue                    # index was built from a different file
        side["pdf_pruned"] = True
        side["pdf_bytes"] = pp.stat().st_size
        from figlib.classify import atomic_write_json
        atomic_write_json(jp, side)
        freed += pp.stat().st_size
        pp.unlink()
        n += 1
    log(f"prune-pdfs: deleted {n} PDFs, freed {freed / 1e9:.2f} GB")


# ------------------------------------------------------------------ split

def cmd_split(args):
    from figlib.split import run
    run(args.data, log=log, force=args.force)


# --------------------------------------------------------------- classify

def cmd_classify(args):
    from figlib.classify import run
    usage = run(args.data, args.model, jobs=min(args.jobs, 4), limit=args.limit, force=args.force, log=log)
    log(f"classify: done, usage {usage}")


# ------------------------------------------------------------------ build

_VENUE_ALIAS = {"nips": "NeurIPS", "s&p": "IEEE S&P", "atc": "USENIX ATC", "vr": "IEEE VR", "ubicomp": "IMWUT", "sigchi": "CHI"}


def _norm_venue(v: str) -> str:
    v = (v or "").strip()
    return _VENUE_ALIAS.get(v.lower(), v)


AWARD_TIERS = [
    # (rank, short label zh, short label en, regex on the seed note)
    (0, "最佳论文", "Best paper", r"best (research |student |industry |practical |conference |journal |short |long |regular |systems |applied )?paper|marr prize|best paper|günter enderle|mccalla|van toch|kleene|machtey|danny lewin"),
    (1, "优秀论文", "Outstanding paper", r"outstanding|distinguished|honou?rable|runner|nominee|finalist|community award|exemplary|highlight|award"),
    (2, "Spotlight", "Spotlight", r"spotlight"),
    (3, "Oral", "Oral", r"\boral\b"),
    (4, "高影响力", "High impact", r"high-impact|classic"),
]


def award_of(note: str, source: str):
    """(rank, zh, en) for the ordering inside a conference edition."""
    n = (note or "").lower()
    for rank, zh, en, rx in AWARD_TIERS:
        if re.search(rx, n):
            return rank, zh, en
    if source == "seed":
        return 4, "高影响力", "High impact"
    return 5, "", ""


_HASH_CACHE = {}


def _thumb_hash(data: Path, fig_id: str):
    """md5 of the thumbnail, cached in data/hashes.json by (name, mtime)."""
    import hashlib
    from figlib.extract import fig_path
    if not _HASH_CACHE:
        try:
            _HASH_CACHE.update(json.loads((data / "hashes.json").read_text()))
        except Exception:
            pass
        _HASH_CACHE["__dirty__"] = False
    p = fig_path(data / "figs", fig_id, thumb=True)
    if not p.exists():
        return None
    key = f"{p.name}:{int(p.stat().st_mtime)}"
    h = _HASH_CACHE.get(key)
    if h is None or not isinstance(h, list):
        from PIL import Image
        raw = p.read_bytes()
        try:
            with Image.open(p) as im:
                tw, th = im.size
        except Exception:
            tw = th = 0
        h = [hashlib.md5(raw).hexdigest(), tw, th, len(raw)]
        _HASH_CACHE[key] = h
        _HASH_CACHE["__dirty__"] = True
    return h


def _dedupe_images(data: Path, figs):
    """Within one paper keep the first figure per identical thumbnail (neighbouring captions
    sometimes locate the same picture; their panels then repeat as well)."""
    seen, out = set(), []
    for f in figs:
        h = _thumb_hash(data, f["fig_id"])
        if h and h[0] in seen:
            continue
        if h:
            seen.add(h[0])
            ex = f.setdefault("extra", {})
            ex["tw"], ex["th"] = h[1], h[2]
            ex["density"] = round(h[3] / max(1, h[1] * h[2]) * 1000, 1)   # webp bytes per kilo-pixel: text-heavy figures compress badly
            ex["v"] = h[0][:8]                                          # content version for cache busting
        out.append(f)
    return out


def _save_hash_cache(data: Path):
    if _HASH_CACHE.get("__dirty__"):
        keep = {k: v for k, v in _HASH_CACHE.items() if k != "__dirty__"}
        (data / "hashes.json").write_text(json.dumps(keep))


def load_index(data: Path):
    papers = []
    for jp in sorted((data / "index").glob("*.json")):
        rec = json.loads(jp.read_text())
        if "error" in rec or not rec.get("figures"):
            continue
        key = rec["paper_key"]
        meta = dict(rec["meta"])
        side = data / "pdf" / f"{key}.json"
        if side.exists():
            s = json.loads(side.read_text())
            if s.get("title"):
                meta["title"] = s["title"]
            if s.get("published"):
                meta["year"] = s["published"][:4]
            if s.get("venue_hint"):
                meta["venue"] = s["venue_hint"]
            if s.get("year_hint"):
                meta["year"] = s["year_hint"]     # curated seed note beats the PDF stamp
            meta["authors"] = s.get("authors", [])
            meta["citations"] = s.get("citations", 0)
            if not meta.get("year") and s.get("s2_year"):
                meta["year"] = s["s2_year"]
            meta["note"] = s.get("note", "")
            if not meta.get("arxiv_id") and ARXIV_ID.fullmatch(key):
                meta["arxiv_id"] = key
            meta["url"] = s.get("url", "")
        if side.exists():
            award = re.search(r"best|outstanding|distinguished|honou?rable|award", meta.get("note", ""), re.I)
            meta["source"] = "award" if award else ("seed" if s.get("url") else "local")
        else:
            meta["source"] = "local"
        from figlib.areas import area_of
        meta["venue"] = _norm_venue(meta.get("venue", ""))
        meta["award_rank"], meta["award_zh"], meta["award_en"] = award_of(meta.get("note", ""), meta["source"])
        meta["area"] = area_of(meta["venue"]) or ("preprint" if meta.get("arxiv_id") else "")
        figs = [f for f in rec["figures"] if not f.get("panels")]      # a split composite is shown as its panels
        figs = _dedupe_images(data, figs)                              # two captions can crop the same picture
        from figlib.extract import fig_path, classify as _kind, Figure as _Fig
        for f in figs:
            if not f.get("vision"):
                # caption heuristics may have improved since extraction: recompute the guess
                try:
                    f["kind"] = _kind(_Fig(fig_id=f["fig_id"], num=f["num"], page=f["page"], caption=f["caption"], bbox=tuple(f["bbox"]),
                                           full_width=f["full_width"], has_raster=f["has_raster"], n_drawings=f["n_drawings"],
                                           width_pt=f["width_pt"], height_pt=f["height_pt"]))
                except Exception:
                    pass
            v2 = f.get("vision2")
            if v2:
                base = f.get("vision") or {"tags": [], "summary_zh": ""}
                f["vision"] = {**base, "category": v2["category"], "chart_type": v2.get("chart_type", ""), "verified": v2["model"]}
            ver = f.get("extra", {}).get("v")
            q = f"?v={ver}" if ver else ""
            f["thumb"] = "figs/" + fig_path(data / "figs", f["fig_id"], thumb=True).name + q
            f["full"] = "figs/" + fig_path(data / "figs", f["fig_id"]).name + q
        papers.append({"paper_key": key, "meta": meta, "figures": figs, "dpi": rec.get("dpi", 170)})
    # the local library holds many drafts of the same paper: keep one per title
    # (the copy with the most figures; seeds win ties)
    best = {}
    for p in papers:
        k = re.sub(r"[^a-z0-9]+", "", p["meta"].get("title", "").lower())[:80] or p["paper_key"]
        cur = best.get(k)
        if cur is None or (len(p["figures"]), p["meta"]["source"] == "seed") > (len(cur["figures"]), cur["meta"]["source"] == "seed"):
            best[k] = p
    dropped = len(papers) - len(best)
    if dropped:
        log(f"dedupe: {dropped} duplicate copies folded by title")
    _save_hash_cache(data)
    return sorted(best.values(), key=lambda p: p["paper_key"])


def slim_index(index):
    """The page only needs a slice of each record (index.json keeps everything)."""
    KEEP = ("fig_id", "num", "page", "caption", "kind", "has_raster", "full_width", "panel", "n_panels", "thumb", "full")
    return {**index, "papers": [
        {"paper_key": p["paper_key"], "meta": {**{k: v for k, v in p["meta"].items() if k != "authors"}, "authors": (p["meta"].get("authors") or [])[:6]},
         "figures": [{**{k: f[k] for k in KEEP if k in f},
                      "extra": {**{k: f.get("extra", {}).get(k, 0) for k in ("px_w", "px_h", "rotated", "tw", "th", "density")}, "dpi": p.get("dpi", 170)},
                      **({"vision": {k: f["vision"].get(k) for k in ("category", "chart_type", "tags", "summary_zh", "verified")}} if f.get("vision") else {})}
                     for f in p["figures"]]}
        for p in index["papers"]]}


def cmd_site(args):
    """Write a self-contained static site: index.html + data shards + hard-linked figures.

    Upload the directory to any static host (Cloudflare Pages + R2, S3 + CloudFront,
    an nginx box). Pass --img-base when the figures live on another host (Cloudflare
    Pages caps a deployment at 20,000 files, so figures go to R2 there)."""
    data = args.data
    out = Path(args.out)
    (out / "data").mkdir(parents=True, exist_ok=True)
    papers = load_index(data)
    drop = set(x for x in args.exclude_source.split(",") if x)
    papers = [p for p in papers if p["meta"].get("source") not in drop]
    index = {"generated": time.strftime("%Y-%m-%d %H:%M"), "n_papers": len(papers),
             "n_figures": sum(len(p["figures"]) for p in papers)}
    from figlib.areas import areas_table
    index["areas"] = areas_table()
    slim = slim_index({**index, "papers": papers})
    # shards of ~800 papers each; shard 0 carries the header fields
    n = max(1, args.shards)
    per = -(-len(slim["papers"]) // n)
    shard_urls = []
    for i in range(n):
        chunk = slim["papers"][i * per:(i + 1) * per]
        if not chunk and i > 0:
            break
        body = {**{k: v for k, v in slim.items() if k != "papers"}, "papers": chunk} if i == 0 else {"papers": chunk}
        name = f"data/index-{i}.json"
        (out / name).write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
        shard_urls.append(name)
    tpl = (HERE / "gallery.html").read_text()
    site_cfg = json.dumps({"img_base": args.img_base, "shards": shard_urls})
    page = tpl.replace('<script id="data" type="application/json">__DATA__</script>',
                       f"<script>window.SITE = {site_cfg};</script>").replace("__MODE__", "site")
    (out / "index.html").write_text(page)
    (out / "robots.txt").write_text("User-agent: *\nAllow: /\n")
    for asset in (HERE / "assets").iterdir():
        shutil.copy2(asset, out / asset.name)
    (out / "_headers").write_text("/figs/*\n  Cache-Control: public, max-age=31536000, immutable\n/data/*\n  Cache-Control: public, max-age=3600\n")
    linked = 0
    if not args.no_figs:
        figs_out = out / "figs"
        figs_out.mkdir(exist_ok=True)
        live = {f["fig_id"] for p in papers for f in p["figures"]}
        for src in (data / "figs").iterdir():
            fid = re.sub(r"(\.thumb)?\.(png|webp)$", "", src.name)
            if fid not in live:
                continue
            dst = figs_out / src.name
            if dst.exists():
                if os.path.samefile(src, dst):
                    continue
                dst.unlink()
            try:
                os.link(src, dst)          # same filesystem: no extra disk
            except OSError:
                shutil.copy2(src, dst)
            linked += 1
    log(f"site: {len(papers)} papers, {index['n_figures']} figures, {len(shard_urls)} shards, {linked} figure files linked -> {out}")


def cmd_build(args):
    data = args.data
    papers = load_index(data)
    n_fig = sum(len(p["figures"]) for p in papers)
    from figlib.areas import areas_table
    index = {"generated": time.strftime("%Y-%m-%d %H:%M"), "n_papers": len(papers), "n_figures": n_fig,
             "areas": areas_table(), "papers": papers}
    (data / "index.json").write_text(json.dumps(index, ensure_ascii=False))
    tpl = (HERE / "gallery.html").read_text()
    slim = slim_index(index)
    # the index is inlined inside <script type="application/json">: escape the
    # three characters that could close the element early (captions are untrusted)
    payload = (json.dumps(slim, ensure_ascii=False, separators=(",", ":"))
               .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e"))
    page = tpl.replace("__DATA__", payload).replace("__MODE__", "files")
    (data / "gallery.html").write_text(page)
    log(f"build: {len(papers)} papers, {n_fig} figures -> {data / 'gallery.html'}")
    if getattr(args, "prune", False):
        # every figure any index record references stays, including copies folded by dedupe
        live = set()
        for jp in (data / "index").glob("*.json"):
            try:
                live.update(f["fig_id"] for f in json.loads(jp.read_text()).get("figures", []))
            except Exception:
                pass
        gone = 0
        for png in list((data / "figs").glob("*.png")) + list((data / "figs").glob("*.webp")):
            fid = re.sub(r"(\.thumb)?\.(png|webp)$", "", png.name)
            if fid not in live:
                png.unlink()
                gone += 1
        log(f"prune: removed {gone} stale PNGs")


# ------------------------------------------------------------------ main

def main(argv=None):
    ap = argparse.ArgumentParser(prog="figlib")
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch")
    f.add_argument("--seeds", action="append", help="seed list file (repeatable)")
    f.add_argument("--ids", nargs="*")
    f.add_argument("--delay", type=float, default=3.0)
    f.add_argument("--no-api", action="store_true", help="skip the arXiv metadata API (rate limited from some hosts)")
    f.add_argument("--min-free-gb", type=float, default=2.0, help="stop downloading when the disk has less than this free")
    f.add_argument("--refetch", action="store_true", help="re-download PDFs that prune-pdfs deleted")
    f.set_defaults(fn=cmd_fetch)

    e = sub.add_parser("extract")
    e.add_argument("--pdf-dir", action="append", help="extra directory of PDFs (repeatable)")
    e.add_argument("--dpi", type=int, default=300)
    e.add_argument("--jobs", type=int, default=4)
    e.add_argument("--force", action="store_true")
    e.set_defaults(fn=cmd_extract)

    b = sub.add_parser("build")
    b.add_argument("--prune", action="store_true", help="delete PNGs in figs/ that no index entry references")
    b.set_defaults(fn=cmd_build)

    cw = sub.add_parser("convert-webp", help="re-encode existing PNG figures as WebP (one-off migration)")
    cw.set_defaults(fn=cmd_convert_webp)

    en = sub.add_parser("enrich", help="fill authors / titles / citations from Semantic Scholar")
    en.add_argument("--force", action="store_true")
    en.set_defaults(fn=cmd_enrich)

    rt = sub.add_parser("rethumb", help="regenerate thumbnails from the full-size images")
    rt.add_argument("--px", type=int, default=960)
    rt.add_argument("--force", action="store_true")
    rt.set_defaults(fn=cmd_rethumb)

    pr = sub.add_parser("prune-pdfs", help="delete PDFs whose figures are already extracted (fetch --refetch restores)")
    pr.set_defaults(fn=cmd_prune_pdfs)

    vf = sub.add_parser("verify", help="second opinion from a stronger vision model for the confusable categories")
    vf.add_argument("--model", default=os.environ.get("FIGLIB_VERIFY_MODEL", "gpt-5-mini"))
    vf.add_argument("--cats", default="method,background,other", help="comma list of effective categories to re-check")
    vf.add_argument("--jobs", type=int, default=3)
    vf.add_argument("--limit", type=int)
    vf.add_argument("--labeled-only", action="store_true", help="skip figures that only have a caption-based guess")
    vf.set_defaults(fn=cmd_verify)

    sp = sub.add_parser("split", help="cut multi-panel data figures into panels (needs labels first)")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_split)

    st = sub.add_parser("site", help="write a static site directory for public hosting")
    st.add_argument("--out", default=str(DEFAULT_DATA / "site"))
    st.add_argument("--img-base", default="", help="URL prefix for figures when they live on another host (ends with /)")
    st.add_argument("--shards", type=int, default=4)
    st.add_argument("--no-figs", action="store_true", help="do not link figures into the site dir")
    st.add_argument("--exclude-source", default="local", help="comma list of sources left out of the public site (default: the local library)")
    st.set_defaults(fn=cmd_site)

    r = sub.add_parser("remeta", help="recompute titles / venues from the PDFs")
    r.set_defaults(fn=cmd_remeta)

    c = sub.add_parser("classify", help="label figures with a vision model (needs OPENAI_API_KEY)")
    c.add_argument("--model", default=os.environ.get("FIGLIB_VISION_MODEL", "gpt-4.1-mini"))
    c.add_argument("--jobs", type=int, default=3)
    c.add_argument("--limit", type=int)
    c.add_argument("--force", action="store_true")
    c.set_defaults(fn=cmd_classify)

    a = sub.add_parser("all")
    a.add_argument("--pdf-dir", action="append")
    a.set_defaults(fn=None)

    args = ap.parse_args(argv)
    if args.cmd == "all":
        seeds = [str(x) for x in sorted((HERE.parent / "seeds").glob("*.txt"))]
        cmd_fetch(argparse.Namespace(data=args.data, seeds=seeds, ids=None, delay=3.0, no_api=True, min_free_gb=2.0, refetch=False))
        cmd_extract(argparse.Namespace(data=args.data, pdf_dir=args.pdf_dir, dpi=300, jobs=2, force=False))
        model = os.environ.get("FIGLIB_VISION_MODEL", "gpt-4.1-mini")
        cmd_classify(argparse.Namespace(data=args.data, model=model, jobs=3, limit=None, force=False))
        cmd_split(argparse.Namespace(data=args.data, force=False))
        cmd_classify(argparse.Namespace(data=args.data, model=model, jobs=3, limit=None, force=False))
        cmd_build(argparse.Namespace(data=args.data, prune=True))
    else:
        args.fn(args)


if __name__ == "__main__":
    main()
