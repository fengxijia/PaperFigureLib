"""Local chart-type classifier: CLIP image features + a softmax layer trained on the figures the vision
model already labelled. No API calls.

    figlib chartclf            # embed, train, predict, write `chart_pred` into the per-paper index files

Why: the vision model labelled a few thousand figures; the rest only have a category guessed from the
caption, so every chart among them lands in "other chart". The labelled figures are enough to teach a
linear layer on frozen CLIP features which chart is a bar / line / scatter / heatmap / ..., and which
"chart" is really a photo, a diagram or a table.

Model: Xenova/clip-vit-base-patch32 vision tower as ONNX (352 MB, downloaded once into data/models/).
Features are cached in data/clipfeat/<paper>.npz keyed by figure id + thumbnail version."""

import json
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

MODEL_URL = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model.onnx"
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
ALIAS = {"violin": "box", "mixed": "other"}
NON_CHART = {"method": "x:method", "background": "x:background", "example": "x:example", "table": "x:table", "prompt": "x:prompt"}
MIN_PER_CLASS = 12
VERSION = 2


def _model_path(data: Path, log=print):
    p = data / "models" / "clip-vit-b32-vision.onnx"
    if not p.exists() or p.stat().st_size < 300_000_000:
        p.parent.mkdir(parents=True, exist_ok=True)
        log(f"chartclf: downloading the CLIP vision model to {p}")
        tmp = p.with_suffix(".part")
        urllib.request.urlretrieve(MODEL_URL, tmp)
        tmp.rename(p)
    return p


