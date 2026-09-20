"""Split multi-panel data figures into single panels (recursive XY-cut).

A chart figure often holds several subplots side by side. The library wants
one entry per chart type, so a composite "scatter + line" figure is cut along
its blank gutters into panels, each of which is then labelled on its own.
Only figures the vision model called `data` are split; method diagrams and
example grids keep their whitespace and stay whole.

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


def _cut(ink, box, axis, depth):
    """Split the sub-array ink[box] along `axis` (0 rows, 1 cols)."""
    y0, y1, x0, x1 = box
    sub = ink[y0:y1, x0:x1]
    prof = sub.sum(axis=1 - axis)
    perp = sub.shape[1 - axis]
    prof = (prof > BLANK_TOL * perp).astype(np.uint8)
    length = len(prof)
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


def split_panels(png: Path):
    """Return a list of (x0, y0, x1, y1) panel boxes in pixel coords, or []."""
    im = Image.open(png).convert("L")
    arr = np.asarray(im)
    ink = (arr < INK).astype(np.uint8)
    H, W = ink.shape
    boxes = [(0, H, 0, W)]
    for depth in range(MAX_DEPTH):
        axis = depth % 2
        new, changed = [], False
        for b in boxes:
            parts = _cut(ink, b, axis, depth)
            if parts and len(parts) > 1:
                new.extend(parts)
                changed = True
            else:
                new.append(b)
        boxes = new
        if len(boxes) > MAX_PANELS:
            return []
        # try the other axis once more on the same level when nothing changed
        if not changed and depth == 0:
            new = []
            for b in boxes:
                parts = _cut(ink, b, 1, depth)
                new.extend(parts if parts and len(parts) > 1 else [b])
            boxes = new
    panels = []
    for b in boxes:
        t = _trim(ink, b)
        if t is None:
            continue
        y0, y1, x0, x1 = t
        if (y1 - y0) < MIN_SIDE or (x1 - x0) < MIN_SIDE:
            continue
        panels.append((max(0, x0 - MARGIN), max(0, y0 - MARGIN), min(W, x1 + MARGIN), min(H, y1 + MARGIN)))
    if len(panels) < 2:
        return []
    # a "panel" covering almost the whole figure means the cut only shaved a legend off;
    # pieces much smaller than the largest one are legends, colour bars or titles
    area = W * H
    sizes = [(p[2] - p[0]) * (p[3] - p[1]) for p in panels]
    big = max(sizes)
    panels = [p for p, a in zip(panels, sizes) if a < 0.9 * area and a >= 0.25 * big]
    return panels if len(panels) >= 2 else []


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
    """Add panel entries to every labelled data figure that splits."""
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
            if not (f.get("vision") or f.get("vision2")) or effective(f)[0] != "data":
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
                        "caption": f["caption"] + f"  [子图 {k}/{len(f['panels'])}]",
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
