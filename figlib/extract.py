"""Locate figures in a PDF and render each one to PNG.

Strategy (no ML, no external service):
1. Find caption blocks: text blocks whose first line starts with
   "Figure N" / "Fig. N". "Table N" captions are also located, but only
   to bound the search region of neighbouring figures.
2. Decide whether the caption spans the full text width or a single column.
3. Collect every visual element (embedded raster image or vector drawing)
   that sits above the caption inside the same horizontal band, and walk
   upward from the caption while the elements stay vertically contiguous.
4. Render the union box with PyMuPDF at the requested DPI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

import pymupdf

CAPTION_RE = re.compile(
    r"^\s*(?P<kind>Figure|Fig\.?|Table|图|表)\s*"
    r"(?P<num>(?:\d+(?:\.\d+)*|[IVXLC]+|[A-Z]\.?\d+)(?:\s*\([a-z]\))?)"
    r"(?P<sep>\s*[.:|：。]|\s+|$)(?P<rest>.*)$", re.I | re.S
)
# "Figure 3 shows ..." is a body sentence, not a caption
BODY_VERBS = re.compile(
    r"^(shows?|illustrates?|presents?|depicts?|demonstrates?|compares?|plots?|reports?|provides?|"
    r"gives?|summari[sz]es?|displays?|lists?|describes?|contains?|visuali[sz]es?|highlights?|"
    r"and|to|in|of|for|is|are|was|were|has|have|also)\b")   # case-sensitive: captions start with a capital
VENUE_RE = re.compile(
    r"\b(NeurIPS|NIPS|ICLR|ICML|CVPR|ICCV|ECCV|ACL|EMNLP|NAACL|COLING|CHI|UIST|"
    r"SIGGRAPH(?: Asia)?|TOG|AAAI|IJCAI|KDD|WWW|TPAMI|JMLR|TACL)\b"
)
ARXIV_RE = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5})(v\d+)?", re.I)
YEAR_RE = re.compile(r"\b(20[0-3][0-9])\b")

# rendered figures are stored as WebP (quality 90): charts compress to about a
# third of PNG with no visible loss; older PNG files are still read if present
FIG_EXT = ".webp"
WEBP_QUALITY = 92


def fig_path(figs_dir, fig_id, thumb=False):
    """Existing image file for a figure id (WebP preferred, PNG fallback), or the WebP path if neither exists."""
    figs_dir = Path(figs_dir)
    suffix = ".thumb" if thumb else ""
    for ext in (FIG_EXT, ".png"):
        p = figs_dir / f"{fig_id}{suffix}{ext}"
        if p.exists():
            return p
    return figs_dir / f"{fig_id}{suffix}{FIG_EXT}"


def save_pix(pix, path):
    """Save a PyMuPDF pixmap as WebP through PIL (PyMuPDF itself only writes PNG/JPEG/...)."""
    from PIL import Image
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    im.save(str(path), quality=WEBP_QUALITY, method=4)
    return im.size


# gap (pt) allowed between caption and the figure, and between stacked elements
CAPTION_GAP = 60.0
ELEMENT_GAP = 28.0
MIN_W, MIN_H = 70.0, 40.0
PAD = 3.0


@dataclass
class Caption:
    kind: str          # figure | table
    num: str
    text: str
    bbox: tuple        # x0, y0, x1, y1
    full_width: bool
    column: int        # 0 = left / single, 1 = right, -1 = full


@dataclass
class Figure:
    fig_id: str
    num: str
    page: int          # 1-based
    caption: str
    bbox: tuple
    full_width: bool
    has_raster: bool
    n_drawings: int
    width_pt: float
    height_pt: float
    kind: str = "other"     # teaser | method | result | example | other
    extra: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


# ---------------------------------------------------------------- captions

def _text_blocks(page):
    d = page.get_text("dict")
    out = []
    for b in d["blocks"]:
        if b["type"] != 0:
            continue
        lines = []
        for ln in b["lines"]:
            lines.append("".join(sp["text"] for sp in ln["spans"]))
        txt = "\n".join(lines).strip()
        if txt:
            out.append((pymupdf.Rect(b["bbox"]), txt, b))
    return out


def _text_extent(blocks):
    """Left/right extent of body text on the page."""
    xs0 = sorted(r.x0 for r, _, _ in blocks)
    xs1 = sorted(r.x1 for r, _, _ in blocks)
    if not xs0:
        return None
    # 5th / 95th percentile to ignore stray marks
    i0 = int(len(xs0) * 0.05)
    i1 = max(0, int(len(xs1) * 0.95) - 1)
    return xs0[i0], xs1[i1]


def find_captions(page, blocks, extent):
    caps = []
    if extent is None:
        return caps
    left, right = extent
    width = right - left
    mid = (left + right) / 2
    for rect, txt, _ in blocks:
        flat = re.sub(r"\s+", " ", txt).strip()
        m = CAPTION_RE.match(flat)
        if not m:
            continue
        # a caption is a short paragraph, not a body paragraph that merely
        # begins with "Figure 3 shows ..."
        if not m.group("sep").strip() and BODY_VERBS.match(m.group("rest")):
            continue
        if rect.height > page.rect.height * 0.35:
            continue
        kind = "table" if m.group("kind").lower() in ("table", "表") else "figure"
        full = rect.width > 0.62 * width or (rect.x0 < mid - 20 and rect.x1 > mid + 20)
        if full:
            col = -1
        else:
            col = 1 if (rect.x0 + rect.x1) / 2 > mid else 0
        num = re.sub(r"\s+", "", m.group("num"))
        caps.append(Caption(kind, num, flat, tuple(rect), full, col))
    return caps


# ---------------------------------------------------------------- visuals

def _visual_rects(page, layout=None):
    """Image + vector rectangles. Images come from the text dict's image blocks,
    whose bboxes respect PDF clip paths (get_image_info() returns the raw
    transform box, which for tiled or clipped images can cover the page)."""
    rects = []
    layout = layout or page.get_text("dict")
    for b in layout["blocks"]:
        if b["type"] != 1:
            continue
        r = pymupdf.Rect(b["bbox"]) & page.rect
        if r.is_empty or r.width < 4 or r.height < 4:
            continue
        rects.append((r, "image"))
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    for dr in drawings:
        r = pymupdf.Rect(dr["rect"]) & page.rect
        if r.is_empty:
            continue
        # ignore full-width hairlines (section rules, header rules)
        if r.height < 1.5 and r.width > page.rect.width * 0.55:
            continue
        if r.width < 1 and r.height < 1:
            continue
        rects.append((r, "draw"))
    return rects


def _band_for(cap: Caption, extent, page_rect):
    left, right = extent
    mid = (left + right) / 2
    if cap.column == -1:
        # full-width figures often run into the margins: use the page, not the text extent
        return page_rect.x0 + 8, page_rect.x1 - 8
    if cap.column == 0:
        return left - 10, mid + 4
    return mid - 4, right + 10


def _header_bottom(page, blocks):
    """Bottom edge of the running header (short text lines in the top 8%)."""
    limit = page.rect.y0 + page.rect.height * 0.08
    hb = page.rect.y0
    for r, txt, _ in blocks:
        if r.y1 <= limit and r.height < 24 and "\n" not in txt.strip():
            hb = max(hb, r.y1)
    return hb


def _region_top(cap: Caption, others, band, page_rect, floor=None, visuals=()):
    """Lowest edge of anything above this caption that belongs to another
    figure or table: another caption, or the body of a table whose caption
    sits above it (table captions go on top, so the body hangs below them)."""
    x0, x1 = band
    top = page_rect.y0 if floor is None else floor
    cy0 = cap.bbox[1]
    for o in others:
        if o is cap:
            continue
        ox0, oy0, ox1, oy1 = o.bbox
        if oy1 > cy0 - 5:
            continue
        if ox1 < x0 or ox0 > x1:
            continue
        bottom = oy1
        if o.kind == "table":
            # walk downward from the table caption through contiguous elements
            # tables are rules + text, so only vector elements count; a raster
            # figure sitting right under a table must not be absorbed
            below = sorted((r for r, t in visuals if t == "draw" and r.y0 >= oy1 - 2 and r.y1 <= cy0 - 2
                            and r.x1 > x0 and r.x0 < x1), key=lambda r: r.y0)
            cur = oy1
            for r in below:
                if r.y0 > cur + ELEMENT_GAP:
                    break
                cur = max(cur, r.y1)
            bottom = cur
        top = max(top, bottom)
    return top


def locate_figure(page, cap: Caption, all_caps, blocks, extent, visuals):
    band = _band_for(cap, extent, page.rect)
    top = _region_top(cap, all_caps, band, page.rect, floor=_header_bottom(page, blocks), visuals=visuals)
    cx0, cy0, cx1, cy1 = cap.bbox
    bx0, bx1 = band

    page_area = page.rect.width * page.rect.height
    cand = []
    band_rect = pymupdf.Rect(bx0, top - 2, bx1, cy0 + 2)
    for r, t in visuals:
        if t == "image" and r.width * r.height > 0.8 * page_area:
            # a page-sized image (scanned or translated PDF): fall back to text layout
            return _locate_by_text_gap(page, cap, blocks, band, top)
        if r.y1 > cy0 + 2 or r.y0 < top - 2:
            continue
        visible = r & band_rect
        if visible.is_empty or visible.width < 1:
            continue
        # for a column figure, drop elements that mostly live in the other column,
        # and keep only the part inside our band so a full-width rule does not
        # drag the crop across both columns
        if cap.column != -1 and visible.width < 0.5 * r.width:
            continue
        cand.append((visible, t))
    if not cand:
        return None

    cand.sort(key=lambda it: -it[0].y1)
    # walk upward from the caption
    cur_top = cy0
    chosen = []
    for r, t in cand:
        allowed = CAPTION_GAP if not chosen else ELEMENT_GAP
        if r.y1 < cur_top - allowed:
            break
        chosen.append((r, t))
        cur_top = min(cur_top, r.y0)
    if not chosen:
        return None

    box = pymupdf.Rect(chosen[0][0])
    for r, _ in chosen[1:]:
        box |= r
    # a table body directly above (between a Table caption and us) would
    # already be excluded by `top`; but a text-only figure yields nothing.
    if box.width < MIN_W or box.height < MIN_H:
        return None

    # widen to include text labels (axis titles, panel letters) that hang partly
    # outside the drawn box: a block counts when at least half of it overlaps
    # the box and it is not a body-width paragraph; never grow into the caption
    grew = True
    while grew:
        grew = False
        for r, txt, _ in blocks:
            if r.x0 < bx0 - 2 or r.x1 > bx1 + 2 or r.width >= 0.9 * (bx1 - bx0):
                continue
            if r.y1 > cy0 - 1 or r.y0 < top - 2:
                continue
            inter = r & pymupdf.Rect(box.x0 - 6, box.y0 - 6, box.x1 + 6, box.y1 + 6)
            if inter.is_empty:
                continue
            if inter.get_area() >= 0.5 * r.get_area() and not (r in box):
                box |= r
                grew = True
    box = pymupdf.Rect(box.x0 - PAD, box.y0 - PAD, box.x1 + PAD, min(box.y1 + PAD, cy0 - 1))
    box &= page.rect
    has_raster = any(t == "image" for _, t in chosen)
    n_draw = sum(1 for _, t in chosen if t == "draw")
    return box, has_raster, n_draw


def _locate_by_text_gap(page, cap: Caption, blocks, band, top):
    """Figure = the text-free gap between the last body paragraph and the caption."""
    bx0, bx1 = band
    cx0, cy0, cx1, cy1 = cap.bbox
    floor = top
    for r, txt, _ in blocks:
        if r.y1 > cy0 - 4 or r.y1 < top:
            continue
        if r.x1 < bx0 or r.x0 > bx1:
            continue
        if r.width < 0.45 * (bx1 - bx0):
            continue        # short lines are probably labels inside the figure
        floor = max(floor, r.y1)
    box = pymupdf.Rect(bx0 + 6, floor + 4, bx1 - 6, cy0 - 1) & page.rect
    if box.width < MIN_W or box.height < MIN_H:
        return None
    return box, True, 0


# ---------------------------------------------------------------- classify

METHOD_WORDS = re.compile(
    r"\b(overview|framework|pipeline|architecture|illustration|schematic|"
    r"workflow|diagram|structure of|block diagram|flowchart)\b", re.I)
RESULT_WORDS = re.compile(
    r"\b(results?|accuracy|performance|ablation|comparison|compar(e|ing|ed)|curves?|"
    r"scaling|vs\.?|versus|scores?|error|loss|reward|win rate|benchmark|as a function of|"
    r"trade-?off|distributions?|histogram|trajector(y|ies)|over time|time steps?|iterations?|epochs?|"
    r"latency|throughput|speedup|precision|recall|f1|auc|bleu|perplexity|rate|ratio|percentage|"
    r"plot|plotted|axis|x-axis|y-axis|log scale|per-|median|mean|average|std|variance|confidence)\b", re.I)
EXAMPLE_WORDS = re.compile(
    r"\b(example|qualitative|samples?|generated|visualization|visualisation|"
    r"case stud(y|ies)|screenshot|interface|prompt)\b", re.I)


PROMPT_WORDS = re.compile(r"\b(prompt|instruction template|system message)s?\b", re.I)


def classify(fig: Figure) -> str:
    c = fig.caption
    if PROMPT_WORDS.search(c) and not fig.has_raster and fig.n_drawings < 12:
        return "prompt"
    if fig.num == "1" and fig.page <= 2 and not RESULT_WORDS.search(c):
        return "teaser"
    if RESULT_WORDS.search(c) and not fig.has_raster:
        return "result"                       # vector chart with quantitative words: a plot
    if METHOD_WORDS.search(c):
        return "method"
    if EXAMPLE_WORDS.search(c) and fig.has_raster:
        return "example"
    if RESULT_WORDS.search(c):
        return "result"
    if EXAMPLE_WORDS.search(c):
        return "example"
    if fig.has_raster:
        return "example"
    return "other"


# ---------------------------------------------------------------- paper meta

def paper_meta(doc, fallback_title=""):
    meta = {"title": "", "arxiv_id": "", "venue": "", "year": ""}
    t = (doc.metadata or {}).get("title") or ""
    if t and not re.search(r"\.(dvi|tex|pdf)$|untitled|^\s*$", t, re.I):
        meta["title"] = t.strip()
    if doc.page_count:
        p0 = doc[0]
        txt = p0.get_text()
        m = ARXIV_RE.search(txt)
        if m:
            meta["arxiv_id"] = m.group(1)
        v = VENUE_RE.search(txt[:3000])
        if v:
            meta["venue"] = v.group(1)
        # year: prefer arXiv stamp line, then any year on page 1
        ym = re.search(r"arXiv:\d{4}\.\d{4,5}(?:v\d+)?\s*\[[^\]]*\]\s*\d{1,2}\s+\w+\s+(20\d\d)", txt)
        if ym:
            meta["year"] = ym.group(1)
        else:
            yy = YEAR_RE.findall(txt[:3000])
            if yy:
                meta["year"] = max(yy)
        if not meta["title"]:
            meta["title"] = _largest_text(p0)
    if not meta["title"]:
        meta["title"] = fallback_title
    return meta


def _largest_text(page):
    """Title = the lines set in the largest type in the upper half of page 1.

    Works line by line (spans joined as-is) so small-caps titles, whose initial
    letters sit in their own larger spans, come out whole. The rotated arXiv
    stamp in the margin is skipped.
    """
    d = page.get_text("dict")
    limit = page.rect.y0 + page.rect.height * 0.5
    lines = []
    for b in d["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b["lines"]:
            if abs(ln.get("dir", (1, 0))[0]) < 0.9 or ln["bbox"][1] > limit:
                continue
            txt = "".join(sp["text"] for sp in ln["spans"]).strip()
            if len(txt) < 3 or ARXIV_RE.search(txt):
                continue
            lines.append((max(sp["size"] for sp in ln["spans"]), ln["bbox"][1], txt))
    if not lines:
        return ""
    best = max(sz for sz, _, _ in lines)
    parts = [t for sz, y, t in sorted(lines, key=lambda x: x[1]) if sz >= 0.85 * best]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()[:200]


def _sideways(page, box):
    """Return the rotation (degrees, PIL convention: positive = counter-clockwise)
    that makes the text inside `box` read horizontally, or 0 if it already does.
    Landscape figures are usually turned 90 degrees on a portrait page; their
    labels then run bottom-to-top (dir (0,-1)) or top-to-bottom (dir (0,1))."""
    up = down = horiz = 0
    for b in page.get_text("dict", clip=box)["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b["lines"]:
            n = sum(len(sp["text"].strip()) for sp in ln["spans"])
            if n < 2:
                continue
            dx, dy = ln.get("dir", (1, 0))
            if abs(dx) >= 0.5:
                horiz += n
            elif dy < 0:
                up += n
            else:
                down += n
    vert = up + down
    if vert + horiz < 20 or vert <= 1.5 * horiz:
        return 0
    return -90 if up >= down else 90      # text runs upward -> turn clockwise


def _save_rotated(pix, path, angle):
    from PIL import Image
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    im = im.rotate(angle, expand=True)
    im.save(str(path), quality=WEBP_QUALITY, method=4)
    return im.size


# ---------------------------------------------------------------- driver

# ---------------------------------------------------------------- tables

TABLE_CAP_RE = re.compile(r"^\s*(Figure|Fig\.?|Table|图|表)\s*\d", re.I)


def _page_lines(page):
    """[(rect, max word gap, text)] one entry per text line."""
    groups = {}
    for w in page.get_text("words"):            # x0, y0, x1, y1, word, block, line, wordno
        groups.setdefault((w[5], w[6]), []).append(w)
    out = []
    for ws in groups.values():
        ws.sort(key=lambda w: w[0])
        r = pymupdf.Rect(ws[0][:4])
        for w in ws[1:]:
            r |= pymupdf.Rect(w[:4])
        gaps = [ws[i + 1][0] - ws[i][2] for i in range(len(ws) - 1)]
        out.append((r, max(gaps) if gaps else 0.0, " ".join(w[4] for w in ws)))
    out.sort(key=lambda t: (t[0].y0, t[0].x0))
    return out


def _page_rules(page):
    """Thin horizontal rules (booktabs) and vertical grid lines."""
    rules = []
    for d in page.get_drawings():
        r = d.get("rect")
        if r is None:
            continue
        if (r.height <= 2.5 and r.width >= 30) or (r.width <= 2.5 and r.height >= 20):
            rules.append(pymupdf.Rect(r))
    return rules


def locate_table(cap: Caption, band, lines, rules, all_caps):
    """Body of a 'Table N' caption: the run of table-like text lines (cells with wide gaps, narrow
    lines, lines between rules) right below or above the caption, plus the rules that frame them.
    Stops at another caption, a clear gap, a bottom rule followed by space, or a plain paragraph line."""
    cb = pymupdf.Rect(cap.bbox)
    bw = band[1] - band[0]
    others = [pymupdf.Rect(c.bbox) for c in all_caps if c is not cap]

    def in_band(r):
        return min(r.x1, band[1]) - max(r.x0, band[0]) > 0.3 * min(r.width, bw)

    hr = [r for r in rules if r.height <= 2.5 and in_band(r)]

    def walk(direction):
        cand = [l for l in lines if in_band(l[0]) and (l[0].y0 >= cb.y1 - 1 if direction > 0 else l[0].y1 <= cb.y0 + 1)]
        cand.sort(key=lambda l: l[0].y0 * direction)
        taken, edge = [], (cb.y1 if direction > 0 else cb.y0)
        for r, maxgap, text in cand:
            gap = (r.y0 - edge) if direction > 0 else (edge - r.y1)
            lo, hi = (edge, r.y0) if direction > 0 else (r.y1, edge)
            between = [x for x in hr if lo - 1 <= x.y0 <= hi + 1]
            if gap > 30 and not between:
                break
            if TABLE_CAP_RE.match(text) or any(o.intersects(r) for o in others):
                break
            if taken and between:               # a rule and then clear space: the table ended at that rule
                far = (r.y0 - max(x.y1 for x in between)) if direction > 0 else (min(x.y0 for x in between) - r.y1)
                if far > 9:
                    break
            tabular = maxgap >= 12 or (r.width < 0.8 * bw and (maxgap >= 6 or re.search(r"\d", text)))
            if not tabular and r.width >= 0.8 * bw:
                # a full-width plain line belongs to the table only when rules hug it on both sides
                near_above = any(r.y0 - 25 <= x.y1 <= r.y0 + 1 for x in hr)
                near_below = any(r.y1 - 1 <= x.y0 <= r.y1 + 25 for x in hr)
                if not (near_above and near_below):
                    break
            taken.append(r)
            edge = r.y1 if direction > 0 else r.y0
        return taken

    best = None
    for direction in (1, -1):
        taken = walk(direction)
        if len(taken) >= 2 and (best is None or len(taken) > len(best)):
            best = taken
    if not best:
        return None
    box = pymupdf.Rect(best[0])
    for r in best[1:]:
        box |= r
    for r in rules:                                   # frame rules just outside the text
        if in_band(r) and r.y0 >= box.y0 - 8 and r.y1 <= box.y1 + 8:
            box |= r
    box = box + (-3, -3, 3, 3)
    if box.width < 60 or box.height < 18:
        return None
    return box


def extract_tables(page, pno, paper_key, caps, blocks, extent, out_dir, dpi, thumb_px, seen):
    """Render every located 'Table N' on the page; returns [Figure] with kind 'table'."""
    tab_caps = [c for c in caps if c.kind == "table"]
    if not tab_caps:
        return []
    lines, rules = _page_lines(page), _page_rules(page)
    out = []
    for cap in tab_caps:
        slug = re.sub(r"[^0-9A-Za-z.]+", "", cap.num)
        if (pno, "tab", slug) in seen:
            continue
        band = _band_for(cap, extent, page.rect)
        box = locate_table(cap, band, lines, rules, caps)
        if box is None:
            continue
        seen.add((pno, "tab", slug))
        fig_id = f"{paper_key}__p{pno + 1}__tab{slug}"
        pix = page.get_pixmap(clip=box, dpi=dpi, alpha=False, annots=False)
        scale = min(1.0, thumb_px / max(1, pix.width))
        tp = page.get_pixmap(clip=box, dpi=max(36, int(dpi * scale)), alpha=False, annots=False)
        w, h = save_pix(pix, out_dir / f"{fig_id}{FIG_EXT}")
        save_pix(tp, out_dir / f"{fig_id}.thumb{FIG_EXT}")
        fig = Figure(fig_id=fig_id, num=cap.num, page=pno + 1, caption=cap.text,
                     bbox=tuple(round(v, 1) for v in box), full_width=cap.full_width,
                     has_raster=False, n_drawings=len(rules), width_pt=round(box.width, 1), height_pt=round(box.height, 1),
                     kind="table", extra={"px_w": w, "px_h": h, "rotated": 0})
        out.append(fig)
    return out


def extract_tables_only(path, out_dir, paper_key, dpi=300, thumb_px=960):
    """Tables of one PDF (used to backfill papers extracted before tables existed)."""
    doc = pymupdf.open(path)
    figs, seen = [], set()
    for pno in range(doc.page_count):
        page = doc[pno]
        blocks = _text_blocks(page)
        extent = _text_extent(blocks)
        caps = find_captions(page, blocks, extent)
        figs += extract_tables(page, pno, paper_key, caps, blocks, extent, out_dir, dpi, thumb_px, seen)
    doc.close()
    return figs


def extract_pdf(path, out_dir, paper_key, dpi=300, thumb_px=960, max_pages=None):
    """Return (meta, [Figure]) and write PNGs into out_dir."""
    doc = pymupdf.open(path)
    meta = paper_meta(doc, fallback_title=paper_key)
    figures = []
    seen = set()
    n_pages = doc.page_count if max_pages is None else min(doc.page_count, max_pages)
    for pno in range(n_pages):
        page = doc[pno]
        blocks = _text_blocks(page)
        extent = _text_extent(blocks)
        caps = find_captions(page, blocks, extent)
        figures += extract_tables(page, pno, paper_key, caps, blocks, extent, out_dir, dpi, thumb_px, seen)
        fig_caps = [c for c in caps if c.kind == "figure"]
        if not fig_caps:
            continue
        visuals = _visual_rects(page)
        for cap in fig_caps:
            slug = re.sub(r"[^0-9A-Za-z.]+", "", cap.num)
            if (pno, slug) in seen:
                continue
            loc = locate_figure(page, cap, caps, blocks, extent, visuals)
            if loc is None:
                continue
            box, has_raster, n_draw = loc
            seen.add((pno, slug))
            fig_id = f"{paper_key}__p{pno + 1}__fig{slug}"
            fig = Figure(
                fig_id=fig_id, num=cap.num, page=pno + 1, caption=cap.text,
                bbox=tuple(round(v, 1) for v in box), full_width=cap.full_width,
                has_raster=has_raster, n_drawings=n_draw,
                width_pt=round(box.width, 1), height_pt=round(box.height, 1),
            )
            fig.kind = classify(fig)
            angle = _sideways(page, box)
            pix = page.get_pixmap(clip=box, dpi=dpi, alpha=False, annots=False)
            # thumbnail: scale so the longer side after rotation is ~thumb_px
            long_side = max(pix.width, pix.height) if angle else pix.width
            scale = min(1.0, thumb_px / max(1, long_side))
            tp = page.get_pixmap(clip=box, dpi=max(36, int(dpi * scale)), alpha=False, annots=False)
            if angle:
                w, h = _save_rotated(pix, out_dir / f"{fig_id}{FIG_EXT}", angle)
                _save_rotated(tp, out_dir / f"{fig_id}.thumb{FIG_EXT}", angle)
            else:
                w, h = save_pix(pix, out_dir / f"{fig_id}{FIG_EXT}")
                save_pix(tp, out_dir / f"{fig_id}.thumb{FIG_EXT}")
            fig.extra = {"px_w": w, "px_h": h, "rotated": angle}
            figures.append(fig)
    doc.close()
    return meta, figures
