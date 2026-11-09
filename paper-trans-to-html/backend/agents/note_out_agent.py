"""笔记导出 Agent：联网核验参考文献 + 提取代码仓库 + 形成分析报告。

这个模块只负责 **Agent 那一段**（`note_out.py` 的 agent 模式会调用它）：

1. 带着论文标题 / 摘要 / 正文 / 参考文献 / 笔记，告诉模型可以调用两个**只读工具**：
   * `verify_reference`：按标题、作者、年份、DOI、arXiv 号，在 Crossref、OpenAlex、
     Semantic Scholar、arXiv 上检索，判断这条参考文献是否真实存在；
   * `find_code_repository`：从论文正文与笔记里提取 GitHub / GitLab 等代码地址，
     联网确认仓库可达；正文里没有时，还能按标题去 GitHub 搜一次仓库。
2. 工具结果只作为**证据**记下来（不直接写正文），最后再用一次结构化 JSON 调用，
   让模型把「论文分析 + 笔记分析 + 参考文献核验结论 + 代码仓库」整理成报告。

**参考文献是全量核验的**（2026-11）：进模型之前先由 `_verify_all_references()`
把整张列表（上限 `MAX_AUTO_VERIFY` 条、并发 `AUTO_VERIFY_WORKERS` 条、
总时长 `AUTO_VERIFY_DEADLINE` 秒）逐条查一遍，统计「检索到且信息对得上」
（verified + likely）的条数写进报告的 `references_stats`；模型只负责复核
其中没过的那几条（最多 4 条），不再由它挑「值得核验的」。
环境变量 `NOTE_OUT_VERIFY_ALL=0` 可关闭全量核验（离线环境/测试用）。

设计原则：

* **不臆造**：工具返回什么就记什么；检索失败（网络不可用 / 限流）记 `unverified`，
  不能当成 `not_found`。
* **离线安全**：所有联网检索都包在 try/except 里，失败只写进结果，不打断导出；
  测试环境没有网络时，机械导出不受影响，Agent 导出只是证据少一些。
* **不依赖业务存储**：本模块只接收 `paper` / `sections` / `notes` 这些纯数据，
  不读数据库、不碰权限（权限与落库由 `app.py` 负责）。

对外接口：

| 函数 | 作用 |
| --- | --- |
| `run_note_out_agent()` | 跑工具循环 + 生成结构化报告，返回可序列化的 `report` 字典 |
| `extract_code_links()` | 从任意文本里提取代码仓库地址（机械导出也会复用） |
| `NOTE_OUT_TOOLS` | 工具声明（调试/测试用） |
"""
from __future__ import annotations

import difflib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from ..envfile import env_value as _env_value
from .llm import LlmClient, LlmUnavailable

# 学术库密钥的配置名（`.env` / 进程环境变量 / 账号设置都用这三个名字）
CFG_OPENALEX = "OPENALEX_API_KEY"
CFG_S2 = "SEMANTIC_SCHOLAR_API_KEY"
CFG_MAILTO = "SCHOLARLY_MAILTO"
CFG_S2_BASE = "SEMANTIC_SCHOLAR_BASE_URL"
# Semantic Scholar 服务地址：官方直连经常 429 / 国内不通，默认走兼容代理
# （地址只到 `/s2`，后面接 `/graph/v1/…`；鉴权用 `Authorization: Bearer <令牌>`）
S2_DEFAULT_BASE = "https://s2api.ominiai.cn/s2"
S2_OFFICIAL_HOST = "api.semanticscholar.org"


