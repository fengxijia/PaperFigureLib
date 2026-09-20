"""Classify extracted figures with a vision model (OpenAI-compatible relay).

Each figure gets a `vision` dict written back into index/<paper_key>.json:
    category    background | method | data | example | prompt | table | other
    chart_type  for data figures: bar | line | scatter | pie | heatmap | box |
                violin | radar | histogram | area | confusion_matrix | mixed | other
    tags        short style descriptors ("error bars", "log scale", "2x3 panels", ...)
    summary_zh  one sentence in Chinese describing what the figure shows and how

Results are cached: a figure that already has `vision` is skipped unless --force.
Concurrency is capped at 3 (shared relay key rule). Labels are written back per
paper as soon as that paper's figures are done (atomic replace), keyed by fig_id,
so an interrupted run keeps everything finished so far.
"""

from __future__ import annotations

import base64
import concurrent.futures as cf
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import httpx
from PIL import Image

CATEGORIES = ["background", "method", "data", "example", "prompt", "table", "other"]
CHART_TYPES = ["bar", "line", "scatter", "pie", "heatmap", "box", "violin", "radar",
               "histogram", "area", "confusion_matrix", "mixed", "other"]

SYSTEM = """You label figures cut out of machine-learning / HCI / graphics research papers.
Return ONLY a JSON object with these keys:
- category: one of background | method | data | example | prompt | table | other
    background: motivates the problem or explains a concept (teaser, problem illustration, conceptual comparison of paradigms)
    method: architecture, pipeline, framework, algorithm flow, system diagram
    data: a quantitative chart (bars, curves, points, pies, heatmaps, distributions)
    example: qualitative samples: generated images, screenshots, case studies, rendered scenes
    prompt: a text box showing a prompt or template
    table: a table rendered as a figure
- chart_type: only when category is data; one of bar | line | scatter | pie | heatmap | box | violin | radar | histogram | area | confusion_matrix | mixed | other. Otherwise "".
- tags: 2 to 6 short lowercase style descriptors, e.g. "error bars", "log scale", "2x3 panels", "annotated arrows", "icons", "color-coded modules", "legend inside", "dual y-axis", "hand-drawn style".
- summary_zh: one Chinese sentence (<= 40 chars) saying what the figure shows and how it is drawn.
Use the caption for context but judge the category from the picture."""


def _thumb_b64(path: Path, max_side=640):
    im = Image.open(path).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


class Classifier:
    def __init__(self, model, base_url=None, api_key=None, timeout=90):
        self.model = model
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise SystemExit("no API key: set OPENAI_API_KEY")
        # trust_env=False: never pick up the machine proxy (requests hang otherwise)
        self.client = httpx.Client(timeout=timeout, trust_env=False)
        self.lock = threading.Lock()
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0, "errors": 0}

    def label(self, png: Path, caption: str):
        b64 = _thumb_b64(png)
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 300,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": f"Caption: {caption[:600]}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"}},
                ]},
            ],
        }
        last = None
        for attempt in range(3):
            try:
                r = self.client.post(f"{self.base_url}/chat/completions", json=body,
                                     headers={"Authorization": f"Bearer {self.api_key}"})
                if r.status_code == 429 or r.status_code >= 500:
                    last = f"HTTP {r.status_code}: {r.text[:200]}"
                    time.sleep(2 + 3 * attempt)
                    continue
                r.raise_for_status()
                data = r.json()
                txt = data["choices"][0]["message"]["content"]
                u = data.get("usage", {})
                with self.lock:
                    self.usage["calls"] += 1
                    self.usage["prompt_tokens"] += u.get("prompt_tokens", 0)
                    self.usage["completion_tokens"] += u.get("completion_tokens", 0)
                return _parse(txt)
            except httpx.HTTPStatusError as e:
                # a 400 is a parameter problem, not transient: surface it
                raise RuntimeError(f"{e.response.status_code}: {e.response.text[:300]}")
            except Exception as e:      # network / json
                last = repr(e)
                time.sleep(2 + 3 * attempt)
        raise RuntimeError(last or "unknown")     # caller counts the error


