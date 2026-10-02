"""Split multi-panel data figures into single panels (recursive XY-cut).

A chart figure often holds several subplots side by side. The library wants
one entry per chart type, so a composite "scatter + line" figure is cut along
its blank gutters into panels, each of which is then labelled on its own.
Figures the vision model called `data`, and figures a caption sorted as charts, are split;
method diagrams and example grids keep their whitespace and stay whole.

Algorithm: binarise the rendered PNG (ink = pixel darker than 235), project
onto rows, find blank runs at least `min_gap` tall, cut there; then the same on
columns inside each piece; recurse up to `max_depth`. Pieces smaller than
`min_side` pixels on either side (colour bars, shared axis titles, legends)
are dropped. Each surviving panel is trimmed to its ink bounding box plus a
margin. A figure is only replaced by its panels when at least two survive.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

INK = 235
MIN_GAP_FRAC = 0.004      # blank run must be this fraction of the axis length
MIN_GAP_PX = 7
BLANK_TOL = 0.004         # a row/col with ink on <= this fraction of its pixels counts as blank
MIN_SIDE = 190            # px, at 300 dpi that is about 1.6 cm
MAX_DEPTH = 3
MAX_PANELS = 16
ABSORB_PX = 70            # a thin strip this close to a panel (about 6 mm at 300 dpi) is its label, not a separate thing
MAX_SUBPLOTS = 6          # more similar cells than this inside one block: keep the grid whole
MARGIN = 14


def _blank_runs(profile, min_gap):
    """Yield (start, end) of runs where profile == 0 with length >= min_gap."""
    runs, start = [], None
    for i, v in enumerate(profile):
        if v == 0 and start is None:
            start = i
        elif v != 0 and start is not None:
            if i - start >= min_gap:
                runs.append((start, i))
            start = None
    if start is not None and len(profile) - start >= min_gap:
        runs.append((start, len(profile)))
    return runs


MAJOR_GAP_FRAC = 0.018    # a gutter this wide (fraction of the axis) separates different things, not subplots of one grid
MAJOR_GAP_PX = 22


def _cut(ink, box, axis, depth=0, major=False):
    """Split the sub-array ink[box] along `axis` (0 rows, 1 cols) at its blank gutters."""
    y0, y1, x0, x1 = box
    sub = ink[y0:y1, x0:x1]
    prof = sub.sum(axis=1 - axis)
    perp = sub.shape[1 - axis]
    prof = (prof > BLANK_TOL * perp).astype(np.uint8)
    length = len(prof)
    if major:
        min_gap = max(MAJOR_GAP_PX, int(length * MAJOR_GAP_FRAC))
    else:
        min_gap = max(MIN_GAP_PX, int(length * MIN_GAP_FRAC))
    runs = [r for r in _blank_runs(prof, min_gap) if r[0] > 0 and r[1] < length]   # interior gaps only
    if not runs:
        return None
    pieces, prev = [], 0
    for a, b in runs:
        pieces.append((prev, a))
        prev = b
    pieces.append((prev, length))
    out = []
    for a, b in pieces:
        if axis == 0:
            out.append((y0 + a, y0 + b, x0, x1))
        else:
            out.append((y0, y1, x0 + a, x0 + b))
    return out


def _trim(ink, box):
    y0, y1, x0, x1 = box
    sub = ink[y0:y1, x0:x1]
    rows = np.where(sub.sum(axis=1) > 0)[0]
    cols = np.where(sub.sum(axis=0) > 0)[0]
    if len(rows) == 0 or len(cols) == 0:
        return None
    return (y0 + rows[0], y0 + rows[-1] + 1, x0 + cols[0], x0 + cols[-1] + 1)


def _area(b):
    return (b[1] - b[0]) * (b[3] - b[2])


def _keep(ink, boxes):
    """Trim every piece to its ink; pieces big enough to be a figure of their own are kept, and the thin ones
    next to them (tick labels, axis titles, a panel letter) are folded back into the panel they belong to.
    A strip that would make two panels overlap, such as a legend shared by a whole row, is dropped."""
    big, small = [], []
    for b in boxes:
        t = _trim(ink, b)
        if t is None:
            continue
        (big if (t[1] - t[0]) >= MIN_SIDE and (t[3] - t[2]) >= MIN_SIDE else small).append(t)
    if not big or not small:
        return big

    def dist(p, q):          # gap between two boxes (0 when they touch or overlap)
        dy = max(0, max(p[0], q[0]) - min(p[1], q[1])); dx = max(0, max(p[2], q[2]) - min(p[3], q[3]))
        return max(dx, dy)

    def hits(u, others):
        return any(min(u[1], o[1]) - max(u[0], o[0]) > 4 and min(u[3], o[3]) - max(u[2], o[2]) > 4 for o in others)

    changed = True
    while changed and small:
        changed = False
        for sm in sorted(small, key=lambda q: min(dist(q, p) for p in big)):
            i = min(range(len(big)), key=lambda k: dist(sm, big[k]))
            if dist(sm, big[i]) > ABSORB_PX:
                continue
            p = big[i]
            u = (min(p[0], sm[0]), max(p[1], sm[1]), min(p[2], sm[2]), max(p[3], sm[3]))
            if hits(u, big[:i] + big[i + 1:]):
                continue
            big[i] = u; small.remove(sm); changed = True
            break
    return big


def _fine(ink, box):
    """The recursive XY-cut inside one block: every piece is cut along its columns, or failing that its rows,
    pass after pass until nothing splits any more. A shared legend above a row of charts blocks the column cut
    at first; once a row cut has taken the legend off, the next pass separates the charts. [] when the block
    shatters into more than MAX_PANELS * 3 pieces."""
    boxes = [box]
    for _ in range(MAX_DEPTH + 3):
        new, changed = [], False
        for b in boxes:
            parts = _cut(ink, b, 1) or _cut(ink, b, 0)
            if parts and len(parts) > 1:
                new.extend(parts); changed = True
            else:
                new.append(b)
        boxes = new
        if len(boxes) > MAX_PANELS * 3:
            return []
        if not changed:
            break
    return boxes


def split_panels(png: Path):
    """Return a list of (x0, y0, x1, y1) panel boxes in pixel coords, or [].

    Two stages. First the figure is cut at its wide gutters only: that separates things of different kinds that
    were drawn side by side (a method diagram next to a chart, a photo next to two grids). Then each block is
    tried with the fine cut, which is accepted only when it yields a regular set of similar pieces, i.e. a row or
    grid of subplots; a diagram, whose pieces come out ragged, stays whole."""
    im = Image.open(png).convert("L")
    ink = (np.asarray(im) < INK).astype(np.uint8)
    H, W = ink.shape
    blocks = [(0, H, 0, W)]
    for axis in (1, 0, 1):
        new = []
        for b in blocks:
            parts = _cut(ink, b, axis, major=True)
            new.extend(parts if parts and len(parts) > 1 else [b])
        blocks = new
        if len(blocks) > MAX_PANELS * 6:      # shattered beyond use (thin label strips are dropped below, so allow many)
            return []
    blocks = _keep(ink, blocks)
    if len(blocks) > MAX_PANELS:
        return []
    panels = []
    for blk in blocks:
        sub = _keep(ink, _fine(ink, blk))
        areas = [_area(x) for x in sub]
        # a handful of similar pieces is a row or grid of subplots; a larger grid of small multiples reads as one figure
        regular = (2 <= len(sub) <= MAX_SUBPLOTS and max(areas) <= 3.5 * min(areas) and sum(areas) >= 0.45 * _area(blk))
        panels.extend(sub if regular else [blk])
    if len(panels) < 2 or len(panels) > MAX_PANELS:
        return []
    big = max(_area(x) for x in panels)
    panels = [x for x in panels if _area(x) < 0.92 * W * H and _area(x) >= 0.05 * big]
    if len(panels) < 2:
        return []
    return [(max(0, x0 - MARGIN), max(0, y0 - MARGIN), min(W, x1 + MARGIN), min(H, y1 + MARGIN)) for (y0, y1, x0, x1) in panels]


def write_panels(png: Path, figs_dir: Path, fig_id: str, thumb_px=960):
    from figlib.extract import FIG_EXT, WEBP_QUALITY
    boxes = split_panels(png)
    if not boxes:
        return []
    im = Image.open(png).convert("RGB")
    out = []
    for k, (x0, y0, x1, y1) in enumerate(boxes, 1):
        pid = f"{fig_id}_s{k}"
        crop = im.crop((x0, y0, x1, y1))
        crop.save(figs_dir / f"{pid}{FIG_EXT}", quality=WEBP_QUALITY, method=4)
        th = crop.copy()
        th.thumbnail((thumb_px, thumb_px * 3))
        th.save(figs_dir / f"{pid}.thumb{FIG_EXT}", quality=WEBP_QUALITY, method=4)
        out.append({"fig_id": pid, "panel": k, "n_panels": len(boxes), "box_px": [x0, y0, x1, y1],
                    "px_w": crop.width, "px_h": crop.height})
    return out


def run(data: Path, log=print, force=False):
    """Add panel entries to every data figure that splits (labelled by the model, or sorted as a chart by its caption)."""
    from figlib.classify import atomic_write_json
    idx_dir, figs_dir = data / "index", data / "figs"
    n_fig = n_split = n_panels = 0
    for jp in sorted(idx_dir.glob("*.json")):
        rec = json.loads(jp.read_text())
        if "error" in rec:
            continue
        changed = False
        from figlib.extract import fig_path
        old_panels = {x["fig_id"]: x for x in rec.get("figures", []) if x.get("parent")}
        for f in rec.get("figures", []):
            if f.get("parent"):
                continue
            from figlib.classify import effective
            labelled = f.get("vision") or f.get("vision2")
            if labelled and effective(f)[0] != "data":
                continue
            if not labelled and f.get("kind") != "result":      # caption-sorted charts count as data
                continue
            if "panels" in f and not force:
                continue
            png = fig_path(figs_dir, f["fig_id"])
            if not png.exists():
                continue
            n_fig += 1
            prev = f.get("panels") or []
            panels = write_panels(png, figs_dir, f["fig_id"])
            f["panels"] = [p["fig_id"] for p in panels]
            changed = True
            if panels:
                n_split += 1
                n_panels += len(panels)
            # re-split with the same panel count keeps the old labels by position;
            # a different count drops the stale panel records (they get relabelled)
            if prev and len(prev) != len(panels):
                for pid in prev:
                    old_panels.pop(pid, None)
        if changed:
            rec["figures"] = [x for x in rec["figures"] if not x.get("parent") or x["fig_id"] in old_panels]
        if changed:
            # append panel records as figures of their own (labelled later by classify)
            have = {x["fig_id"] for x in rec["figures"]}
            for f in list(rec["figures"]):
                for pid in f.get("panels") or []:
                    if pid in have:
                        continue
                    k = int(pid.rsplit("_s", 1)[1])
                    rec["figures"].append({
                        "fig_id": pid, "parent": f["fig_id"], "panel": k, "n_panels": len(f["panels"]),
                        "num": f["num"], "page": f["page"],
                        "caption": f["caption"] + f" [{k}/{len(f['panels'])}]",
                        "bbox": f["bbox"], "full_width": f["full_width"], "has_raster": f["has_raster"],
                        "n_drawings": f["n_drawings"], "width_pt": f["width_pt"], "height_pt": f["height_pt"],
                        "kind": f["kind"], "extra": {"rotated": 0, **_png_size(fig_path(figs_dir, pid))},
                    })
            for x in rec["figures"]:
                if x.get("parent"):
                    x["extra"] = {"rotated": 0, **_png_size(fig_path(figs_dir, x["fig_id"]))}
            atomic_write_json(jp, rec)
    log(f"split: {n_fig} data figures examined, {n_split} split into {n_panels} panels")


def _png_size(p: Path):
    try:
        with Image.open(p) as im:
            return {"px_w": im.width, "px_h": im.height}
    except Exception:
        return {"px_w": 0, "px_h": 0}
