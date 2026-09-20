"""Tiny read-only web server for the figure library.

Serves the built gallery page and the rendered PNGs from the data directory.
Rebuilding the library is a CLI job (`figlib extract / classify / build`), the
server only reads what those wrote.
"""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

DATA = Path(os.environ.get("FIGLIB_DATA", "data"))
app = FastAPI(title="paper-fig-library", docs_url=None, redoc_url=None)


@app.get("/api/health")
def health():
    ok = (DATA / "gallery.html").exists()
    return JSONResponse({"ok": ok, "data": str(DATA)}, status_code=200 if ok else 503)


@app.get("/api/index.json")
def index():
    p = DATA / "index.json"
    if not p.exists():
        return JSONResponse({"error": "index.json not built yet"}, status_code=404)
    return FileResponse(p, media_type="application/json")


@app.get("/")
def root():
    p = DATA / "gallery.html"
    if not p.exists():
        return PlainTextResponse("gallery not built yet: run `figlib build`", status_code=503)
    return FileResponse(p, media_type="text/html", headers={"Cache-Control": "no-cache"})


ASSETS = Path(__file__).resolve().parent / "figlib" / "assets"


@app.get("/{name}")
def asset(name: str):
    p = ASSETS / name
    if name in {a.name for a in ASSETS.iterdir()} and p.is_file():
        return FileResponse(p)
    return JSONResponse({"detail": "Not Found"}, status_code=404)


if (DATA / "figs").is_dir():
    app.mount("/figs", StaticFiles(directory=DATA / "figs"), name="figs")
