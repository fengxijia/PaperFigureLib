# paperfigure

**Live: <https://paperfigure.net/>**

Asked AI to turn your paper into a figure, only to get a complete mess? Want to learn from the figure styles of top-tier conference papers, but are not sure where to find enough good examples? The ultimate collection of figures from top CS conferences is here!

paperfigure collects and organizes high-quality figures from top computer science conference papers, with categories including method diagrams, model architectures, workflows, data visualizations, and experimental results. Whether you are looking for inspiration, designing a layout, or choosing reference figures for AI generation, you can quickly find examples that meet the visual standards of top-tier conference papers.

把论文丢给AI画图，结果画得一团糟？想要参考顶会论文的画图风格，却担心找不全？顶会论文图表大全来啦！paperfigure 收集并整理了计算机科学顶会论文中的优秀图表，按照方法图、模型架构图、流程图、数据图、实验结果图等类别进行归档。无论你是在寻找灵感、设计版式，还是为 AI 绘图挑选参考模板，都可以从这里快速找到真正经得起顶会审美检验的范例。

## How it works

1. `figlib fetch` downloads the papers in `seeds/*.txt` (best, outstanding and high-impact papers of the last five years, by CSRankings area).
2. `figlib extract` crops every captioned figure with PyMuPDF at 300 dpi.
3. `figlib classify` labels each figure with a vision model (type, chart subtype, style tags); `figlib verify` double-checks with a stronger model; `figlib split` cuts multi-panel charts into panels.
4. `figlib build` produces the gallery; `figlib site` writes a static site for hosting.

## Run it yourself

```bash
git clone https://github.com/fengxijia/paperfigure && cd paperfigure
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

python -m figlib.cli extract --pdf-dir ~/papers --jobs 3     # your own PDFs, no API key needed
python -m figlib.cli build
python -m uvicorn server:app --port 8131                     # http://127.0.0.1:8131/

python -m figlib.cli fetch --seeds seeds/best-papers-ml.txt  # add a curated seed list
export OPENAI_API_KEY=sk-...                                  # or any OpenAI-compatible endpoint via OPENAI_BASE_URL
python -m figlib.cli classify --jobs 3 && python -m figlib.cli split && python -m figlib.cli classify --jobs 3
python -m figlib.cli build
```

Data lives in `./data` (override with `FIGLIB_DATA`). Seed lines are an arXiv id or a PDF URL followed by a comment with the venue and award, e.g. `2005.14165   # NeurIPS 2020 · Language Models are Few-Shot Learners`.

## Deploy the site

```bash
cp site.env.example site.env            # domain, Cloudflare account id and API token
python deploy/cf-setup.py site.env      # once: R2 bucket, Pages project, domains, DNS
bash deploy/publish-site.sh cloudflare  # figures to R2, page to Pages
```

## License

Code: MIT. Figures belong to the authors of the papers; every figure links to its source.