def _parse(txt):
    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        if txt.startswith("json"):
            txt = txt[4:]
    obj = json.loads(txt)
    cat = str(obj.get("category", "other")).lower().strip()
    if cat not in CATEGORIES:
        cat = "other"
    ct = str(obj.get("chart_type", "") or "").lower().strip()
    if cat != "data":
        ct = ""
    elif ct not in CHART_TYPES:
        ct = "other"
    tags = obj.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    tags = [str(t).lower().strip() for t in tags if str(t).strip()][:6]
    return {"category": cat, "chart_type": ct, "tags": tags,
            "summary_zh": str(obj.get("summary_zh", "")).strip()[:80]}


def atomic_write_json(path: Path, obj):
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _write_back(jp: Path, labels: dict):
    """Merge {fig_id: vision} into the paper record, by fig_id (never by index)."""
    rec = json.loads(jp.read_text())
    by_id = {f["fig_id"]: f for f in rec.get("figures", [])}
    n = 0
    for fid, v in labels.items():
        if fid in by_id:
            by_id[fid]["vision"] = v
            n += 1
    if n:
        atomic_write_json(jp, rec)
    return n


VERIFY_SYSTEM = """You double-check the category of a figure cut out of a research paper. A cheaper model already
guessed it; you see a larger image and decide. Return ONLY JSON {"category": ..., "chart_type": ..., "why": "<10 words"}.
category: background (motivation / concept illustration, teaser), method (architecture, pipeline, framework, system diagram),
data (any quantitative chart: axes, bars, curves, points, heatmaps, distributions, even when the caption talks about our method),
example (qualitative samples, screenshots, rendered scenes, image grids), prompt (text box), table, other.
chart_type only when category is data: bar | line | scatter | pie | heatmap | box | violin | radar | histogram | area | confusion_matrix | mixed | other, else ""."""


def effective(f):
    """Category / chart_type the page should show: second opinion > first pass > caption heuristics."""
    v2, v = f.get("vision2"), f.get("vision")
    if v2:
        return v2["category"], v2.get("chart_type", "")
    if v:
        return v["category"], v.get("chart_type", "")
    return {"teaser": "background", "method": "method", "result": "data", "example": "example",
            "prompt": "prompt", "other": "other"}.get(f.get("kind"), "other"), ""


