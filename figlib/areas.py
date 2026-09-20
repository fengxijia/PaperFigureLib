"""Venue -> research area.

Areas follow the CSRankings grouping, but the venue list inside each area is the
commonly accepted "top venues" list people actually quote (the security big four,
the vision big three, the ML big three, ...). `top` is that canonical list; `also`
holds venues we still map into the area for classification (journals, second-tier
or regional conferences) without presenting them as the area's headline venues.
"""

import re

# (area id, zh label, en label, group, top venues, also-mapped venues)
AREAS = [
    ("ml", "机器学习", "Machine learning", "AI", ["NeurIPS", "ICML", "ICLR"], ["NIPS", "KDD", "AISTATS", "UAI", "JMLR", "TMLR", "COLM", "MLSys"]),
    ("ai", "人工智能", "Artificial intelligence", "AI", ["AAAI", "IJCAI"], []),
    ("vision", "计算机视觉", "Computer vision", "AI", ["CVPR", "ICCV", "ECCV"], ["WACV", "TPAMI", "IJCV"]),
    ("nlp", "自然语言处理", "Natural language processing", "AI", ["ACL", "EMNLP", "NAACL", "COLING", "EACL"], ["Findings", "TACL", "IJCNLP", "SIGDIAL"]),
    ("ir", "Web 与信息检索", "Web & information retrieval", "AI", ["SIGIR", "WWW"], []),
    ("security", "计算机安全", "Computer security", "Systems", ["IEEE S&P", "CCS", "USENIX Security", "NDSS"], ["S&P", "AISec"]),
    ("os", "操作系统", "Operating systems", "Systems", ["OSDI", "SOSP", "EuroSys", "USENIX ATC", "FAST"], ["ATC"]),
    ("arch", "计算机体系结构", "Computer architecture", "Systems", ["ISCA", "MICRO", "ASPLOS", "HPCA"], []),
    ("networks", "计算机网络", "Computer networks", "Systems", ["SIGCOMM", "NSDI"], []),
    ("db", "数据库", "Databases", "Systems", ["SIGMOD", "VLDB", "ICDE"], ["PODS"]),
    ("pl", "编程语言", "Programming languages", "Systems", ["PLDI", "POPL", "OOPSLA", "ICFP"], []),
    ("se", "软件工程", "Software engineering", "Systems", ["ICSE", "FSE", "ASE", "ISSTA"], []),
    ("hpc", "高性能计算", "High-performance computing", "Systems", ["SC", "HPDC", "ICS"], []),
    ("mobile", "移动计算", "Mobile computing", "Systems", ["MobiCom", "MobiSys", "SenSys"], []),
    ("metrics", "测量与性能分析", "Measurement & performance", "Systems", ["SIGMETRICS", "IMC"], []),
    ("eda", "设计自动化", "Design automation", "Systems", ["DAC", "ICCAD"], []),
    ("embedded", "嵌入式与实时系统", "Embedded & real-time systems", "Systems", ["RTSS", "RTAS", "EMSOFT"], []),
    ("theory", "算法与复杂性", "Algorithms & complexity", "Theory", ["STOC", "FOCS", "SODA"], []),
    ("crypto", "密码学", "Cryptography", "Theory", ["CRYPTO", "EUROCRYPT"], []),
    ("logic", "逻辑与验证", "Logic & verification", "Theory", ["CAV", "LICS"], []),
    ("hci", "人机交互", "Human-computer interaction", "Interdisciplinary", ["CHI", "UIST", "IMWUT"], ["UbiComp", "SIGCHI", "DIS", "PACMHCI"]),
    ("graphics", "计算机图形学", "Computer graphics", "Interdisciplinary", ["SIGGRAPH", "SIGGRAPH Asia", "Eurographics"], ["TOG"]),
    ("vis", "可视化与虚拟现实", "Visualization & VR", "Interdisciplinary", ["VIS", "IEEE VR"], ["VR", "TVCG", "ISMAR"]),
    ("robotics", "机器人", "Robotics", "Interdisciplinary", ["ICRA", "IROS", "RSS"], ["CoRL"]),
    ("bio", "计算生物学", "Computational biology", "Interdisciplinary", ["ISMB", "RECOMB"], []),
    ("econ", "经济与计算", "Economics & computation", "Interdisciplinary", ["EC", "WINE"], []),
    ("speech", "语音与信号处理", "Speech & signal processing", "Beyond CSRankings", ["ICASSP", "INTERSPEECH"], ["ICIP", "TASLP"]),
    ("preprint", "预印本 / 期刊", "Preprints & journals", "Beyond CSRankings", ["arXiv"], ["Science", "Nature"]),
]

_LOOKUP = {}
for aid, zh, en, group, top, also in AREAS:
    for v in top + also:
        _LOOKUP[v.lower()] = aid


def area_of(venue: str) -> str:
    """Area id for a venue string as found in seed notes ("IEEE S&P 2024 ...") or PDF text."""
    if not venue:
        return ""
    v = venue.strip().lower()
    if v in _LOOKUP:
        return _LOOKUP[v]
    # "SIGGRAPH Asia" before "SIGGRAPH", "USENIX Security" before "USENIX", etc.: longest key first
    for key in sorted(_LOOKUP, key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(key) + r"(?![a-z])", v):
            return _LOOKUP[key]
    return ""


def areas_table():
    return [{"id": aid, "zh": zh, "en": en, "group": group, "top": top} for aid, zh, en, group, top, _ in AREAS]
