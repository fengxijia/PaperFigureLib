# PaperFigure

A style-reference gallery of every figure in top-venue CS papers.

**Live site: <https://paperfigure.net/>** (1,700+ best / outstanding / high-impact papers from 2021 to 2026, 16,000+ figures).

PaperFigure pulls every figure out of a set of papers, labels each one with a vision model (method diagram, background figure, data chart by subtype: bar, line, scatter, pie, heatmap, box, violin, radar, histogram, area, confusion matrix; example, prompt, table), cuts multi-panel charts into single panels, and renders the result as a fast, searchable static gallery. Use it to find how the best papers in your area draw a given kind of figure before you draw your own.

![PaperFigure gallery](https://paperfigure.net/apple-touch-icon.png)

## What it does

| Step | Command | What happens |
|---|---|---|
| Fetch | `figlib fetch --seeds seeds/best-papers-ml.txt` | Downloads PDFs listed in a seed file (arXiv ids or direct PDF links, one per line, venue and award in the comment). |
| Extract | `figlib extract` | Finds every "Figure N" caption with PyMuPDF, collects the images and vector drawings above it in the same column, renders the crop at 300 dpi (WebP) plus a 960 px thumbnail. Sideways figures are rotated upright. |
| Classify | `figlib classify` | Sends thumbnail and caption to a vision model (`gpt-4.1-mini` by default) and stores category, chart type, style tags and a one-line summary. About 550 input tokens per figure. |
| Verify | `figlib verify` | Second opinion from a stronger model (`gpt-5-mini`) on the confusable categories (method vs. result). |
| Split | `figlib split` | Recursive XY-cut of multi-panel data charts along blank gutters; panels are labelled again and shown instead of the composite. |
| Enrich | `figlib enrich` | Authors, titles and citation counts from Semantic Scholar. |
| Build | `figlib build` | Merges everything into `index.json` and `gallery.html` (single file, works offline). |
| Site | `figlib site --img-base https://img.example.org/` | Writes a static site: `index.html`, sharded JSON, figures. Deploy anywhere static (see below). |

`figlib all` runs the whole chain. Every step is cached: re-running only touches new papers or figures.

The gallery itself is one HTML file with no framework: filter by figure type and chart subtype, venue (grouped by CSRankings area), award tier and drawing style; full-text search over captions, titles, tags and arXiv ids; a justified "wall" layout that fills the page and sizes cards by the figure's physical size; hover ribbons with venue, award, title and authors; a lightbox with zoom and deep links; Chinese and English UI.

## Quick start

```bash
git clone https://github.com/fengxijia/paperfigure && cd paperfigure
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

# 1. figures from your own PDFs, no API key needed
python -m figlib.cli extract --pdf-dir ~/papers --jobs 3
python -m figlib.cli build
python -m uvicorn server:app --port 8131      # open http://127.0.0.1:8131/

# 2. add the curated seed lists (best papers by area, 2021 to 2026)
python -m figlib.cli fetch --seeds seeds/best-papers-ml.txt --delay 2
python -m figlib.cli extract --jobs 3

# 3. label with a vision model (OPENAI_API_KEY, or any OpenAI-compatible endpoint via OPENAI_BASE_URL)
export OPENAI_API_KEY=sk-...
python -m figlib.cli classify --limit 40      # probe first
python -m figlib.cli classify --jobs 3
python -m figlib.cli split && python -m figlib.cli classify --jobs 3
python -m figlib.cli verify
python -m figlib.cli build
```

Data lives in `./data` (override with `FIGLIB_DATA`): `pdf/` (PDF plus a sidecar json per paper), `index/<paper>.json` (extraction and labels), `figs/*.webp`, `index.json`, `gallery.html`. `figlib prune-pdfs` deletes PDFs whose figures are already extracted; `figlib fetch --refetch` brings them back.

## Seed lists

`seeds/*.txt` hold about 2,300 papers: the best, outstanding and distinguished papers of the last five years at the top venues of each CSRankings area (AI, ML, NLP, CV and graphics, HCI and robotics, systems, networking and security, databases, web, PL and SE, theory, speech), plus high-impact classics. One line per paper:

```
2005.14165   # NeurIPS 2020 · Language Models are Few-Shot Learners
https://example.org/paper.pdf   # CHI 2024 Best Paper · Title
```

The venue and award in the comment feed the gallery's venue filter and ordering. Add a line, run `fetch`, `extract`, `classify`, `build`.

## Deploying the public site

`figlib site` writes a static directory. Cloudflare Pages plus R2 serves it worldwide with no server to protect:

```bash
cp site.env.example site.env && $EDITOR site.env    # domain, account id, API token
python deploy/cf-setup.py site.env                  # one time: R2 bucket, Pages project, custom domains, DNS
bash deploy/publish-site.sh cloudflare              # figures to R2, page to Pages
```

`bash deploy/publish-site.sh s3` pushes everything into any S3-compatible bucket instead.

## Layout of the code

```
figlib/extract.py    caption anchoring and cropping (the core)
figlib/classify.py   vision labelling and verification
figlib/split.py      panel splitting (recursive XY-cut)
figlib/enrich.py     Semantic Scholar metadata
figlib/areas.py      venues by CSRankings area
figlib/cli.py        fetch / extract / classify / verify / split / enrich / build / site / ...
figlib/gallery.html  the gallery page (index inlined by build, fetched as shards by site)
server.py            read-only FastAPI server for local use
seeds/               curated paper lists
deploy/              Cloudflare setup and publishing
```

## Known limits

- Captions must start with "Figure N" / "Fig. N" and sit below the figure; the few venues that put captions above figures are missed.
- A table close above a figure can leak into the crop.
- Category labels come from a model looking at a thumbnail; "background" and "method" are sometimes confused. `verify` reduces this.

## License

Code: MIT. The figures belong to the authors of the papers; the gallery links every figure to its source.

## 中文说明

PaperFigure 把顶会论文里的每一张图裁出来，用视觉模型按「方法图 / 背景图 / 数据图（柱状、曲线、散点、饼图、热力图……）/ 样例图 / 提示词 / 表格」分类，切开多面板图表，做成一个能搜、能筛、铺满整页的静态图库。画自己论文的图之前，先看看同领域最佳论文是怎么画同一类图的。在线版：<https://paperfigure.net/>。上面的 Quick start 三步（抽图、加种子清单、视觉模型打标）在本地就能跑；`figlib site` 生成的静态目录可以部署到 Cloudflare Pages 加 R2，或任何静态托管。