def verify(data: Path, model: str, cats, jobs=3, limit=None, include_unlabeled=True, log=print):
    """Second opinion from a stronger model for figures whose effective category is in `cats`."""
    from figlib.extract import fig_path
    idx_dir, figs_dir = data / "index", data / "figs"
    clf = Classifier(model)
    jobs = max(1, min(jobs, 3))
    todo = []
    for jp in sorted(idx_dir.glob("*.json")):
        rec = json.loads(jp.read_text())
        if "error" in rec:
            continue
        for f in rec.get("figures", []):
            if f.get("vision2") or f.get("panels"):
                continue
            if not f.get("vision") and not include_unlabeled:
                continue
            if effective(f)[0] not in cats:
                continue
            png = fig_path(figs_dir, f["fig_id"], thumb=True)
            if png.exists():
                todo.append((jp, f["fig_id"], png, f["caption"]))
    if limit:
        todo = todo[:limit]
    log(f"verify: {len(todo)} figures with {model}, {jobs} workers")
    if not todo:
        return clf.usage

    def one(png, cap):
        b64 = _thumb_b64(png, max_side=1024)
        body = {"model": model,
                "messages": [{"role": "system", "content": VERIFY_SYSTEM},
                             {"role": "user", "content": [{"type": "text", "text": f"Caption: {cap[:600]}"},
                                                          {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]}
        if not model.startswith("gpt-5"):
            body["temperature"] = 0
            body["response_format"] = {"type": "json_object"}
        r = clf.client.post(f"{clf.base_url}/chat/completions", json=body, headers={"Authorization": f"Bearer {clf.api_key}"})
        if r.status_code in (400, 401, 403):
            raise SystemExit(f"{r.status_code} from relay: {r.text[:200]}")
        r.raise_for_status()
        d = r.json()
        u = d.get("usage", {})
        with clf.lock:
            clf.usage["calls"] += 1
            clf.usage["prompt_tokens"] += u.get("prompt_tokens", 0)
            clf.usage["completion_tokens"] += u.get("completion_tokens", 0)
        out = _parse(d["choices"][0]["message"]["content"])
        return {"model": model, "category": out["category"], "chart_type": out["chart_type"]}

    # canary
    jp0, fid0, png0, cap0 = todo[0]
    _write_back_key(jp0, {fid0: one(png0, cap0)}, "vision2")
    todo = todo[1:]
    pending, buffered, lock = {}, {}, threading.Lock()
    for jp, *_ in todo:
        pending[jp] = pending.get(jp, 0) + 1

    def work(item):
        jp, fid, png, cap = item
        try:
            return jp, fid, one(png, cap)
        except SystemExit:
            raise
        except Exception as e:
            with clf.lock:
                clf.usage["errors"] += 1
            return jp, fid, {"error": str(e)[:200]}

    done = 0
    try:
        with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
            for jp, fid, v in ex.map(work, todo):
                done += 1
                with lock:
                    if "error" not in v:
                        buffered.setdefault(jp, {})[fid] = v
                    pending[jp] -= 1
                    if pending[jp] == 0 and buffered.get(jp):
                        _write_back_key(jp, buffered.pop(jp), "vision2")
                if done % 25 == 0 or done == len(todo):
                    log(f"  {done + 1}/{len(todo) + 1}  tokens in/out {clf.usage['prompt_tokens']}/{clf.usage['completion_tokens']}  errors {clf.usage['errors']}")
    finally:
        for jp, labels in buffered.items():
            if labels:
                _write_back_key(jp, labels, "vision2")
    return clf.usage


def _write_back_key(jp: Path, labels: dict, key: str):
    rec = json.loads(jp.read_text())
    by_id = {f["fig_id"]: f for f in rec.get("figures", [])}
    n = 0
    for fid, v in labels.items():
        if fid in by_id:
            by_id[fid][key] = v
            n += 1
    if n:
        atomic_write_json(jp, rec)
    return n


def run(data: Path, model: str, jobs=3, limit=None, force=False, log=print):
    idx_dir = data / "index"
    figs_dir = data / "figs"
    clf = Classifier(model)
    jobs = max(1, min(jobs, 3))         # shared relay key: never more than 3 in flight
    todo = []
    for jp in sorted(idx_dir.glob("*.json")):
        rec = json.loads(jp.read_text())
        if "error" in rec:
            continue
        for f in rec.get("figures", []):
            if f.get("vision") and not force:
                continue
            if f.get("panels"):
                continue                # composite already split: only its panels get labels
            from figlib.extract import fig_path
            png = fig_path(figs_dir, f["fig_id"], thumb=True)
            if png.exists():
                todo.append((jp, f["fig_id"], png, f["caption"]))
    if limit:
        todo = todo[:limit]
    log(f"classify: {len(todo)} figures with {model}, {jobs} workers")
    if not todo:
        return clf.usage

    # canary: one synchronous call so a bad parameter / dead key fails before the pool starts
    jp0, fid0, png0, cap0 = todo[0]
    v0 = clf.label(png0, cap0)
    v0["model"] = model
    _write_back(jp0, {fid0: v0})
    todo = todo[1:]

    pending = {}                        # jp -> remaining count
    for jp, *_ in todo:
        pending[jp] = pending.get(jp, 0) + 1
    buffered = {}                       # jp -> {fig_id: vision}
    lock = threading.Lock()

    def work(item):
        jp, fid, png, cap = item
        try:
            v = clf.label(png, cap)
            v["model"] = model
        except Exception as e:
            with clf.lock:
                clf.usage["errors"] += 1
            v = {"error": str(e)[:200]}
        return jp, fid, v

    done = 0
    try:
        with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
            for jp, fid, v in ex.map(work, todo):
                done += 1
                with lock:
                    if "error" not in v:
                        buffered.setdefault(jp, {})[fid] = v
                    pending[jp] -= 1
                    if pending[jp] == 0 and buffered.get(jp):
                        _write_back(jp, buffered.pop(jp))
                if done % 25 == 0 or done == len(todo):
                    log(f"  {done + 1}/{len(todo) + 1}  tokens in/out {clf.usage['prompt_tokens']}/{clf.usage['completion_tokens']}  errors {clf.usage['errors']}")
                if "error" in v:
                    log(f"  ! {fid}: {v['error']}")
                    if v["error"].startswith(("400", "401", "403")):
                        raise SystemExit(f"{v['error'][:3]} from relay: fix key / parameters before continuing")
    finally:
        # flush partially finished papers (interrupt, SystemExit, crash)
        for jp, labels in buffered.items():
            if labels:
                _write_back(jp, labels)
    return clf.usage
