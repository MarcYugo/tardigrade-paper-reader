"""解析配置：后端选择、环境变量覆盖、`status()`。

见包文档 `backend/pdf_parser/__init__.py`。

「文字版式后端」当前有三个实现：

  * **本地解析方案**（`backend_pymupdf.py` + `local_parse_pipeline/`）：PyMuPDF
    文字层 + texify 公式识别 + OXR 版面分析（D-FINE）/ SLANet 表格识别 + 插图提取；后端取值
    `pymupdf` / `auto` 都对应这一条链路，前端逐行绝对定位渲染；
  * **Surya 2**（`../surya_parser.py` + `backend_surya.py`）：整页 OCR，文字 /
    公式 / 插图全部来自 Surya，前端按 `parser === "surya"` 独立渲染；
  * **Unlimited-OCR**（`../unlimited_ocr.py` + `backend_unlimited_ocr.py`）：整页
    OCR，文字 / 公式（LaTeX）/ 表格（HTML）/ 插图全部来自 Unlimited-OCR，
    前端按 `parser === "unlimited_ocr"` 独立渲染（与 Surya 那套各自维护）。
    数据来源有两条（前端渲染路径相同）：本地推理服务
    （`../unlimited_ocr.py`）与在线解析（`../unlimited_ocr_online.py`，百度智能云
    「文档解析（Unlimited-OCR）」）。**网页设置面板不新增项**（只改 `config.json`）：
    `unlimited_ocr_route`（默认 `auto` = 本地没配置就自动切在线，`local`/`online` 可强制）
    与在线凭据 `unlimited_ocr_online_api_key` / `unlimited_ocr_online_secret_key`
    （也可用 `UNLIMITED_OCR_ROUTE` / `UNLIMITED_OCR_ONLINE_API_KEY` /
    `UNLIMITED_OCR_ONLINE_SECRET_KEY` 环境变量覆盖）。

`status()` 的返回字段是对外契约（`/api/settings/parser` 与网页
「⚙ 设置 → 存储 / OCR」面板都在读）：`surya_*` 与
`unlimited_ocr_*` 是两套协议；`surya_ready` 与 `unlimited_ocr_ready` 都是**真探测**
（`GET /models`，超时 `PROBE_TIMEOUT`），`surya_client_ready` 是本机 surya-ocr
客户端依赖是否装好；两者（探测 + 客户端依赖）都就绪才能用 `surya` 后端解析。
旧 PaddleOCR 链路的配置项（`paddle_*`）**已随解析/渲染代码一起删除**，
不再被读取也不再识别（旧值 `backend="paddle"` 会走“未知后端”分支按 auto 处理）。

默认常量的家在各客户端模块：Surya 在 `../surya_parser.py`、Unlimited-OCR 在 `../unlimited_ocr.py` —— config.json 里
`parser` 段的键与这里一一对应，改动作时两边一起对齐。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from ..surya_parser import (
    DEFAULT_BACKEND as DEFAULT_SURYA_BACKEND,
    DEFAULT_IMAGE_LABELS,
    DEFAULT_URL as DEFAULT_SURYA_URL,
    PROBE_TIMEOUT as SURYA_PROBE_TIMEOUT,
    check_server as surya_check,
    client_ready as surya_client_ready)
from ..unlimited_ocr import (
    DEFAULT_MODEL as DEFAULT_UNLIMITED_OCR_MODEL,
    DEFAULT_TIMEOUT as DEFAULT_UNLIMITED_OCR_TIMEOUT,
    DEFAULT_URL as DEFAULT_UNLIMITED_OCR_URL,
    PROBE_TIMEOUT as UNLIMITED_OCR_PROBE_TIMEOUT,
    check_server as unlimited_ocr_check)
from ..unlimited_ocr_online import available as unlimited_ocr_online_available
from .backend_pymupdf import local_status as local_parse_status


# =====================================================================
#  解析配置(后端选择 / 服务参数)
# =====================================================================
# 仓库根的 `config.json`（与 `app.py` 的 `CONFIG_FILE` 是**同一个文件**）。
# ⚠️ 拆包时在这里踩过坑：原来单文件 `backend/pdf_parser.py` 上溯 2 层正好是仓库根，
#    拆成 `backend/pdf_parser/options.py` 后少了一层，指向了 `backend/config.json`
#    —— 那个文件根本不存在。后果：网页面板“切换解析后端”写的是根 `config.json`，
#    而解析时 `parse_pdf()` 读的是这个空路径（文件不存在就静默用默认值）→ 永远走
#    `auto`。而 `app.py` 的 `status(CONFIG_FILE)` 传的是根路径，所以面板里显示的
#    是“已保存的值”，实际解析却是另一回事 —— 表现就是“手动选后端不生效”。
#    改这里时顺手对一下 `app.py::CONFIG_FILE`（两者必须指向同一份）。
CONFIG_FILE = Path(__file__).resolve().parents[2] / "config.json"

# 合法的后端取值。`auto` / `pymupdf` 都走本地解析方案。
PARSER_MODES = ("auto", "surya", "unlimited_ocr", "pymupdf")

# ---- 两条整页 OCR 链的默认参数 ----
# 默认常量住在各自的客户端模块（导入见文件头）：
#   surya                   -> ../surya_parser.py（默认地址与 surya_doc_parse_service 端口一致）
#   unlimited_ocr           -> ../unlimited_ocr.py（默认地址与 unlimited_ocr_service 端口一致）

DEFAULT_OPTIONS: dict = {
    # auto / pymupdf → 本地解析方案（PyMuPDF + 本地模型）
    "backend": "auto",
    # ---- 本地解析方案（backend_pymupdf.py + local_parse_pipeline）----
    "local_device": "",              # 空/auto = 自动（cuda > mps > cpu）
    "local_dpi": 300,                 # 公式框裁剪渲染 dpi
    "local_padding": 2.0,             # 公式框外扩（pt）
    "local_temperature": 0.0,         # texify 解码温度（0 = 贪心）
    "local_max_tokens": 384,          # texify 单条公式最大 token
    "local_keep_crops": True,         # 公式裁图落盘（KaTeX 失败时的兜底）
    "local_min_figure_size": 24,      # 插图（检测框/矢量图）最小宽高（pt）
    "local_max_figures": 24,          # 每页最多保留的插图数
    "local_use_vector_figures": False,  # 关：插图只用 OXR 版面检出的 Picture 框；开：再加 get_drawings 矢量矩形
    "surya_url": DEFAULT_SURYA_URL,
    "surya_backend": DEFAULT_SURYA_BACKEND,
    "unlimited_ocr_url": DEFAULT_UNLIMITED_OCR_URL,        # 推理服务地址
    "unlimited_ocr_model": DEFAULT_UNLIMITED_OCR_MODEL,    # vLLM 的 served-model-name
    "unlimited_ocr_timeout": int(DEFAULT_UNLIMITED_OCR_TIMEOUT),  # 单页请求超时(秒)
    "unlimited_ocr_api_key": "",      # 服务端设了 --api-key 才需要（vLLM 默认不校验）
    # 数据来源路由：auto（本地没配置/没起来就自动切在线解析） / local / online。
    # 不上面板，只通过 config.json / 环境变量 UNLIMITED_OCR_ROUTE 配置。
    "unlimited_ocr_route": "auto",
    # 在线解析（百度智能云「文档解析（Unlimited-OCR）」）凭据：本地服务不可用时用它兜底。
    # 不上面板；空 = 在线链路不可用（status 会提示）。
    "unlimited_ocr_online_api_key": "",
    "unlimited_ocr_online_secret_key": "",
    "dpi": 150,                        # 页面渲染分辨率
    "image_dpi": 0,                    # 插图裁剪的独立渲染 dpi（0=与 dpi 相同；只影响裁图清晰度）
    "image_labels": list(DEFAULT_IMAGE_LABELS),
    "image_pad": 4,                    # 插图裁剪四周外扩像素
    "min_image_size": 32,              # 宽或高小于该值的插图丢弃
    "keep_html": True,                 # 把 block 的 html(表格/公式)写进 doc.json
    "fallback": True,                  # [已失效] 旧「OCR 失败回退 PyMuPDF」开关
    # 去重影：插图里已经“烘焙”了文字时，不再把同一批文字叠在图上渲染
    "drop_text_in_images": True,       # 关掉则保留全部文字行(可能叠字)
    "text_in_image_overlap": 0.6,      # 行框被插图覆盖超过该比例 → 判为图内文字
}

_ENV_KEYS = {
    "backend": "PDF_PARSER_BACKEND",
    "local_device": "LOCAL_PARSER_DEVICE",
    "local_dpi": "LOCAL_PARSER_DPI",
    "local_padding": "LOCAL_PARSER_PADDING",
    "local_temperature": "LOCAL_PARSER_TEMPERATURE",
    "local_max_tokens": "LOCAL_PARSER_MAX_TOKENS",
    "surya_url": "SURYA_INFERENCE_URL",
    "surya_backend": "SURYA_INFERENCE_BACKEND",
    "unlimited_ocr_url": "UNLIMITED_OCR_URL",
    "unlimited_ocr_model": "UNLIMITED_OCR_MODEL",
    "unlimited_ocr_timeout": "UNLIMITED_OCR_TIMEOUT",
    "unlimited_ocr_api_key": "UNLIMITED_OCR_API_KEY",
    "unlimited_ocr_route": "UNLIMITED_OCR_ROUTE",
    "unlimited_ocr_online_api_key": "UNLIMITED_OCR_ONLINE_API_KEY",
    "unlimited_ocr_online_secret_key": "UNLIMITED_OCR_ONLINE_SECRET_KEY",
    "dpi": "PDF_PARSER_DPI",
    "image_dpi": "PDF_PARSER_IMAGE_DPI",
    "image_labels": "PDF_PARSER_IMAGE_LABELS",
    "drop_text_in_images": "PDF_PARSER_DROP_TEXT_IN_IMAGES",
}

def _to_bool(val, default: bool = False) -> bool:
    """config.json / 环境变量里的 true/false/1/0/yes/no 统一成 bool。"""
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return bool(val)
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off", ""):
        return False
    return default


def _normalize_url(url: Any, default: str) -> str:
    """补全默认地址，并去掉结尾多余的 '/'。"""
    return (str(url or "").strip() or default).rstrip("/")


def _resolve_image_labels(spec: Any = None) -> set[str]:
    """None -> 默认图片类标签；'Picture,Figure' / ['Picture'] -> 集合"""
    if not spec:
        return set(DEFAULT_IMAGE_LABELS)
    if isinstance(spec, str):
        return {s.strip() for s in spec.split(",") if s.strip()}
    return {str(s).strip() for s in spec if str(s).strip()}


def load_options(config_file: Optional[Path] = None,
                 overrides: Optional[dict] = None,
                 backend: Optional[str] = None,
                 account_options: Optional[dict] = None) -> tuple[dict, list[str]]:
    """合并解析配置：默认值 < config.json < 账号设置 < 环境变量 < 显式参数。"""
    opts = dict(DEFAULT_OPTIONS)
    warnings: list[str] = []

    cfg_file = Path(config_file) if config_file else CONFIG_FILE
    if cfg_file.exists():
        try:
            raw = json.loads(cfg_file.read_text(encoding="utf-8"))
            section = raw.get("parser") or {}
            if not isinstance(section, dict):
                raise ValueError("parser 段应为对象")
            for key, val in section.items():
                if key in opts and val is not None:
                    opts[key] = val
            if "local_image_fallback" in section:
                warnings.append("解析配置项 local_image_fallback 已停用；"
                                "Figure 图片只使用解析器返回的裁图")
        except Exception as exc:
            warnings.append(f"读取解析配置 {cfg_file} 失败，使用默认值：{exc}")
    elif not os.environ.get(_ENV_KEYS["backend"]):
        # 没配置文件、又没环境变量覆盖 → 这次解析用的是内置默认值。以前这里完全静默，
        # 于是“手动选后端不生效”无从下手；现在跟着解析结果一起报出来(落在 doc.json
        # 的 warnings 里)。
        warnings.append(f"未找到解析配置 {cfg_file}，本次按内置默认值或账号设置解析")

    if account_options:
        opts.update({k: v for k, v in account_options.items()
                     if k in opts and v is not None})

    for key, env in _ENV_KEYS.items():
        val = os.environ.get(env)
        if val:
            opts[key] = val
    if overrides:
        opts.update({k: v for k, v in overrides.items() if v is not None and k in opts})
    if backend:
        opts["backend"] = backend

    mode = str(opts["backend"] or "").strip().lower()
    if mode not in PARSER_MODES:
        warnings.append(f"未知的解析后端 {opts['backend']!r}，已按 auto 处理")
        mode = "auto"
    opts["backend"] = mode
    try:
        opts["dpi"] = max(48, min(400, int(opts["dpi"])))
    except (TypeError, ValueError):
        opts["dpi"] = DEFAULT_OPTIONS["dpi"]
    try:
        # 插图裁剪分辨率：0 = 跟随 dpi；上限 600（render_pages 的渲染上限）
        opts["image_dpi"] = max(0, min(600, int(opts["image_dpi"])))
    except (TypeError, ValueError):
        opts["image_dpi"] = DEFAULT_OPTIONS["image_dpi"]
    for key in ("image_pad", "min_image_size"):
        try:
            opts[key] = int(opts[key])
        except (TypeError, ValueError):
            opts[key] = DEFAULT_OPTIONS[key]
    for key in ("keep_html", "fallback", "drop_text_in_images"):
        opts[key] = _to_bool(opts[key], DEFAULT_OPTIONS[key])
    try:
        opts["text_in_image_overlap"] = min(
            1.0, max(0.0, float(opts["text_in_image_overlap"])))
    except (TypeError, ValueError):
        opts["text_in_image_overlap"] = DEFAULT_OPTIONS["text_in_image_overlap"]
    opts["image_labels"] = _resolve_image_labels(opts["image_labels"])
    # ---- 本地解析方案 ----
    opts["local_device"] = str(opts.get("local_device") or "").strip().lower()
    if opts["local_device"] in ("auto", "none"):
        opts["local_device"] = ""
    try:
        opts["local_dpi"] = max(96, min(600, int(opts["local_dpi"])))
    except (TypeError, ValueError):
        opts["local_dpi"] = DEFAULT_OPTIONS["local_dpi"]
    try:
        opts["local_padding"] = max(0.0, min(24.0, float(opts["local_padding"])))
    except (TypeError, ValueError):
        opts["local_padding"] = DEFAULT_OPTIONS["local_padding"]
    try:
        opts["local_temperature"] = max(0.0, min(2.0, float(opts["local_temperature"])))
    except (TypeError, ValueError):
        opts["local_temperature"] = DEFAULT_OPTIONS["local_temperature"]
    try:
        opts["local_max_tokens"] = max(64, min(2048, int(opts["local_max_tokens"])))
    except (TypeError, ValueError):
        opts["local_max_tokens"] = DEFAULT_OPTIONS["local_max_tokens"]
    for key in ("local_keep_crops", "local_use_vector_figures"):
        opts[key] = _to_bool(opts[key], DEFAULT_OPTIONS[key])
    for key in ("local_min_figure_size", "local_max_figures"):
        try:
            opts[key] = max(1, int(opts[key]))
        except (TypeError, ValueError):
            opts[key] = DEFAULT_OPTIONS[key]
    opts["surya_url"] = _normalize_url(opts["surya_url"], DEFAULT_SURYA_URL)
    opts["surya_backend"] = (str(opts["surya_backend"] or "").strip().lower()
                             or DEFAULT_SURYA_BACKEND)
    opts["unlimited_ocr_url"] = _normalize_url(opts["unlimited_ocr_url"],
                                                DEFAULT_UNLIMITED_OCR_URL)
    opts["unlimited_ocr_model"] = (str(opts["unlimited_ocr_model"] or "").strip()
                                   or DEFAULT_UNLIMITED_OCR_MODEL)
    opts["unlimited_ocr_api_key"] = str(opts["unlimited_ocr_api_key"] or "").strip()
    key = "unlimited_ocr_timeout"
    try:
        opts[key] = max(30, int(opts[key]))
    except (TypeError, ValueError):
        opts[key] = DEFAULT_OPTIONS[key]
    # 本地/在线路由（默认 auto）；认不出的值按 auto，不报错
    route = str(opts.get("unlimited_ocr_route") or "").strip().lower()
    opts["unlimited_ocr_route"] = route if route in ("local", "online") else "auto"
    for key in ("unlimited_ocr_online_api_key", "unlimited_ocr_online_secret_key"):
        opts[key] = str(opts.get(key) or "").strip()
    return opts, warnings


def status(config_file: Optional[Path] = None,
           account_options: Optional[dict] = None) -> dict:
    """解析后端状态(供 /api/config 与 /api/settings/parser 展示)。

    返回字段是对外契约（前端 `common.js::renderParser` 在读）：
      * `effective` —— 实际生效的版式后端：`pymupdf`（本地解析方案）/ `surya` /
        `unlimited_ocr`；本机缺少本地模型依赖时本地方案为 `unavailable`；
      * `local_ready` / `local_message` —— 本地解析方案的依赖与模型是否就绪
        （不导入 torch/texify/paddle，只看模块可否找到与权重文件是否齐全）；
      * `surya_ready` 是**真探测**（`GET /models`）**且**本机 surya-ocr 客户端依赖
        就绪（`surya_client_ready`）—— 两者都满足才能用 surya 后端解析；
        `surya_service_ready` 单独给出服务端探测结果，`surya_message` 是明细。
    """
    try:
        opts, warnings = load_options(config_file, account_options=account_options)
    except Exception as exc:  # pragma: no cover - 配置损坏时不应影响服务
        return {"backend": "auto", "effective": "unavailable", "message": str(exc)}

    try:
        local = local_parse_status()
    except Exception as exc:  # 探测本身出错不影响其它后端的状态
        local = {"ready": False, "missing": [], "message": f"探测失败：{exc}",
                 "model_dir": ""}

    try:
        s_ok, s_msg = surya_check(opts["surya_url"], timeout=SURYA_PROBE_TIMEOUT)
    except Exception as exc:   # 同上：探测异常只是"不可用"
        s_ok, s_msg = False, f"探测失败：{exc}"
    try:
        c_ok, c_msg = surya_client_ready()
    except Exception as exc:
        c_ok, c_msg = False, f"客户端依赖探测失败：{exc}"
    try:
        u_ok, u_msg = unlimited_ocr_check(opts["unlimited_ocr_url"],
                                          opts["unlimited_ocr_model"],
                                          timeout=UNLIMITED_OCR_PROBE_TIMEOUT)
    except Exception as exc:   # 同上：探测异常只是"不可用"
        u_ok, u_msg = False, f"探测失败：{exc}"
    try:
        o_ok, o_msg = unlimited_ocr_online_available(opts)
    except Exception as exc:   # 凭据缺失 / 读环境变量出错都只是"在线不可用"
        o_ok, o_msg = False, f"在线解析不可用：{exc}"
    # 实际会用的数据来源（auto：本地就绪走本地，否则切在线）
    route_pref = opts["unlimited_ocr_route"]
    route_eff = (route_pref if route_pref in ("local", "online")
                 else ("local" if u_ok else "online"))

    backend = opts["backend"]
    if backend == "surya":
        if s_ok and c_ok:
            message = ("文字版式、公式（<math>→LaTeX）与插图由 Surya 2 整页 OCR 提供，"
                       "前端按 Surya 数据独立渲染")
        else:
            why = "推理服务不可用" if not s_ok else "本机缺少 surya-ocr 客户端依赖"
            message = f"解析后端现为 surya，但{why} —— 上传 / 重解析会失败（见下方明细）"
    elif backend == "unlimited_ocr":
        if opts["unlimited_ocr_route"] == "online":
            message = (("已强制使用在线解析（UNLIMITED_OCR_ROUTE=online）：百度智能云"
                        "「文档解析（Unlimited-OCR）」，按页计费；前端渲染路径不变")
                       if o_ok else
                       ("已强制使用在线解析，但在线凭据缺失 —— 上传 / 重解析会失败"
                        "（见下方明细）"))
        elif opts["unlimited_ocr_route"] == "local":
            message = (("文字、公式（LaTeX）、表格与插图由本地 Unlimited-OCR 推理服务提供"
                        "（已强制 local）；服务不可用时上传 / 重解析会失败")
                       if u_ok else
                       ("解析后端现为 unlimited_ocr，但已强制本地推理且服务不可用"
                        " —— 上传 / 重解析会失败（见下方明细）"))
        elif u_ok:
            message = ("文字、公式（LaTeX）、表格与插图由 Unlimited-OCR 整页 OCR 提供，"
                       "前端按 Unlimited-OCR 数据独立渲染")
        elif o_ok:
            message = ("本地 Unlimited-OCR 推理服务未配置 / 未就绪，解析会自动改用在线解析"
                       "（百度智能云「文档解析（Unlimited-OCR）」，按页计费）；"
                       "前端渲染路径不变")
        else:
            message = ("解析后端现为 unlimited_ocr，但本地推理服务与在线解析都不可用"
                       " —— 上传 / 重解析会失败（见下方明细）")
    else:
        if local["ready"]:
            message = ("本地解析方案：PyMuPDF 版式 + texify 公式识别 + "
                       "OXR 版面分析 + SLANet 表格识别；公式用页面覆盖层渲染")
        else:
            message = (f"解析后端现为 '{backend}'（本地解析方案），但本机还不具备条件："
                       + local["message"] + "；上传 / 重解析会失败（见下方明细）")
    effective = ("surya" if backend == "surya"
                 else "unlimited_ocr" if backend == "unlimited_ocr"
                 else "pymupdf" if local["ready"] else "unavailable")
    return {
        "backend": backend,              # 用户保存的解析后端
        # 实际生效的版式后端：pymupdf（本地解析方案）/ surya / unlimited_ocr
        "effective": effective,
        "local_ready": bool(local["ready"]),
        "local_message": local["message"],
        "local_missing": list(local.get("missing") or []),
        "local_model_dir": local.get("model_dir") or "",
        "env_override": bool(os.environ.get(_ENV_KEYS["backend"])),
        "surya_url": opts["surya_url"],
        "surya_backend": opts["surya_backend"],
        # 服务端 + 客户端都就绪才算"能用"；细分给 surya_service_ready / client_ready
        "surya_ready": bool(s_ok and c_ok),
        "surya_service_ready": bool(s_ok),
        "surya_client_ready": bool(c_ok),
        "surya_message": f"{s_msg}；{c_msg}",
        "unlimited_ocr_url": opts["unlimited_ocr_url"],
        "unlimited_ocr_model": opts["unlimited_ocr_model"],
        # 实际会用的数据来源：local = 本地推理服务 / online = 在线解析（设置不新增项）
        "unlimited_ocr_route": route_eff,
        # 纯 HTTP 服务：真探测（GET /models）通过就能用，没有本机客户端依赖
        "unlimited_ocr_ready": bool(u_ok),
        "unlimited_ocr_message": u_msg,
        # 在线解析（百度智能云）：本地不可用时的自动兜底；只判断凭据是否齐全
        "unlimited_ocr_online_ready": bool(o_ok),
        "unlimited_ocr_online_message": o_msg,
        "dpi": opts["dpi"],
        "image_dpi": opts["image_dpi"],
        "local_device": opts["local_device"] or "auto",
        "local_dpi": opts["local_dpi"],
        "message": message,
        "warnings": warnings,
    }