def _prep(path: Path):
    """Pad to a square on white (charts are often very wide: a centre crop would cut them), resize to 224."""
    im = Image.open(path).convert("RGB")
    s = max(im.size)
    sq = Image.new("RGB", (s, s), (255, 255, 255))
    sq.paste(im, ((s - im.width) // 2, (s - im.height) // 2))
    a = np.asarray(sq.resize((224, 224), Image.BICUBIC), dtype=np.float32) / 255.0
    return ((a - MEAN) / STD).transpose(2, 0, 1)


def _embed(sess, paths, batch=32):
    out = []
    name = sess.get_inputs()[0].name
    for i in range(0, len(paths), batch):
        x = np.stack([_prep(p) for p in paths[i:i + batch]])
        res = sess.run(None, {name: x})
        emb = next(r for r in res if r.ndim == 2)              # image_embeds (the pooled, projected vector)
        out.append(emb / np.linalg.norm(emb, axis=1, keepdims=True))
    return np.concatenate(out) if out else np.zeros((0, 512), dtype=np.float32)


def _label_of(f, guess):
    """Training label of a figure, or None. Vision labels first; a chart class named in tags or caption counts
    as a (weaker) label so that rare classes such as contour get examples."""
    v = f.get("vision") or {}
    cat = v.get("category")
    if cat == "data":
        ct = ALIAS.get(v.get("chart_type"), v.get("chart_type") or "")
        if ct and ct != "other":
            return ct
        return ALIAS.get(guess, guess) or "other"
    if cat in NON_CHART:
        return NON_CHART[cat]
    if not v and guess:
        return ALIAS.get(guess, guess)
    return None


def _train(X, y, n_cls, epochs=300, lr=0.5, l2=1e-4, log=print):
    """Class-balanced softmax regression on L2-normalised features (plain numpy, full batch)."""
    n, d = X.shape
    W = np.zeros((d, n_cls), dtype=np.float32); b = np.zeros(n_cls, dtype=np.float32)
    counts = np.bincount(y, minlength=n_cls).astype(np.float32)
    w = (n / (n_cls * np.maximum(counts, 1)))[y]
    Y = np.eye(n_cls, dtype=np.float32)[y]
    Xs = X * 10.0                                               # features are unit length: scale up so the logits can separate
    for ep in range(epochs):
        z = Xs @ W + b
        z -= z.max(axis=1, keepdims=True)
        p = np.exp(z); p /= p.sum(axis=1, keepdims=True)
        g = (p - Y) * w[:, None] / n
        W -= lr * (Xs.T @ g + l2 * W); b -= lr * g.sum(axis=0)
    return W, b


def _predict(X, W, b):
    z = (X * 10.0) @ W + b
    z -= z.max(axis=1, keepdims=True)
    p = np.exp(z); p /= p.sum(axis=1, keepdims=True)
    return p


def run(data: Path, log=print, min_p=0.8, threads=6):
    import onnxruntime as ort
    from figlib.extract import fig_path
    from figlib.cli import _chart_from_text, load_overrides, apply_override
    overrides = load_overrides(data)
    idx_dir, figs_dir, feat_dir = data / "index", data / "figs", data / "clipfeat"
    feat_dir.mkdir(exist_ok=True)
    so = ort.SessionOptions(); so.intra_op_num_threads = threads
    sess = ort.InferenceSession(str(_model_path(data, log)), so, providers=["CPUExecutionProvider"])

    # 1. features for every figure that is labelled (training) or a chart candidate (prediction)
    recs, rows = {}, []          # rows: (paper, fig dict, label or None, wants prediction)
    n_new = 0
    for jp in sorted(idx_dir.glob("*.json")):
        try:
            rec = json.loads(jp.read_text())
        except Exception:
            continue
        if "error" in rec:
            continue
        recs[jp] = rec
        cache_p = feat_dir / f"{jp.stem}.npz"
        cache = dict(np.load(cache_p)) if cache_p.exists() else {}
        todo = []
        for f in rec["figures"]:
            if f.get("panels"):
                continue                                   # shown as its panels
            apply_override(f, overrides)
            v = f.get("vision") or {}
            guess = _chart_from_text(" ; ".join(v.get("tags") or []), f.get("caption", "")) if not f.get("parent") else ""
            label = _label_of(f, guess)
            is_chart_cand = ((v.get("category") == "data" and ALIAS.get(v.get("chart_type"), v.get("chart_type") or "other") == "other")
                             or (not v and f.get("kind") == "result") or (not v and bool(f.get("parent"))))
            if label is None and not is_chart_cand:
                continue
            tp = fig_path(figs_dir, f["fig_id"], thumb=True)
            if not tp.exists():
                continue
            key = f"{f['fig_id']}|{int(tp.stat().st_mtime)}"
            rows.append((jp, f, label, is_chart_cand, key, cache_p))
            if key not in cache:
                todo.append((key, tp))
        if todo:
            emb = _embed(sess, [t for _, t in todo])
            for (key, _), e in zip(todo, emb):
                cache[key] = e
            live = {r[4] for r in rows if r[5] == cache_p}
            np.savez(cache_p, **{k: v for k, v in cache.items() if k in live})
            n_new += len(todo)
            if n_new % 2000 < len(todo):
                log(f"  embedded {n_new} thumbnails")
    log(f"chartclf: {len(rows)} figures in play, {n_new} newly embedded")

    feats = {}
    for cp in {r[5] for r in rows}:
        if cp.exists():
            feats.update(dict(np.load(cp)))
    rows = [r for r in rows if r[4] in feats]

    # 2. train on the labelled ones; 5-fold cross-validation reports what the chosen threshold buys
    from collections import Counter
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_val_predict
    lab = [(r, r[2]) for r in rows if r[2] is not None]
    cnt = Counter(l for _, l in lab)
    lab = [(r, l) for r, l in lab if cnt[l] >= MIN_PER_CLASS]
    X = np.stack([feats[r[4]] for r, _ in lab]).astype(np.float32)
    y = np.array([l for _, l in lab])
    make = lambda: LogisticRegression(C=20.0, max_iter=3000, class_weight="balanced")
    Pcv = cross_val_predict(make(), X, y, cv=StratifiedKFold(5, shuffle=True, random_state=0), method="predict_proba")
    classes = list(np.unique(y))
    hit = np.array(classes)[Pcv.argmax(1)] == y
    conf = Pcv.max(1) >= min_p
    acc, acc_conf = float(hit.mean()), float(hit[conf].mean()) if conf.any() else 0.0
    log(f"chartclf: {len(y)} labelled figures, {len(classes)} classes {dict(cnt.most_common())}")
    log(f"chartclf: cross-validated accuracy {acc:.3f}; at p >= {min_p}: {acc_conf:.3f} on {conf.mean():.0%} of the labelled figures")
    model = make().fit(X, y)                                   # final model on everything
    _predict_final = lambda Z: model.predict_proba(Z)

    # 3. predict the chart candidates and write the result back
    cand = [r for r in rows if r[3]]
    P = _predict_final(np.stack([feats[r[4]] for r in cand]).astype(np.float32)) if cand else np.zeros((0, len(classes)))
    out = Counter()
    touched = set()
    for r, p in zip(cand, P):
        k = int(p.argmax()); pk = float(p[k])
        f = r[1]
        if pk >= min_p:
            f["chart_pred"] = {"label": classes[k], "p": round(pk, 3), "model": f"clip-b32-probe-v{VERSION}"}
            out[classes[k]] += 1
        else:
            f.pop("chart_pred", None)
            out["(unsure)"] += 1
        touched.add(r[0])
    from figlib.classify import atomic_write_json
    for jp in touched:
        atomic_write_json(jp, recs[jp])
    log(f"chartclf: predictions for {len(cand)} chart candidates: {dict(out.most_common())}")
    return {"accuracy": acc, "accuracy_confident": acc_conf, "predicted": dict(out)}