# =====================================================================
#  工具声明
# =====================================================================
NOTE_OUT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "verify_reference",
            "description": (
                "核验一条参考文献是否真实存在：按标题/作者/年份/DOI/arXiv 号，"
                "检索 Crossref、OpenAlex、Semantic Scholar 与 arXiv。"
                "返回 verified（确认存在）/ likely（高度疑似）/ not_found（没查到）/ "
                "unverified（网络不可用，无法判断）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reference_id": {
                        "type": "integer",
                        "description": "参考文献在导出列表里的编号（从 1 开始）",
                    },
                    "title": {
                        "type": "string",
                        "description": "文献标题；留空时按 reference_id 取导出列表里的原文",
                    },
                    "authors": {"type": "string", "description": "第一作者或作者串（可空）"},
                    "year": {"type": "integer", "description": "发表年份（可空）"},
                    "doi": {"type": "string", "description": "DOI（可空）"},
                    "arxiv": {"type": "string", "description": "arXiv 编号，如 2301.12345（可空）"},
                },
                "required": ["reference_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_code_repository",
            "description": (
                "提取论文的代码仓库地址并确认可达：从正文/笔记里的 GitHub、GitLab、"
                "Bitbucket、Hugging Face 链接中找；也可以按标题搜索 GitHub 仓库。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "已知的仓库地址（可空）"},
                    "query": {
                        "type": "string",
                        "description": "搜索词（默认用论文标题；可空）",
                    },
                    "search_github": {
                        "type": "boolean",
                        "description": "正文里没找到时，是否按标题搜索 GitHub（默认 true）",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
]


# =====================================================================
#  提示词
# =====================================================================
_AGENT_SYSTEM = (
    "你是论文阅读助手，负责在「笔记导出」之前做一轮核查。你可以调用两个工具：\n"
    "1. verify_reference：核验单条参考文献是否真实存在；\n"
    "2. find_code_repository：提取并确认论文的代码仓库地址。\n"
    "工作方式（重要）：参考文献**已经由系统自动全量核验**，结果见「参考文献自动核验」"
    "一节，你**不需要**再逐条核验。\n"
    "- 如果那一节里列出了「需要复核的条目」（not_found / likely / unverified），"
    "挑最多 4 条，用 verify_reference 换一种检索词再查一次（可选）；\n"
    "- 然后调用一次 find_code_repository 找代码。\n"
    "规则：\n"
    "- 工具没返回的结果不要编造；网络不可用时如实说明「无法核验」。\n"
    "- 不要写最终报告，完成工具调用后直接结束；系统会根据证据生成结构化报告。\n"
)

_REPORT_SYSTEM = (
    "你是论文阅读助手。下面给出论文的分节内容、用户的笔记、以及刚才工具核验得到的证据。"
    "请据此写一份**中文**分析报告，帮助用户回顾这篇论文和他的笔记。\n"
    "要求：\n"
    "1. 只依据给定内容，不要编造论文里没有的结论；正文被截断时不要假装看过全文。\n"
    "2. `summary`：200~400 字，说明这篇论文研究什么、怎么做、主要结论，以及用户的笔记集中在哪些点。\n"
    "3. `paper_overview`：包含 problem（问题）、method（方法）、contributions（贡献数组）、"
    "findings（发现/结论数组）、limitations（局限数组，没有就给空数组）。\n"
    "4. `notes_analysis`：整理**用户的笔记**（不是复述论文）：overview（3~5 句）、"
    "themes（[{title, points[]}]）、connections（关联/冲突）、questions（还没想清楚）、"
    "next（下一步可以做什么）。\n"
    "5. `references_review`：**只列需要人工复核的条目**（status 为 not_found / likely / "
    "unverified），每条 {id, status, note}；status 只能取这四个值且必须与证据一致，"
    "不能自己改。verified 的条目由系统自动补全，你不要写。\n"
    "6. `code_repositories`：只写证据里出现过的仓库 {url, verified, note}；"
    "没有就返回空数组。\n"
    "7. `conclusion`：一段总结（100~200 字）；`next_steps`：3~6 条可执行建议。\n"
    "8. 参考文献核验统计（total / checked / matched / verified / likely / not_found / "
    "unverified）由系统给出（`reference_stats`）。写 summary / conclusion 时可以直接引用"
    "这些数字（尤其是「检索到且信息对得上」的 `matched` 条数），**不要自己计算或编造数字**。\n"
    "9. 没有内容的字段返回空数组或空字符串，不要写「暂无」「无」之类的占位符。\n"
    "10. 数学公式一律用 LaTeX：行内 `$…$`、独立成行 `$$…$$`；**不要**用反引号代码或纯文本写公式。\n"
    "只输出 JSON："
    "{\"summary\":\"…\","
    "\"paper_overview\":{\"problem\":\"…\",\"method\":\"…\",\"contributions\":[],"
    "\"findings\":[],\"limitations\":[]},"
    "\"notes_analysis\":{\"overview\":\"…\",\"themes\":[{\"title\":\"…\",\"points\":[]}],"
    "\"connections\":[],\"questions\":[],\"next\":[]},"
    "\"references_review\":[{\"id\":1,\"status\":\"verified\",\"note\":\"…\"}],"
    "\"code_repositories\":[{\"url\":\"…\",\"verified\":true,\"note\":\"…\"}],"
    "\"conclusion\":\"…\",\"next_steps\":[]}"
)


# =====================================================================
#  一些上限（防止把整篇论文 / 整份证据链塞爆模型）
# =====================================================================
MAX_ABSTRACT_CHARS = 5000
MAX_MAIN_CHARS = 24000
MAX_APPENDIX_CHARS = 6000
MAX_REF_TEXT_CHARS = 600
MAX_REFS = 60
MAX_NOTES_ROWS = 120
MAX_NOTE_CHARS = 1000
MAX_TOOL_CALLS = 12
MAX_TOOL_ROUNDS = 5
MAX_TOOL_CANDIDATES = 4
# 全量核验：默认把整张参考文献列表都查一遍（不依赖模型挑选）
MAX_AUTO_VERIFY = 120        # 最多自动核验这么多条（再多也不会无限打外部接口）
AUTO_VERIFY_WORKERS = 3      # 并发核验的文献条数（每条内部还会并发查 3 个库）
AUTO_VERIFY_DEADLINE = 180.0  # 全量核验的总时长上限（秒），超时剩下的标 unverified

_STRONG_STATUS = {"verified", "likely", "not_found", "unverified"}

_UA = {
    "User-Agent": "tardigrade-note-out/1.0 (paper reader export)",
    "Accept": "application/json",
}


# =====================================================================
#  配置：账号设置 → 进程环境变量 → 仓库里的 .env
# =====================================================================
# 前端「设置 → 学术检索」里填的 key 存在账号记录里（`users.prefs.scholar`），
# 由 `app.py` 拼成这个模块认识的字典传进来；没配就回落到环境变量 / `.env`。
def _cfg_value(name: str, cfg: dict | None = None) -> str:
    """学术库配置值：账号配置 → 环境变量 → `.env` → 空串。"""
    if cfg:
        value = str(cfg.get(name) or "").strip()
        if value:
            return value
    return _env_value(name)


def _polite_mailto(cfg: dict | None = None) -> str:
    """学术库的「polite pool」邮箱（Crossref / OpenAlex 都认，限额更宽松）。"""
    return _cfg_value(CFG_MAILTO, cfg) or "tardigrade@example.invalid"


# 本轮已因限流熔断的学术库（由 `_verify_all_references` 每轮清空）
_DOWN_SOURCES: set[str] = set()


# =====================================================================
#  文本 / 代码链接
# =====================================================================
_RE_CODE_URL = re.compile(
    r"(?i)\bhttps?://(?:www\.)?(?:github\.com|gitlab\.com|bitbucket\.org|huggingface\.co)"
    r"/[^\s)\]}>\"'，。；、]+")
_RE_CODE_BARE = re.compile(
    r"(?i)(?<![\w@/])(?:github\.com|gitlab\.com|bitbucket\.org|huggingface\.co)"
    r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# 这些不是“论文代码仓库”，是站点自身的功能页
_CODE_FALSE_OWNERS = {
    "about", "features", "pricing", "enterprise", "marketplace", "sponsors",
    "topics", "collections", "settings", "login", "join", "site", "security",
    "readme", "explore", "trending", "apps", "contact", "customer-stories",
    "solutions", "resources", "docs", "help", "support", "pulls", "issues",
}
_TRAILING = ".,;:!?)]}>'\"，。；：！？）】》」』"


def _clean_code_url(url: str) -> str:
    url = str(url or "").strip().rstrip(_TRAILING)
    if not re.match(r"(?i)^https?://", url):
        url = "https://" + url
    # 去掉末尾的 .git / 多余路径（保留 owner/repo 两层）
    m = re.match(
        r"(?i)^(https?://(?:www\.)?(?:github\.com|gitlab\.com|bitbucket\.org|"
        r"huggingface\.co)/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", url)
    if m:
        url = m.group(1)
    return url.rstrip(_TRAILING)


def extract_code_links(text: str) -> list[str]:
    """从任意文本里提取代码仓库地址（去重、补 `https://`、裁到 owner/repo）。

    这是**确定性**的：机械导出用它列出「论文里提到的代码地址」，
    Agent 的工具也用它做第一轮候选（再去联网确认）。
    """
    found: list[str] = []
    for m in _RE_CODE_URL.finditer(str(text or "")):
        url = _clean_code_url(m.group(0))
        if _valid_code_url(url):
            found.append(url)
    for m in _RE_CODE_BARE.finditer(str(text or "")):
        url = _clean_code_url(m.group(0))
        if _valid_code_url(url):
            found.append(url)
    out: list[str] = []
    seen: set[str] = set()
    for url in found:
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


def _valid_code_url(url: str) -> bool:
    m = re.match(
        r"(?i)^https?://(?:www\.)?(github\.com|gitlab\.com|bitbucket\.org|huggingface\.co)"
        r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$", url)
    if not m:
        return False
    owner = m.group(2).lower()
    repo = m.group(3).lower()
    if owner in _CODE_FALSE_OWNERS or repo in (".", ".."):
        return False
    return True


# =====================================================================
#  工具实现：参考文献核验
# =====================================================================
def _norm_text(value: Any, limit: int = 1000) -> str:
    return " ".join(str(value or "").split())[:limit]


def _title_key(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(text or "").lower())


def _title_similarity(a: str, b: str) -> float:
    ka, kb = _title_key(a), _title_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    if len(ka) >= 20 and (ka in kb or kb in ka):
        return 0.94
    return difflib.SequenceMatcher(None, ka, kb).ratio()


def _http_json(url: str, params: dict | None = None,
               timeout: float = 8.0, *, headers: dict | None = None,
               retries: int = 1, source: str = "") -> tuple[dict | None, str]:
    """GET 一个 JSON 接口；失败返回 `(None, 原因)`，绝不抛异常。

    429 / 5xx 会退避重试一次 —— 学术库（尤其是 Semantic Scholar）限流很常见，
    不重试的话一次抖动就会白白把真实存在的文献记成「没查到」。
    连续 429 的库会被熔断（本轮不再请求），免得几十条文献全在等限流。
    """
    request_headers = dict(_UA)
    if headers:
        request_headers.update(headers)
    delay = 0.8
    last = ""
    for attempt in range(retries + 1):
        try:
            resp = httpx.get(url, params=params, headers=request_headers,
                             timeout=timeout, follow_redirects=True)
            if resp.status_code == 200:
                data = resp.json()
                return (data if isinstance(data, dict) else None), ""
            last = f"HTTP {resp.status_code}"
            if resp.status_code == 429 and source:
                _DOWN_SOURCES.add(source)      # 额度用完/被限流：本轮不再打它
            if resp.status_code != 429 and resp.status_code < 500:
                return None, last          # 4xx（除了限流）重试也没用
        except Exception as exc:                       # 网络 / 解析 / 超时
            last = f"{type(exc).__name__}: {exc}"[:200]
        if attempt < retries:
            time.sleep(delay)
            delay *= 2
    return None, last


def _source_ready(name: str) -> bool:
    """这个学术库本轮还能不能请求（被 429 熔断的就不打丁）。"""
    return name not in _DOWN_SOURCES


def _crossref_work(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    title = item.get("title")
    if isinstance(title, list):
        title = title[0] if title else ""
    year = ""
    issued = item.get("issued")
    if isinstance(issued, dict):
        parts = issued.get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            year = parts[0][0]
    authors = []
    for a in item.get("author") or []:
        if not isinstance(a, dict):
            continue
        name = " ".join(x for x in (a.get("given"), a.get("family")) if x)
        if name:
            authors.append(name)
    doi = str(item.get("DOI") or "").strip()
    return {
        "title": _norm_text(title, 500),
        "year": int(year) if str(year).isdigit() else None,
        "authors": authors[:8],
        "doi": doi,
        "url": ("https://doi.org/" + doi) if doi else _norm_text(item.get("URL"), 300),
        "venue": _norm_text((item.get("container-title") or [""])[0]
                            if isinstance(item.get("container-title"), list)
                            else item.get("container-title"), 200),
        "source": "crossref",
    }


def _openalex_work(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    authors = []
    for a in item.get("authorships") or []:
        if not isinstance(a, dict):
            continue
        name = ((a.get("author") or {}).get("display_name")
                if isinstance(a.get("author"), dict) else "")
        if name:
            authors.append(name)
    doi = _norm_text(item.get("doi"), 200).replace("https://doi.org/", "")
    year = item.get("publication_year")
    location = item.get("primary_location")
    source = location.get("source") if isinstance(location, dict) else None
    venue = source.get("display_name") if isinstance(source, dict) else ""
    return {
        "title": _norm_text(item.get("display_name") or item.get("title"), 500),
        "year": int(year) if isinstance(year, int) else None,
        "authors": authors[:8],
        "doi": doi,
        "url": _norm_text(item.get("id") or item.get("doi"), 300),
        "venue": _norm_text(venue, 200),
        "source": "openalex",
    }


def _semantic_work(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    authors = [str((a or {}).get("name") or "") for a in item.get("authors") or []
               if isinstance(a, dict)]
    ids = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
    return {
        "title": _norm_text(item.get("title"), 500),
        "year": item.get("year") if isinstance(item.get("year"), int) else None,
        "authors": [a for a in authors if a][:8],
        "doi": _norm_text(ids.get("DOI"), 200),
        "url": _norm_text(item.get("url"), 300),
        "venue": _norm_text(item.get("venue"), 200),
        "source": "semantic_scholar",
    }


def _crossref_search(query: str, rows: int = 3,
                     cfg: dict | None = None) -> tuple[list[dict], str]:
    data, err = _http_json(
        "https://api.crossref.org/works",
        {"query.bibliographic": query, "rows": rows, "mailto": _polite_mailto(cfg),
         "select": "DOI,title,author,issued,container-title,URL"}, source="crossref")
    if err:
        return [], err
    items = ((data or {}).get("message") or {}).get("items") or []
    return [x for x in (_crossref_work(i) for i in items) if x], ""


def _openalex_search(query: str, rows: int = 3,
                     cfg: dict | None = None) -> tuple[list[dict], str]:
    params = {"search": query, "per-page": rows, "mailto": _polite_mailto(cfg)}
    key = _cfg_value(CFG_OPENALEX, cfg)
    if key:                                # 没 key 时用的是全网共享的免费日额度
        params["api_key"] = key
    data, err = _http_json("https://api.openalex.org/works", params, source="openalex")
    if err:
        return [], err
    return [x for x in (_openalex_work(i) for i in (data or {}).get("results") or [])
            if x], ""


def _s2_base(cfg: dict | None = None) -> str:
    """Semantic Scholar 服务地址：账号配置 → 环境变量/.env → 内置默认（代理）。"""
    return (_cfg_value(CFG_S2_BASE, cfg) or S2_DEFAULT_BASE).strip().rstrip("/")


def _semantic_search(query: str, rows: int = 3,
                     cfg: dict | None = None) -> tuple[list[dict], str]:
    base = _s2_base(cfg)
    official = S2_OFFICIAL_HOST in base
    if official:
        _s2_gate()                  # 官方免费池约 1 req/s，必须让一让；代理不用
    headers = {}
    key = _cfg_value(CFG_S2, cfg)
    if key:
        # 官方：x-api-key；自建/中转（如 s2api）：Authorization: Bearer <令牌>
        if official:
            headers["x-api-key"] = key
        else:
            headers["Authorization"] = "Bearer " + key
    data, err = _http_json(
        f"{base}/graph/v1/paper/search",
        {"query": query, "limit": rows,
         "fields": "title,year,authors,externalIds,url,venue"}, headers=headers,
        source="semantic_scholar")
    if err:
        if err.startswith("HTTP 401"):
            err += "（令牌无效或未配置，检查「设置 → 学术检索」里的 Semantic Scholar 令牌）"
        return [], err
    return [x for x in (_semantic_work(i) for i in (data or {}).get("data") or [])
            if x], ""


def _crossref_doi(doi: str, cfg: dict | None = None) -> tuple[dict | None, str]:
    data, err = _http_json("https://api.crossref.org/works/" + quote(doi, safe=""),
                           {"mailto": _polite_mailto(cfg)})
    if err:
        return None, err
    return _crossref_work((data or {}).get("message") or {}), ""


def _arxiv_lookup(arxiv_id: str) -> tuple[dict | None, str]:
    """按 arXiv 编号查一条文献（Atom XML）。"""
    try:
        resp = httpx.get("https://export.arxiv.org/api/query",
                         params={"id_list": arxiv_id}, headers=_UA, timeout=8.0,
                         follow_redirects=True)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        root = ElementTree.fromstring(resp.content)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entry = root.find("a:entry", ns)
        if entry is None:
            return None, "没有找到该 arXiv 编号"
        title = _norm_text(entry.findtext("a:title", default="", namespaces=ns), 500)
        published = entry.findtext("a:published", default="", namespaces=ns)
        authors = [_norm_text(a.findtext("a:name", default="", namespaces=ns), 200)
                   for a in entry.findall("a:author", ns)]
        link = entry.findtext("a:id", default="", namespaces=ns)
        year = int(published[:4]) if published[:4].isdigit() else None
        return {
            "title": title,
            "year": year,
            "authors": [a for a in authors if a][:8],
            "doi": "",
            "url": _norm_text(link, 300),
            "venue": "arXiv",
            "source": "arxiv",
        }, ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"[:200]


def _arxiv_search(query: str, rows: int = 3) -> tuple[list[dict], str]:
    """arXiv **标题检索**（Atom XML）。免费、无需 key，CS / ML 论文覆盖极好。"""
    _arxiv_gate()
    phrase = str(query or "").replace('"', " ").replace("\n", " ").strip()[:200]
    if not phrase:
        return [], "缺少检索词"
    try:
        resp = httpx.get("https://export.arxiv.org/api/query",
                         params={"search_query": f'ti:"{phrase}"',
                                 "max_results": rows},
                         headers=_UA, timeout=12.0, follow_redirects=True)
        if resp.status_code != 200:
            if resp.status_code == 429:
                _DOWN_SOURCES.add("arxiv")
            return [], f"HTTP {resp.status_code}"
        root = ElementTree.fromstring(resp.content)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        found: list[dict] = []
        for entry in root.findall("a:entry", ns):
            title = _norm_text(entry.findtext("a:title", default="", namespaces=ns), 500)
            if not title or title.lower() == "error":
                continue
            published = entry.findtext("a:published", default="", namespaces=ns)
            authors = [_norm_text(a.findtext("a:name", default="", namespaces=ns), 200)
                       for a in entry.findall("a:author", ns)]
            found.append({
                "title": title,
                "year": int(published[:4]) if published[:4].isdigit() else None,
                "authors": [a for a in authors if a][:8],
                "doi": "",
                "url": _norm_text(entry.findtext("a:id", default="", namespaces=ns), 300),
                "venue": "arXiv",
                "source": "arxiv",
            })
        return found, ""
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"[:200]


PROBE_QUERY = "Attention is all you need"


def probe_scholar(cfg: dict | None = None) -> dict:
    """用一条公开论文探一下各学术库能不能用（设置里的「测试连接」）。

    只发一次小请求，不改任何持久状态；被熔断的源会先临时放行再试。
    返回 `{ok, message, checks:[{name, label, ok, count, error, keyed}]}`。
    """
    plan = (
        ("openalex", "OpenAlex", CFG_OPENALEX,
         lambda: _openalex_search(PROBE_QUERY, 1, cfg)),
        ("semantic_scholar", "Semantic Scholar", CFG_S2,
         lambda: _semantic_search(PROBE_QUERY, 1, cfg)),
        ("arxiv", "arXiv", "", lambda: _arxiv_search(PROBE_QUERY, 1)),
        ("crossref", "Crossref", CFG_MAILTO,
         lambda: _crossref_search(PROBE_QUERY, 1, cfg)),
    )
    checks: list[dict] = []
    for name, label, key_name, fn in plan:
        _DOWN_SOURCES.discard(name)
        keyed = bool(_cfg_value(key_name, cfg)) if key_name else None
        try:
            rows, err = fn()
        except Exception as exc:
            rows, err = [], f"{type(exc).__name__}: {exc}"[:200]
        if err.startswith("HTTP 403") and keyed:
            # 403 基本就是 Key 无效/过期，别让用户以为是网络问题
            err += "（Key 被拒绝：可能已失效或没有权限，请重新申请）"
        check = {
            "name": name,
            "label": label,
            "ok": bool(rows) and not err,
            "count": len(rows),
            "error": err,
            "keyed": keyed,
        }
        if name == "semantic_scholar":
            check["base"] = _s2_base(cfg)
        checks.append(check)
    failed = [c["label"] for c in checks if not c["ok"]]
    message = ("✅ 四个学术库都可用" if not failed
               else "⚠️ 以下学术库当前不可用：" + "、".join(failed))
    return {"ok": not failed, "message": message, "checks": checks}


def _pick_best(query_title: str, candidates: list[dict], year: int | None = None
               ) -> tuple[dict | None, float]:
    best, best_score = None, 0.0
    for cand in candidates:
        score = _title_similarity(query_title, cand.get("title") or "")
        cy = cand.get("year")
        if year and isinstance(cy, int):
            if year == cy:
                score = min(1.0, score + 0.04)
            elif abs(year - cy) <= 1:
                score = min(1.0, score + 0.015)
        if score > best_score:
            best, best_score = cand, score
    return best, best_score


def _verify_reference(reference_id: int, *, title: str, authors: str, year: int | None,
                      doi: str, arxiv: str, query: str,
                      cfg: dict | None = None) -> dict:
    """核验一条参考文献；返回工具结果（同时也是证据）。

    `cfg` 是当前账号的学术库配置（`OPENALEX_API_KEY` / `SEMANTIC_SCHOLAR_API_KEY` /
    `SCHOLARLY_MAILTO`）；不传就回落到环境变量 / `.env`。
    """
    query = _norm_text(query or title, 600)
    candidates: list[dict] = []
    errors: list[str] = []
    sources: list[str] = []

    # 1) DOI / arXiv 是强标识，优先精确查
    if doi:
        cand, err = _crossref_doi(doi, cfg)
        if cand and cand.get("title"):
            cand["doi"] = cand.get("doi") or doi
            cand["match"] = "doi"
            sources.append("crossref:doi")
            return _reference_result(reference_id, query, [cand], "verified", sources, errors)
        errors.append("DOI：" + (err or "没有查到"))
    if arxiv:
        cand, err = _arxiv_lookup(arxiv)
        if cand and cand.get("title"):
            cand["match"] = "arxiv"
            sources.append("arxiv:id")
            return _reference_result(reference_id, query, [cand], "verified", sources, errors)
        errors.append("arXiv：" + (err or "没有查到"))

    # 2) 标题 / 原文检索：三个学术库并发查（串行最坏要 24s，太慢）
    search_text = " ".join(x for x in (title or query, authors, str(year or "")) if x)
    if not search_text.strip():
        return {"ok": False, "reference_id": reference_id,
                "status": "unverified", "message": "缺少可检索的标题/作者/年份",
                "candidates": [], "errors": errors}

    # Crossref 吃「整条引用」最好；OpenAlex 用标题更准；
    # Semantic Scholar 限流很严，只在前面两个都没找到确凿匹配时才用。
    title_query = _norm_text(title or query, 600)
    primary = (("crossref", search_text, _crossref_search),
               ("openalex", title_query, _openalex_search))
    skipped: list[str] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = []
        for name, text, fn in primary:
            if not _source_ready(name):
                skipped.append(name)          # 本轮已被限流熔断
                continue
            futures.append((name, pool.submit(fn, text, 3, cfg)))
        for name, future in futures:
            try:
                got, err = future.result()
            except Exception as exc:           # 工具函数本身已兜底，这里再兜一层
                got, err = [], f"{type(exc).__name__}: {exc}"[:200]
            if err:
                errors.append(f"{name}：{err}")
            if got:
                sources.append(name)
                candidates.extend(got)

    best, score = _pick_best(title or query, candidates, year) if candidates \
        else (None, 0.0)
    # Crossref 对 arXiv / 会议论文的元数据经常很潦草，第二个免费源用 arXiv 标题检索
    if score < 0.86 and _source_ready("arxiv"):
        got, err = _arxiv_search(title_query, 3)
        if err:
            errors.append("arxiv：" + err)
        if got:
            sources.append("arxiv")
            candidates.extend(got)
            best, score = _pick_best(title or query, candidates, year)
    if score < 0.86:
        if _source_ready("semantic_scholar"):
            got, err = _semantic_search(title_query, 3, cfg)
            if err:
                errors.append("semantic_scholar：" + err)
            if got:
                sources.append("semantic_scholar")
                candidates.extend(got)
                best, score = _pick_best(title or query, candidates, year)
        else:
            skipped.append("semantic_scholar")

    if not candidates:
        return {"ok": True, "reference_id": reference_id, "status": "unverified",
                "message": "所有学术库都没有返回结果（可能是网络不可用/被限流）",
                "query": query, "candidates": [], "errors": errors[:6]}
    if score >= 0.86:
        status = "verified"
    elif score >= 0.68:
        status = "likely"
    else:
        status = "not_found"
    return _reference_result(reference_id, query, candidates, status, sources, errors,
                             best=best, score=score, skipped=skipped)


def _reference_result(reference_id: int, query: str, candidates: list[dict], status: str,
                      sources: list[str], errors: list[str], *,
                      best: dict | None = None, score: float = 0.0,
                      skipped: list[str] | None = None) -> dict:
    if best is None and candidates:
        best, score = _pick_best(query, candidates)
    top: list[dict] = []
    seen: set[str] = set()
    for cand in sorted(candidates, key=lambda c: _title_similarity(
            query, c.get("title") or ""), reverse=True)[:MAX_TOOL_CANDIDATES]:
        key = (cand.get("doi") or cand.get("url") or cand.get("title") or "").lower()
        if key in seen:
            continue
        seen.add(key)
        top.append({
            "title": _norm_text(cand.get("title"), 300),
            "year": cand.get("year"),
            "authors": (cand.get("authors") or [])[:4],
            "doi": _norm_text(cand.get("doi"), 120),
            "url": _norm_text(cand.get("url"), 300),
            "venue": _norm_text(cand.get("venue"), 160),
            "source": cand.get("source") or "",
        })
    msg = {
        "verified": "已确认这条文献存在",
        "likely": "检索到高度相似的文献，但标题/年份不是完全一致",
        "not_found": "学术库里没有检索到匹配的文献",
        "unverified": "无法完成核验",
    }.get(status, "无法完成核验")
    return {
        "ok": True,
        "reference_id": reference_id,
        "status": status,
        "message": msg,
        "query": _norm_text(query, 400),
        "best": ({
            "title": _norm_text(best.get("title"), 300),
            "year": best.get("year"),
            "doi": _norm_text(best.get("doi"), 120),
            "url": _norm_text(best.get("url"), 300),
            "similarity": round(float(score), 3),
        } if best else None),
        "candidates": top,
        "sources": sources,
        "sources_skipped": skipped or [],
        "errors": errors[:6],
    }


# =====================================================================
#  工具实现：代码仓库
# =====================================================================
def _check_url(url: str) -> dict:
    """访问一次仓库地址，确认它可达（不下载大文件，只取响应头/前一点内容）。"""
    try:
        resp = httpx.get(url, headers={"User-Agent": _UA["User-Agent"]},
                         timeout=8.0, follow_redirects=True)
        return {
            "url": url,
            "final_url": str(resp.url),
            "verified": resp.status_code < 400,
            "status_code": resp.status_code,
            "note": "" if resp.status_code < 400 else f"HTTP {resp.status_code}",
        }
    except Exception as exc:
        return {"url": url, "final_url": url, "verified": False,
                "status_code": 0, "note": f"{type(exc).__name__}: {exc}"[:160]}


def _github_search(query: str, per_page: int = 5) -> tuple[list[dict], str]:
    data, err = _http_json(
        "https://api.github.com/search/repositories",
        {"q": query, "sort": "stars", "order": "desc", "per_page": per_page},
        timeout=8.0)
    if err:
        return [], err
    rows = []
    for item in (data or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        html_url = _clean_code_url(item.get("html_url") or "")
        if not _valid_code_url(html_url):
            continue
        rows.append({
            "url": html_url,
            "final_url": html_url,
            "verified": True,
            "status_code": 200,
            "source": "github_search",
            "stars": item.get("stargazers_count"),
            "description": _norm_text(item.get("description"), 300),
            "note": "按标题搜索到的仓库（不一定就是论文官方实现）",
        })
    return rows, ""


def _find_code_repository(*, source_text: str, paper_title: str, url: str = "",
                          query: str = "", search_github: bool = True) -> dict:
    from_text = extract_code_links(source_text)
    if url:
        clean = _clean_code_url(url)
        if _valid_code_url(clean):
            from_text.insert(0, clean)

    candidates: list[dict] = []
    seen: set[str] = set()
    for candidate in from_text:
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        row = _check_url(candidate)
        row["source"] = "paper_text"
        candidates.append(row)

    errors: list[str] = []
    if not candidates and search_github:
        rows, err = _github_search((query or paper_title or "").strip(), 5)
        if err:
            errors.append("GitHub 搜索：" + err)
        for row in rows:
            key = row["url"].lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(row)

    verified = [c for c in candidates if c.get("verified")]
    if verified:
        message = f"找到 {len(verified)} 个可达的代码地址"
    elif candidates:
        message = "提取到代码地址，但都没能确认可达"
    else:
        message = "没有在论文正文/笔记里找到代码地址"
    return {
        "ok": True,
        "message": message,
        "candidates": candidates[:MAX_TOOL_CANDIDATES],
        "errors": errors[:4],
    }


# =====================================================================
#  工具循环
# =====================================================================
def _refs_of(sections: dict) -> list[dict]:
    refs = (sections or {}).get("references") or {}
    entries = refs.get("entries") if isinstance(refs, dict) else None
    return [e for e in (entries or []) if isinstance(e, dict)]


def _notes_text(notes: dict) -> str:
    rows: list[str] = []
    for n in (notes or {}).get("sentence_notes") or []:
        if not isinstance(n, dict):
            continue
        rows.append(
            f"- [P{n.get('page') or '?'}] 原句：{_norm_text(n.get('sentence'), 500)}\n"
            f"  笔记：{_norm_text(n.get('note'), MAX_NOTE_CHARS)}")
        if len(rows) >= MAX_NOTES_ROWS:
            break
    for n in (notes or {}).get("free_notes") or []:
        if not isinstance(n, dict):
            continue
        rows.append(f"- [独立笔记] {_norm_text(n.get('title'), 120)}："
                    f"{_norm_text(n.get('text'), MAX_NOTE_CHARS)}")
        if len(rows) >= MAX_NOTES_ROWS + 40:
            break
    return "\n".join(rows) or "（没有笔记）"


def _agent_context(paper: dict, sections: dict, notes: dict,
                   instructions: str = "", state: dict | None = None) -> str:
    refs = _refs_of(sections)
    ref_lines = []
    for i, e in enumerate(refs[:MAX_REFS], 1):
        n = e.get("index") or i
        ref_lines.append(f"[{n}] " + _norm_text(e.get("text"), MAX_REF_TEXT_CHARS))
    abstract = _norm_text((sections.get("abstract") or {}).get("text"),
                          MAX_ABSTRACT_CHARS)
    main = _norm_text((sections.get("main_text") or {}).get("text"), MAX_MAIN_CHARS)
    appendix = _norm_text((sections.get("appendix") or {}).get("text"),
                          MAX_APPENDIX_CHARS)
    stats = _reference_stats(sections, state)
    attention = _attention_lines(sections, state)
    stats_block = [
        "===== 参考文献自动核验（已全量完成） =====",
        f"共 {stats['total']} 条，已核验 {stats['checked']} 条；"
        f"检索到且信息对得上 {stats['matched']} 条"
        f"（verified {stats['verified']} / likely {stats['likely']}），"
        f"没查到 {stats['not_found']} 条，无法核验 {stats['unverified']} 条。",
    ]
    if attention:
        stats_block += ["需要复核的条目：", *attention]
    return "\n\n".join([
        "===== 论文信息 =====",
        f"标题：{paper.get('title') or '（未知）'}\n页数：{paper.get('num_pages') or '?'}",
        "===== 摘要 =====", abstract or "（没有识别到摘要）",
        "===== 正文（可能截断） =====", main or "（没有正文）",
        *(["===== 附录（可能截断） =====", appendix] if appendix else []),
        "===== 参考文献 =====",
        "\n".join(ref_lines) or "（没有识别到参考文献）",
        *stats_block,
        "===== 用户笔记 =====", _notes_text(notes),
        "===== 额外要求 =====", (instructions or "（无）")[:2000],
        "参考文献已由系统全量核验，不要重复核验；只对上面「需要复核的条目」"
        "挑最多 4 条用 verify_reference 再查一次，再调用 find_code_repository 找代码。"
        "完成后停止，不要自己写最终报告。",
    ])


def _execute_tool(name: str, args: dict, *, paper: dict, sections: dict,
                  notes: dict, state: dict) -> dict:
    if name == "verify_reference":
        rid = args.get("reference_id")
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            return {"ok": False, "message": "reference_id 必须是整数"}
        refs = _refs_of(sections)
        raw = ""
        for pos, entry in enumerate(refs, 1):
            entry_id = _entry_ref_id(entry, pos)
            if rid in (pos, entry_id):
                rid = entry_id          # 统一用条目自己的编号，避免同一篇存两份证据
                raw = str(entry.get("text") or "")
                break
        title = _norm_text(args.get("title") or "", 600)
        if not title:
            # 从原文里粗略取标题：去掉编号/作者/年份后的前半段
            title = _guess_title(raw)
        doi = _norm_text(args.get("doi") or "", 200) or _find_doi(raw)
        arxiv = _norm_text(args.get("arxiv") or "", 80) or _find_arxiv(raw)
        year = args.get("year")
        try:
            year = int(year) if year not in (None, "") else _find_year(raw)
        except (TypeError, ValueError):
            year = _find_year(raw)
        authors = _norm_text(args.get("authors") or "", 300) or _guess_authors(raw)
        result = _verify_reference(
            rid, title=title, authors=authors, year=year, doi=doi,
            arxiv=arxiv, query=args.get("query") or raw,
            cfg=state.get("scholar") or None)
        # 证据表：同一条被核验多次时，verified/likely 优先
        prev = state["references"].get(rid)
        if not prev or _status_rank(result.get("status")) >= _status_rank(
                prev.get("status")):
            state["references"][rid] = result
        return result

    if name == "find_code_repository":
        source_text = "\n".join(
            str((sections.get(k) or {}).get("text") or "")
            for k in ("title", "abstract", "main_text", "references", "appendix"))
        source_text += "\n" + _notes_text(notes)
        result = _find_code_repository(
            source_text=source_text,
            paper_title=str(paper.get("title") or ""),
            url=_norm_text(args.get("url") or "", 300),
            query=_norm_text(args.get("query") or "", 300),
            search_github=args.get("search_github") is not False)
        prev = state.get("code")
        if not prev or (result.get("candidates") and not prev.get("candidates")):
            state["code"] = result
        return result

    return {"ok": False, "message": "不支持的工具"}


def _status_rank(status: Any) -> int:
    return {"unverified": 0, "not_found": 1, "likely": 2, "verified": 3}.get(
        str(status or ""), 0)


_RE_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.I)
_RE_ARXIV = re.compile(r"(?i)arxiv\s*:?\s*(\d{4}\.\d{4,5})(?:v\d+)?")
_RE_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")

# Semantic Scholar 免费额度大约 1 req/s，全局排队，别把自己的请求刷成 429
_S2_MIN_INTERVAL = 1.0
_S2_LOCK = threading.Lock()
_S2_NEXT = [0.0]
# arXiv 官方建议请求间隔 ≥ 3s，这里取一个折中值（只在 Crossref 没匹配上时才用它）
_ARXIV_MIN_INTERVAL = 1.2
_ARXIV_LOCK = threading.Lock()
_ARXIV_NEXT = [0.0]


def _rate_gate(lock: threading.Lock, next_slot: list, interval: float) -> None:
    """全局限速：保证两次请求之间至少间隔 `interval` 秒。"""
    with lock:
        now = time.monotonic()
        wait_for = next_slot[0] - now
        next_slot[0] = max(now, next_slot[0]) + interval
    if wait_for > 0:
        time.sleep(min(wait_for, interval))


def _s2_gate() -> None:
    """Semantic Scholar 全局限速（其他库不需要）。"""
    _rate_gate(_S2_LOCK, _S2_NEXT, _S2_MIN_INTERVAL)


def _arxiv_gate() -> None:
    _rate_gate(_ARXIV_LOCK, _ARXIV_NEXT, _ARXIV_MIN_INTERVAL)


def _find_doi(text: str) -> str:
    m = _RE_DOI.search(str(text or ""))
    return m.group(0).rstrip(".,;") if m else ""


def _find_arxiv(text: str) -> str:
    m = _RE_ARXIV.search(str(text or ""))
    return m.group(1) if m else ""


def _find_year(text: str) -> int | None:
    years = [int(y) for y in _RE_YEAR.findall(str(text or ""))]
    return years[-1] if years else None


def _guess_authors(raw: str) -> str:
    """从条目原文里猜作者串：编号后的第一句（到句点/年份前）。"""
    text = re.sub(r"^\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[.)])\s*", "", str(raw or ""))
    m = re.match(r"(.{2,160}?)(?:\.\s|\s\((?:19|20)\d{2}|,\s(?:19|20)\d{2})", text)
    return _norm_text(m.group(1), 200) if m else ""


_RE_NUMBERING = re.compile(r"^\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[.)])\s*")
# 期刊 / 会议 / 预印本等“出处”开头的片段，不是标题
_VENUE_PREFIX = re.compile(
    r"(?i)^(?:in\s+|proceedings|proc\.|corr\b|arxiv\b|journal|transactions|advances\s+in\b|conference|nips\b|neurips\b|icml\b|iclr\b|acl\b|emnlp\b|naacl\b|cvpr\b|iccv\b|eccv\b|aaai\b|ijcai\b|ieee\b|acm\b|springer\b|mit\s+press\b|pmlr\b|adv\.\s+neural)")
# 出处 / 编者 / 页码 / 出版社这种片段，绝不能当标题
_VENUE_HINT = re.compile(
    r"(?i)\b(?:editors?|eds\.|pages?|pp\.|vol\.|volume|proceedings|press|publisher|"
    r"inc\.|ltd\b|corr\b|arxiv\b|advances\s+in\s+neural)\b")
_AUTHOR_PARTICLES = {"and", "&", "et", "al", "de", "van", "von", "der", "den",
                     "del", "la", "le", "da", "di", "bin", "ibn", "y", "e"}
# 标题后面紧跟的出处（`… attention? In Advances in Neural Information …`）
_RE_INLINE_VENUE = re.compile(
    r"(?i)\s+in\s+(?:advances|proceedings|proc\.|international|conference|"
    r"the\s+\d|nips|neurips|icml|iclr|aaai|cvpr|iccv|eccv|acl|emnlp|naacl|ieee|acm|"
    r"journal|corr|arxiv|transactions|mit\s+press|pmlr)\b")


def _strip_inline_venue(part: str) -> str:
    """把附在标题后面的 ` In <会议/期刊>` 切掉（标题本身不变）。"""
    text = str(part or "")
    m = _RE_INLINE_VENUE.search(text)
    if m and len(text[:m.start()].split()) >= 2:
        return text[:m.start()].strip(" .,;:")
    return text


def _is_venue_part(part: str) -> bool:
    """这一段是不是「出处 / 编者 / 页码」而不是标题。"""
    text = str(part or "")
    return bool(_VENUE_PREFIX.match(text) or _VENUE_HINT.search(text))


def _all_words_capitalized(part: str) -> bool:
    """所有实词都大写开头（允许 and / van / et al 这类姓名虚词）。"""
    tokens = [t for t in re.findall(r"[^\s,]+(?=\s|$)", part) if t.strip(".,;:()[]")]
    if not tokens or len(tokens) > 14:
        return False
    for token in tokens:
        word = token.strip(".,;:()[]")
        if word.lower() in _AUTHOR_PARTICLES:
            continue
        if not word[:1].isupper():
            return False
    return True


def _looks_like_authors(part: str, rest: list[str] | None = None) -> bool:
    """这一段是不是“作者串”。

    除了「多逗号 + and」/「多个首字母缩写」这些硬信号，还处理
    `Sepp Hochreiter and Jürgen Schmidhuber` 这种**只有两个作者、没有缩写**的写法：
    全大写开头 + 后面存在像标题的段（≥ 2 词且不是出处）才算作者 ——
    单独一个 Title Case 标题（如 `Attention Is All You Need`）后面跟的是 `In NIPS`，
    不会被误删。
    """
    text = str(part or "").strip()
    if not text:
        return False
    if text.count(",") >= 2 and re.search(r"(?i)\band\b|，", text):
        return True
    if len(re.findall(r"\b(?:[A-Z]\.\s*){1,3}", text)) >= 2:
        return True
    if text.count(",") >= 3 and len(text.split()) >= 8:
        return True
    if _all_words_capitalized(text):
        # 标题里可能带冒号（`Xception: Deep learning …`），所以看**后面所有段**
        return any(len(p.split()) >= 2 and not _is_venue_part(p)
                   for p in (rest or []))
    return False


def _guess_title(raw: str) -> str:
    """从条目原文里猜标题（全量核验的检索词就靠它）。

    参考文献条目基本都是 `作者. 标题. 出处, 年份`。旧实现在“年份前取最长片段”，
    而作者串/编者串往往比标题长得多，结果全量核验时把**作者当标题**去检索，
    真实存在的文献也会被判成 not_found。现在：

    1. 剔掉开头的编号、arXiv 号，在年份处截断；
    2. 去掉 `N. D.` 这类首字母缩写里的点，避免被后面的切分拆碎；
    3. 按 `.` / `:` 切段，把开头的「作者串」整段丢掉；
    4. 剩下的取**第一个**不是出处（In … / editors / pages / Press）的段 —— 
       因为标题几乎总是紧跟在作者后面，比“取最长”可靠得多。
    """
    text = _RE_NUMBERING.sub("", str(raw or ""))
    text = re.sub(r"(?i)arxiv\s*:?\s*\d{4}\.\d{4,5}(?:v\d+)?", " ", text)
    m = re.search(r"((?:19|20)\d{2})", text)
    before = text[:m.start()] if m else text
    before = re.sub(r"\b([A-Z])\.\s*", r"\1 ", before)      # N. D. → N D
    # 只按句点切：“Xception: Deep learning …” 这种带冒号的标题不能被切开
    parts = [p.strip(" .;,") for p in re.split(r"[.。]", before) if p.strip(" .;,")]
    parts = [p for p in (_strip_inline_venue(p) for p in parts) if p]
    # 作者段只有一个：只剥一层，再剥就会把标题（`A, B, and C` 句式）也剥掉
    if len(parts) > 1 and _looks_like_authors(parts[0], parts[1:]):
        if any(not _is_venue_part(p) for p in parts[1:]):
            parts = parts[1:]
    for part in parts:
        if not _is_venue_part(part) and len(part.split()) >= 2:
            return _norm_text(part, 400)
    for part in parts:
        if not _is_venue_part(part):
            return _norm_text(part, 400)
    return _norm_text(before, 400)


# =====================================================================
#  全量核验（确定性，不依赖模型挑选）
# =====================================================================
def _entry_ref_id(entry: dict, pos: int) -> int:
    """参考文献条目的编号：优先用它自带的 `index`，否则用列表位置（从 1 开始）。"""
    try:
        value = int((entry or {}).get("index") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else pos


def _unverified_result(rid: int, message: str) -> dict:
    return {"ok": False, "reference_id": rid, "status": "unverified",
            "message": message, "candidates": [], "errors": []}


def _reference_stats(sections: dict, state: dict | None) -> dict:
    """参考文献核验统计。

    核心数字是 `matched`（= verified + likely）：**在学术库里检索到、
    且标题/年份对得上**的条数；`verified` 是其中严格一致的那部分。
    """
    total = len(_refs_of(sections))
    evidence = (state or {}).get("references") or {}
    counts = {"verified": 0, "likely": 0, "not_found": 0, "unverified": 0}
    for ev in evidence.values():
        status = str((ev or {}).get("status") or "").strip().lower()
        counts[status if status in counts else "unverified"] += 1
    checked = sum(counts.values())
    matched = counts["verified"] + counts["likely"]
    return {
        "total": total,
        "checked": checked,
        "not_checked": max(0, total - checked),
        "matched": matched,
        "verified": counts["verified"],
        "likely": counts["likely"],
        "not_found": counts["not_found"],
        "unverified": counts["unverified"],
        "match_rate": round(matched / checked, 4) if checked else 0.0,
        # 本轮因限流/缺 API key 没能用上的学术库（结果可能偏保守）
        "sources_unavailable": sorted(_DOWN_SOURCES),
    }


def _attention_lines(sections: dict, state: dict | None, limit: int = 12) -> list[str]:
    """给模型看的「自动核验没过」的条目：not_found / likely / unverified。"""
    by_id: dict[int, str] = {}
    for pos, entry in enumerate(_refs_of(sections), 1):
        by_id[_entry_ref_id(entry, pos)] = str(entry.get("text") or "")
    lines: list[str] = []
    for rid, ev in sorted(((state or {}).get("references") or {}).items()):
        status = str((ev or {}).get("status") or "unverified")
        if status == "verified":
            continue
        lines.append(f"[{rid}] {status} — {_norm_text(by_id.get(rid), 220)}")
        if len(lines) >= limit:
            break
    return lines


def _verify_all_references(sections: dict, state: dict, *,
                           workers: int = AUTO_VERIFY_WORKERS,
                           deadline: float = AUTO_VERIFY_DEADLINE,
                           cfg: dict | None = None) -> dict:
    """把**整张参考文献列表**逐条送去核验（不让模型决定查哪几条）。

    每条先按 DOI / arXiv 精确查，查不到再用「标题 + 作者 + 年份」去
    Crossref / OpenAlex / arXiv / Semantic Scholar 分级检索 —— 都是
    `_verify_reference` 里现成的逻辑，这里只负责「全部跑一遍 + 并发 + 去重 + 超时兜底」。
    结果写进 `state["references"]`（与 Agent 后续 `verify_reference` 共用一张
    证据表，状态取「更确定」的那个），并返回统计字典。

    `cfg`：当前账号的学术库配置（Key / 邮箱），不传就回落环境变量 / `.env`。
    """
    entries = _refs_of(sections)[:MAX_AUTO_VERIFY]
    _DOWN_SOURCES.clear()                     # 每轮导出重新试一次，上一轮的熔断不带过来
    # 同一篇文献被列了两遍时只查一次（按去标点后的文本去重）
    todo: list[tuple[int, str]] = []
    seen: dict[str, int] = {}
    dup: list[tuple[int, int]] = []
    for pos, entry in enumerate(entries, 1):
        rid = _entry_ref_id(entry, pos)
        raw = str(entry.get("text") or "")
        key = _title_key(raw)[:240]
        if key and key in seen:
            dup.append((rid, seen[key]))
            continue
        if key:
            seen[key] = rid
        todo.append((rid, raw))

    def work(item: tuple[int, str]) -> tuple[int, dict]:
        rid, raw = item
        try:
            return rid, _verify_reference(
                rid, title=_guess_title(raw), authors=_guess_authors(raw),
                year=_find_year(raw), doi=_find_doi(raw), arxiv=_find_arxiv(raw),
                query=raw, cfg=cfg)
        except Exception as exc:                  # 单条失败不影响整批
            return rid, _unverified_result(rid, f"核验失败：{exc}"[:200])

    def record(rid: int, result: dict) -> None:
        prev = state["references"].get(rid)
        if not prev or _status_rank(result.get("status")) >= _status_rank(
                prev.get("status")):
            state["references"][rid] = result

    started = time.time()
    budget = max(1.0, float(deadline))
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
            futures = {pool.submit(work, item): item[0] for item in todo}
            done, pending = wait(futures, timeout=budget * 0.7)
            for future in done:
                rid, result = future.result()
                record(rid, result)
            for future in pending:                # 超时：剩下的先标 unverified
                future.cancel()
                record(futures[future], _unverified_result(
                    futures[future], "核验超时，未能完成检索"))

        # 第二轮：被限流（429）或超时的条目串行补查 —— 并发降下来后成功率高很多，
        # 否则一场限流就会把真实存在的文献误记成「没查到」。
        for rid, raw in todo:
            ev = state["references"].get(rid) or {}
            if str(ev.get("status") or "") not in ("not_found", "unverified"):
                continue
            if not ev.get("errors"):
                continue
            if time.time() - started >= budget:
                break
            new_rid, result = work((rid, raw))
            record(new_rid, result)
            time.sleep(0.4)
    for rid, source in dup:
        base = state["references"].get(source)
        if isinstance(base, dict):
            clone = dict(base)
            clone["reference_id"] = rid
            clone["duplicate_of"] = source
            record(rid, clone)
    return _reference_stats(sections, state)


def _ref_note(ev: dict) -> str:
    """证据 → 一句话说明（状态含义 + 对上的那条文献）。"""
    ev = ev if isinstance(ev, dict) else {}
    message = _s(ev.get("message"), 300)
    best = ev.get("best") if isinstance(ev.get("best"), dict) else {}
    bits: list[str] = []
    if best.get("title"):
        bits.append(_s(best.get("title"), 200))
    if best.get("year"):
        bits.append(str(best.get("year")))
    if best.get("doi"):
        bits.append("DOI " + _s(best.get("doi"), 120))
    if best.get("similarity"):
        bits.append(f"相似度 {best.get('similarity')}")
    joined = "；".join(b for b in bits if b)
    return _s(f"{message}（{joined}）" if joined else message, 800)


def _balanced_history(msgs: list[dict]) -> list[dict]:
    """兜底修复工具消息历史：`tool_calls` 与 `tool` 消息必须严格成对。

    OpenAI 兼容后端（含 DeepSeek）要求 assistant 消息里的**每一个** `tool_call_id`
    都紧跟一条对应的 tool 消息，少一条整轮请求就 400
    （`insufficient tool messages following tool_calls message`）。这里按对重排：

    * 没有 tool 消息回应的 tool_call 直接丢掉（若整条 assistant 因此变空、
      但还有正文，就退化成一条普通 assistant 消息）；
    * 找不到主人的 tool 消息丢掉。
    """
    out: list[dict] = []
    i, total = 0, len(msgs)
    while i < total:
        m = msgs[i]
        if not isinstance(m, dict):
            i += 1
            continue
        role = m.get("role")
        if role != "assistant" or not m.get("tool_calls"):
            if role != "tool":                    # 孤儿 tool 消息丢掉
                out.append(m)
            i += 1
            continue
        calls = [c for c in m.get("tool_calls") or []
                 if isinstance(c, dict) and str(c.get("id") or "").strip()]
        answered: dict[str, dict] = {}
        j = i + 1
        while j < total and isinstance(msgs[j], dict) and msgs[j].get("role") == "tool":
            cid = str(msgs[j].get("tool_call_id") or "").strip()
            if cid and cid not in answered:
                answered[cid] = msgs[j]
            j += 1
        kept = [c for c in calls if str(c.get("id")).strip() in answered]
        if kept:
            out.append({"role": "assistant", "content": m.get("content"),
                        "tool_calls": kept})
            out.extend(answered[str(c.get("id")).strip()] for c in kept)
        elif (m.get("content") or "").strip():
            out.append({"role": "assistant", "content": m.get("content")})
        i = j
    return out


def run_note_out_agent(llm: LlmClient, *, paper: dict, sections: dict,
                       notes: dict, instructions: str = "",
                       verify_all: bool | None = None,
                       scholar: dict | None = None) -> dict:
    """跑 Agent 工具循环，再生成结构化分析报告。

    `llm` 直接使用 `agents/llm.py` 的 `LlmClient`（不经过 `translate.Translator` 转发）。
    `paper` / `sections` / `notes` 都由 `note_out.py` 准备好（纯数据）；
    返回的 `report` 会直接放进导出 JSON。LLM 不可用时抛 `LlmUnavailable`。

    `verify_all` 控制「是否先把整张参考文献列表自动核验一遍」：默认读环境变量
    `NOTE_OUT_VERIFY_ALL`（缺省为开），测试/离线环境可以直接传 `False`。

    `scholar`：当前账号在「设置 → 学术检索」里配的学术库 Key（键名
    `OPENALEX_API_KEY` / `SEMANTIC_SCHOLAR_API_KEY` / `SCHOLARLY_MAILTO`），
    不传就用环境变量 / `.env`。
    """
    state: dict = {"references": {}, "code": None, "scholar": dict(scholar or {})}
    if verify_all is None:
        verify_all = _env_value("NOTE_OUT_VERIFY_ALL").lower() not in (
            "0", "false", "no", "off")
    if verify_all:
        # 全量核验放在进模型之前：既能让报告给出准确条数，
        # 也省掉「让模型自己挑哪几条去查」这一步（原本最多只能查 12 条）。
        _verify_all_references(sections, state, cfg=state["scholar"] or None)
    system = _AGENT_SYSTEM
    msgs: list[dict] = [{"role": "user",
                         "content": _agent_context(paper, sections, notes,
                                                   instructions, state)}]
    calls_made = 0
    for _ in range(MAX_TOOL_ROUNDS):
        message = llm.chat_completion(
            system, _balanced_history(msgs), tools=NOTE_OUT_TOOLS, temperature=0.3)
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not isinstance(calls, list) or not calls:
            break
        # 关键：写回历史的 assistant 消息里有几个 tool_call，就必须补几条 tool 消息。
        # 以前这里只处理 `calls[:4]`（超过 4 个并行的工具调用就漏回应），
        # 上游下一轮直接 400；现在先筛掉「无法回应」的调用（非对象 / 没有 ID /
        # 同一条消息里重复的 ID），再让 pending 与 tool 消息一一对应。
        pending: list[dict] = []
        seen_ids: set[str] = set()
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "").strip()
            if not call_id or call_id in seen_ids:   # 没有 ID 无法回应；重号只回应第一次
                continue
            seen_ids.add(call_id)
            pending.append({**call, "id": call_id})
        if not pending:
            break
        msgs.append({"role": "assistant", "content": message.get("content") or None,
                     "tool_calls": pending})
        for call in pending:                        # 不再截断，逐个回应
            call_id = call["id"]
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(fn.get("name") or "")
            if calls_made >= MAX_TOOL_CALLS:
                result = {"ok": False, "message": "本次导出的工具调用次数已达上限"}
            else:
                calls_made += 1
                try:
                    raw_args = fn.get("arguments") or "{}"
                    args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
                    if not isinstance(args, dict):
                        raise ValueError("工具参数必须是对象")
                    result = _execute_tool(name, args, paper=paper, sections=sections,
                                           notes=notes, state=state)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    result = {"ok": False, "message": f"工具参数无效：{exc}"}
            msgs.append({"role": "tool", "tool_call_id": call_id,
                         "content": json.dumps(result, ensure_ascii=False)})
        if calls_made >= MAX_TOOL_CALLS:
            break

    return _synthesize_report(llm, paper=paper, sections=sections,
                              notes=notes, state=state, instructions=instructions)


# =====================================================================
#  结构化报告
# =====================================================================
def _s(v: Any, limit: int) -> str:
    return " ".join(str(v or "").split())[:limit]


def _list(v: Any, limit: int, chars: int) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    out: list[str] = []
    for item in v:
        text = _s(item, chars)
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _notes_analysis(raw: Any) -> dict:
    data = raw if isinstance(raw, dict) else {}
    themes = []
    raw_themes = data.get("themes")
    if isinstance(raw_themes, dict):
        raw_themes = list(raw_themes.values())
    for t in (raw_themes or [])[:8] if isinstance(raw_themes, list) else []:
        if isinstance(t, dict):
            title = _s(t.get("title"), 120)
            points = _list(t.get("points"), 10, 500)
        else:
            title, points = _s(t, 120), []
        if title or points:
            themes.append({"title": title or "要点", "points": points})
    return {
        "overview": _s(data.get("overview"), 2000),
        "themes": themes,
        "connections": _list(data.get("connections"), 12, 500),
        "questions": _list(data.get("questions"), 12, 500),
        "next": _list(data.get("next"), 12, 500),
    }


def _paper_overview(raw: Any) -> dict:
    data = raw if isinstance(raw, dict) else {}
    return {
        "problem": _s(data.get("problem"), 1000),
        "method": _s(data.get("method"), 1500),
        "contributions": _list(data.get("contributions"), 10, 500),
        "findings": _list(data.get("findings"), 10, 500),
        "limitations": _list(data.get("limitations"), 8, 500),
    }


def _clean_ref_review(raw: Any, evidence: dict) -> list[dict]:
    rows: list[dict] = []
    seen: set[int] = set()
    ev_map = (evidence or {}).get("references") or {}
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            rid = int(row.get("id") or row.get("reference_id") or 0)
        except (TypeError, ValueError):
            continue
        if rid <= 0 or rid in seen:
            continue
        status = str(row.get("status") or "").strip().lower()
        ev = ev_map.get(rid) or {}
        ev_status = str(ev.get("status") or "").strip().lower()
        if ev_status in _STRONG_STATUS:
            # 状态以工具/自动核验为准（模型只能改写说明），保证与统计数字一致
            status = ev_status
        if status not in _STRONG_STATUS:
            continue
        seen.add(rid)
        rows.append({"id": rid, "status": status,
                     "note": _s(row.get("note"), 800)})
    # 证据里有、但模型没写的条目：按证据补上（状态以工具为准）
    for rid, ev in sorted(ev_map.items()):
        if rid in seen:
            continue
        rows.append({
            "id": rid,
            "status": ev.get("status") if ev.get("status") in _STRONG_STATUS else "unverified",
            "note": _ref_note(ev),
        })
    rows.sort(key=lambda r: r["id"])
    return rows


def _clean_code_review(raw: Any, evidence: dict) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        url = _clean_code_url(row.get("url") or "")
        if not _valid_code_url(url) or url.lower() in seen:
            continue
        seen.add(url.lower())
        rows.append({"url": url, "verified": bool(row.get("verified")),
                     "note": _s(row.get("note"), 500)})
    code_ev = (evidence or {}).get("code") or {}
    for cand in code_ev.get("candidates") or []:
        url = _clean_code_url(cand.get("url") or "")
        if not _valid_code_url(url) or url.lower() in seen:
            continue
        seen.add(url.lower())
        rows.append({
            "url": url,
            "verified": bool(cand.get("verified")),
            "note": _s(cand.get("note") or cand.get("description") or "", 500),
        })
    return rows[:10]


def _evidence_view(state: dict) -> dict:
    refs = []
    for rid, ev in sorted(((state or {}).get("references") or {}).items()):
        refs.append({
            "id": rid,
            "status": ev.get("status") or "unverified",
            "query": _s(ev.get("query"), 300),
            "best": ev.get("best"),
            "candidates": (ev.get("candidates") or [])[:MAX_TOOL_CANDIDATES],
            "errors": (ev.get("errors") or [])[:4],
        })
    code = (state or {}).get("code") or {}
    return {
        "references": refs,
        "code_search": {
            "message": _s(code.get("message"), 300),
            "candidates": (code.get("candidates") or [])[:MAX_TOOL_CANDIDATES],
            "errors": (code.get("errors") or [])[:4],
        },
    }


def _report_payload(paper: dict, sections: dict, notes: dict, state: dict,
                    instructions: str) -> str:
    refs = _refs_of(sections)
    ref_lines = [f"[{e.get('index') or i}] " + _norm_text(e.get("text"),
                                                          MAX_REF_TEXT_CHARS)
                 for i, e in enumerate(refs[:MAX_REFS], 1)]
    evidence = _evidence_view(state)
    # 全量核验后证据条数可能上百条：verified 的只留“对上的那一条”的摘要，
    # 没过的才保留候选与错误，避免把模型的上下文塞爆。
    compact_refs = []
    for row in evidence["references"]:
        if row.get("status") == "verified":
            best = row.get("best") or {}
            compact_refs.append({
                "id": row.get("id"), "status": "verified",
                "matched": _s(best.get("title"), 200),
                "year": best.get("year"),
                "doi": _s(best.get("doi"), 80),
            })
        else:
            compact_refs.append(row)
    payload = {
        "reference_stats": _reference_stats(sections, state),
        "paper": {
            "title": paper.get("title") or "",
            "num_pages": paper.get("num_pages") or 0,
            "parser": paper.get("parser") or "",
        },
        "sections": {
            "abstract": _norm_text((sections.get("abstract") or {}).get("text"),
                                   MAX_ABSTRACT_CHARS),
            "main_text": _norm_text((sections.get("main_text") or {}).get("text"),
                                    MAX_MAIN_CHARS),
            "appendix": _norm_text((sections.get("appendix") or {}).get("text"),
                                   MAX_APPENDIX_CHARS),
            "references": ref_lines,
        },
        "notes": _notes_text(notes),
        "evidence": {"references": compact_refs,
                     "code_search": evidence["code_search"]},
        "instructions": (instructions or "")[:2000],
    }
    return json.dumps(payload, ensure_ascii=False)


def _synthesize_report(llm: LlmClient, *, paper: dict, sections: dict,
                       notes: dict, state: dict, instructions: str) -> dict:
    raw = llm.chat_json(
        _REPORT_SYSTEM,
        _report_payload(paper, sections, notes, state, instructions),
        temperature=0.2)
    data = raw if isinstance(raw, dict) else {}
    report = {
        "summary": _s(data.get("summary"), 3000),
        "paper_overview": _paper_overview(data.get("paper_overview")),
        "notes_analysis": _notes_analysis(data.get("notes_analysis")),
        "references_stats": _reference_stats(sections, state),
        "references_review": _clean_ref_review(data.get("references_review"), state),
        "code_repositories": _clean_code_review(data.get("code_repositories"), state),
        "conclusion": _s(data.get("conclusion"), 2000),
        "next_steps": _list(data.get("next_steps"), 10, 500),
        "evidence": _evidence_view(state),
    }
    if not any((report["summary"], report["conclusion"],
                report["paper_overview"].get("method"),
                report["notes_analysis"].get("overview"),
                report["references_review"], report["code_repositories"])):
        raise LlmUnavailable("模型没有返回可用的导出报告，请重试")
    return report
