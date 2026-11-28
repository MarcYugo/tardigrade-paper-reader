"""技能 · 通信桥（agent_comm_service）：消费 Redis Streams → 动作 → 回包。

外部科研 Agent 不直连本应用：请求先经 `agent_comm_service` 鉴权/检测后落到
**干净流** `outside-in-stream`（消费组 `outside-in-workers`），本技能就是这个流的
reader 侧实现 —— 取消息、按内容做动作、把结果写回 `inside-out-stream`，
由通信服务的 dispatcher 落到该请求专属回传流（`agent:reply:{rid}`）。

| 方向 | Redis | 字段 |
| --- | --- | --- |
| 收 | `outside-in-stream`（XREADGROUP + XACK） | `request_id` / `agent_id` / `envelope`(JSON) |
| 发 | `inside-out-stream`（XADD） | `request_id` / `kind`(ack·progress·final·error·cancelled) / `data`(JSON 字符串) / `trace_id` |

**能做哪些动作**（由 `payload.action` 或 `intent` 决定，见 `ACTION_BY_INTENT`）：
`export_notes`（完整导出）/ `list_notes`（笔记清单）/ `answer`（问答）/ `status`（自检）。

**账号从哪来（新信任模型）**：只看通信服务在信封上盖的章 ——
`envelope.owner` + `envelope.auth{via, key_id, from_agent, stamped_at, mac}`。
本技能**不再**自己解析 `ptk_`、不再看 `payload.user` / `AGENT_COMM_USER`、也不做
“单账号自动”：准入完全由通信服务在 HTTP 入口完成（校验账号 transfer key → 反查账号 →
盖章），reader 只消费结论。收到的消息必须：

* `owner` 与 `auth.owner` 一致、`auth.from_agent` 与 `from_agent` 一致；
* `mac = HMAC(stamp_secret, request_id|from_agent|owner|key_id|intent|stamped_at)` 正确且
  在 `AGENT_COMM_STAMP_WINDOW` 时间窗内（盖章密钥优先 `AGENT_COMM_STAMP_SECRET`，否则
  从同一个 MongoDB 的 `agent_gateway_meta` 取，与通信服务自动保持一致）；
* `payload` 已被网关归一化：没有 `transfer_key`，`payload.user` 只能是章上的 owner。

任何一项不满足都直接报错，**不降级、不猜账号**（防止有人绕过网关直接往 Redis 里塞消息）。
本地调试可设 `AGENT_COMM_ALLOW_UNSTAMPED=1` 回到旧行为（payload.transfer_key /
payload.user / AGENT_COMM_USER / 单账号自动），生产不要开。

**导出为什么一定完整**：通信服务对单条 `data` 有 64KB 上限（超了直接回
`payload_too_large`）。所以 `export_notes` 每次都把**完整导出**写进通信服务
Redis 的 `agent:result:{rid}`，消息里只放「摘要 + 引用」。完整导出包含：

* `fulltext`：整篇论文的**纯文本**（标题→首页信息→摘要→正文→参考文献→附录，
  逐段 `{section,id,page,kind,label,text}`），**不带 `x/y/w/h/bbox`**；
* `figures`：**配图** —— 页码 / 文件名 / 图注 / 字节数 / `data_url`
  （`data:image/png;base64,…`，可直接当图片用）；
* `code_links`：正文与笔记里提到的代码仓地址（确定性提取，不联网）；
* `reference_check`：参考文献**结构检查**（引用编号对账 / 缺失 / 未被引用 /
  重复 / DOI·arXiv·链接）；Agent 模式的联网核验写在 `report.references_stats`；
* `sections[*]`：原格式块（bbox / 公式区间 / 表格 HTML）—— 想看版式才用；
* `notes`：全部句子笔记 / 独立笔记 / 高亮；`report` / `ai_summary`。

payload 开关：`include_images`（默认 true）、`include_fulltext`（默认 true）、
`image_budget_mb`（配图 base64 总预算，默认取 `AGENT_COMM_RESULT_MAX_MB` 的一半）。
另外 `mode` 可选 `mechanical`（默认）/ `agent`（该账号配了 LLM 时走联网核验 +
分析报告，没配时按 `allow_fallback` 降级 —— 压测用 `payload.mode` 就能覆盖两条路径）。
科研 agent 拿到 `final` 后用 `GET /v1/requests/{rid}/result`（SDK
`fetch_result(request_id)`；`ask()` 默认自动取回）读完整内容 —— 不会再出现
「取出来的笔记不是全部」。

| 键 | 内容 | TTL |
| --- | --- | --- |
| `agent:result:{rid}` | 完整 JSON（>32KB 时 `gzip+base64`） | `AGENT_COMM_RESULT_TTL`（默认 3600s） |
| `agent:req:{rid}`（hash 字段） | `result_bytes` / `result_sha256` / `result_encoding` … | 同上 |

消息里只发「摘要 + 引用」：

```json
{"ok": true, "action": "export_notes", "doc_id": "...",
 "notes": {"stats": {...}, "preview": {...}},
 "fulltext": {"chars": 29845, "plain": true, "bbox": false,
              "sections": [{"name": "main_text", "pages": [2,3], "chars": 29845}],
              "preview": "标题…摘要…"},
 "figures": {"count": 12, "with_data": 12, "bytes": 810066,
             "items": [{"page": 2, "file": "p002_i000.png",
                        "caption": "Figure 1: …", "mime": "image/png"}]},
 "code_links": ["https://github.com/owner/repo"],
 "reference_check": {"ok": true, "count": 37, "cited": [1,2],
                     "missing": [], "uncited": [], "with_link": 6},
 "sections": {"main_text": {"pages": [2,3], "chars": 29845, "blocks": 124}},
 "report": {...},
 "truncated": true, "result_ref": "agent:result:req_x",
 "result_url": "/v1/requests/req_x/result", "result_bytes": 123456,
 "result_sha256": "..."}
```

科研 agent 拿到这条消息后，用通信服务的 `GET /v1/requests/{rid}/result`
（SDK：`fetch_result(request_id)`；`ask()` 默认自动取回并放进 `data`）取完整内容。

**前提**：本技能与 `agent_comm_service` 必须连**同一个 Redis**（`AGENT_COMM_REDIS_URL`
= 服务端 `REDIS_URL`），否则服务端读不到 `agent:result:{rid}`。`AGENT_COMM_RESULT_BLOB=0`
可退回「超限就裁剪」；`list_notes` / `answer` 仍是超过 `AGENT_COMM_INLINE_KB` 才落库。

**什么时候轮询**：`run_forever()` 每 `AGENT_COMM_PROBE_S`（默认 15s）探一次
`/healthz`，**服务不在就不取消息**（哪怕 Redis 有积压），回来自动恢复。一批消息由
线程池并发处理（`AGENT_COMM_WORKERS`，默认 8 —— 单条 `mode=agent` 导出 60~70s，
串行会排队到 300s 超时）。超过 `timeout_s + AGENT_COMM_STALE_AFTER_S` 的**过期消息
只 ACK**，不执行也不回包（否则陈年积压会拖住新请求、白烧 LLM 配额）。
`lifecycle.py` 在 FastAPI startup 调 `start_auto()` 托管，shutdown 停掉，不用手动跑。
同一篇论文的参考文献**核验缓存**（`refcheck` 集合）让重复导出只补查没结论的条目。

对外接口：`SKILL`（工具 `comm_status` / `comm_poll` / `comm_reply`）、
`CommConfig.from_env()`、`service_status()`、`CommWorker`（`poll_once` / `run_forever` /
`handle` / `send_reply` / `store_result`）、`CommSupervisor`、`start_auto()`、
`run_comm_agent()`、`main()`。

命令行用法（推荐用 `tools/comm_worker.py`，避免 `-m` 的 RuntimeWarning）：

```bash
python tools/comm_worker.py --status      # 只体检（服务 + 队列）
python tools/comm_worker.py --once        # 取一批消息处理完就退出
python tools/comm_worker.py               # 常驻轮询（Ctrl+C 停止）
python tools/comm_worker.py --inspect '{"intent":"paper.note.export","payload":{...}}'
```

依赖：`redis`（`pip install redis`，见 `requirements.txt`）。没有它时 `--status` 仍能
报告「服务在、Redis 客户端缺失」，不会抛栈。
"""
from __future__ import annotations

import argparse
import base64
import difflib
import gzip
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable

import httpx

from ... import records
from ...envfile import env_value
from ...notes_out import build_sections, export_paper as build_note_out_export
from ..agent import Skill, blocking_completion, run_tool_loop
from ..llm import LlmUnavailable

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
ACTIONS = ("export_notes", "list_notes", "answer", "status")
ACTION_BY_INTENT = {
    "paper.note.export": "export_notes",
    "paper.note.list": "list_notes",
    "paper.section.query": "answer",
    "paper.insight.validate": "answer",
}
REPLY_KINDS = ("ack", "progress", "final", "error", "cancelled")

# 通信服务的默认端口：`run_service.py` 本地跑是 8090，docker compose 映射到宿主 9500
DEFAULT_SERVICE_URLS = ("http://127.0.0.1:8090", "http://127.0.0.1:9500")

ANSWER_SYSTEM = (
    "你是论文阅读助手，只能依据用户提供的资料（论文摘要、笔记、AI 整理）回答，"
    "资料里没有的信息要直接说不知道，不要编造。回答用中文、简洁分点，"
    "引用笔记时保留原文措辞。"
)

# 通信服务 dispatcher 的硬上限是 64KB（MAX_INLINE_KB），这里留一点余量
DEFAULT_INLINE_KB = 60


# --------------------------------------------------------------------------- #
# 完整结果：与通信服务 `app/core/streams.py` 用同一套键名（同一个 Redis）
# --------------------------------------------------------------------------- #
def request_key(rid: str) -> str:
    return f"agent:req:{rid}"


def result_key(rid: str) -> str:
    return f"agent:result:{rid}"


class CommUnavailable(RuntimeError):
    """通信链路不可用（没装 redis / 连不上 / 服务没起）。"""


class CommActionError(ValueError):
    """消息内容不满足动作要求（论文不存在、缺参数等），以 `error` 回包。"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def _int_env(name: str, default: int) -> int:
    raw = env_value(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = (env_value(name) or "").lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _float_env(name: str, default: float) -> float:
    raw = env_value(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _str_env(name: str, default: str) -> str:
    return (env_value(name) or "").strip() or default


def mask_url(url: str) -> str:
    """`redis://user:pw@host` → `redis://***@host`（日志/状态里不泄露口令）。"""
    return re.sub(r"://[^@/\s]+@", "://***@", str(url or ""))


# --------------------------------------------------------------------------- #
# 网关盖章校验（与通信服务 `app/core/stamp.py` 的 slot / HMAC 完全一致）
# --------------------------------------------------------------------------- #
STAMP_META_ID = "auth_stamp"


def _meta_collection() -> str:
    return os.environ.get("AGENT_COMM_META_COLLECTION") or "agent_gateway_meta"


def _stamp_slot(request_id: Any, from_agent: Any, owner: Any, key_id: Any,
                intent: Any, stamped_at: Any) -> str:
    return "\n".join([str(request_id or ""), str(from_agent or ""), str(owner or ""),
                      str(key_id or ""), str(intent or ""), str(int(stamped_at or 0))])


def stamp_mac(secret: str, *, request_id: Any, from_agent: Any, owner: Any,
              key_id: Any, intent: Any, stamped_at: Any) -> str:
    slot = _stamp_slot(request_id, from_agent, owner, key_id, intent, stamped_at)
    return hmac.new(secret.encode(), slot.encode(), hashlib.sha256).hexdigest()


def reply_sig(secret: str, *, request_id: Any, kind: Any, owner: Any, data: str) -> str:
    """回包签名：与通信服务 `stamp.sign_reply()` 完全一致。

    slot = "reply" | request_id | kind | owner | sha256(data) —— 绑定到具体请求与正文，
    这样光能写 Redis 的人没法伪造/篡改 `final`。
    """
    body_hash = hashlib.sha256((data or "").encode()).hexdigest()
    slot = "\n".join(["reply", str(request_id or ""), str(kind or ""),
                      str(owner or ""), body_hash])
    return hmac.new(secret.encode(), slot.encode(), hashlib.sha256).hexdigest()


def verify_gateway_stamp(env: dict, secret: str,
                         window: int = 3600) -> tuple[str | None, str]:
    """校验通信服务的盖章，返回 `(owner, 失败原因)`；成功时原因为空字符串。

    这是 reader 侧**唯一**的账号来源：不再从 payload / 配置里"猜"归属。
    """
    env = env if isinstance(env, dict) else {}
    payload = env.get("payload") if isinstance(env.get("payload"), dict) else {}
    auth = env.get("auth")
    if not isinstance(auth, dict):
        return None, "unstamped"
    owner = str(env.get("owner") or "").strip()
    stamped_owner = str(auth.get("owner") or "").strip()
    if not owner or not stamped_owner or owner != stamped_owner:
        return None, "stamp_owner_mismatch"
    if str(auth.get("from_agent") or "") != str(env.get("from_agent") or ""):
        return None, "stamp_agent_mismatch"
    if not secret:
        return None, "no_stamp_secret"
    try:
        stamped_at = int(auth.get("stamped_at") or 0)
    except (TypeError, ValueError):
        return None, "stale_stamp"
    if not stamped_at or abs(int(time.time()) - stamped_at) > max(60, int(window)):
        return None, "stale_stamp"
    expected = stamp_mac(
        secret,
        request_id=env.get("request_id"),
        from_agent=auth.get("from_agent"),
        owner=stamped_owner,
        key_id=auth.get("key_id"),
        intent=env.get("intent"),
        stamped_at=stamped_at,
    )
    if not hmac.compare_digest(expected, str(auth.get("mac") or "")):
        return None, "bad_stamp_mac"
    # 网关会把 payload 归一化：明文 key 已抹掉、user 固定为归档账号
    if "transfer_key" in payload:
        return None, "payload_key_not_stripped"
    claimed = str(payload.get("user") or "").strip()
    if claimed and claimed != stamped_owner:
        return None, "payload_owner_mismatch"
    return stamped_owner, ""


@dataclass
class CommConfig:
    """通信桥的连接与行为参数（全部可用环境变量覆盖）。"""

    service_url: str = ""                 # AGENT_COMM_URL；空 = 依次探测 8090 / 9500
    redis_url: str = "redis://127.0.0.1:6379/0"
    stream_in: str = "outside-in-stream"
    stream_out: str = "inside-out-stream"
    group: str = "outside-in-workers"
    consumer: str = "paper-reader-agent"
    user: str = ""                        # 旧模型的兑底账号（仅 AGENT_COMM_ALLOW_UNSTAMPED=1 时生效）
    block_ms: int = 5000                  # XREADGROUP 阻塞时长
    batch: int = 8                        # 一次取几条（= 一批并发处理的条数）
    workers: int = 8                      # 同时处理几条（导出/回答都是网络等待，可并行）
    stale_after_s: int = 60               # 消息比调用方等待时间超过这么多秒 → 直接跳过
    probe_s: float = 15.0                 # 多久探一次通信服务（存在才轮询）
    inline_kb: int = DEFAULT_INLINE_KB    # 单条回包上限（dispatcher 是 64KB）
    result_blob: bool = True              # 超限时完整结果落 Redis，只回摘要 + 引用
    result_ttl: int = 3600                # 完整结果 TTL（与通信服务 RESULT_TTL 对齐）
    result_max_mb: int = 16               # 单条完整结果上限
    stream_maxlen: int = 10_000
    max_papers: int = 50                  # list_notes 最多扫多少篇
    max_notes: int = 200                  # list_notes 最多回多少条笔记
    # ---- 网关盖章（新信任模型）----
    # 密钥优先 AGENT_COMM_STAMP_SECRET，否则从同一个 MongoDB 的 agent_gateway_meta 读，
    # 与通信服务自动保持一致；两边不一致时 reader 会拒收（stamp 失败）。
    stamp_secret: str = ""                # AGENT_COMM_STAMP_SECRET
    stamp_window: int = 3600              # AGENT_COMM_STAMP_WINDOW：盖章有效期（秒）
    allow_unstamped: bool = False         # AGENT_COMM_ALLOW_UNSTAMPED：仅本地调试，回到旧行为

    @classmethod
    def from_env(cls) -> "CommConfig":
        """读环境变量 / `.env`（缺省值同上面的字段注释）。"""
        return cls(
            service_url=_str_env("AGENT_COMM_URL", ""),
            redis_url=_str_env("AGENT_COMM_REDIS_URL", "redis://127.0.0.1:6379/0"),
            stream_in=_str_env("AGENT_COMM_STREAM_IN", "outside-in-stream"),
            stream_out=_str_env("AGENT_COMM_STREAM_OUT", "inside-out-stream"),
            group=_str_env("AGENT_COMM_GROUP", "outside-in-workers"),
            consumer=_str_env("AGENT_COMM_CONSUMER", "paper-reader-agent"),
            user=_str_env("AGENT_COMM_USER", ""),
            block_ms=_int_env("AGENT_COMM_BLOCK_MS", 5000),
            batch=_int_env("AGENT_COMM_BATCH", 8),
            workers=_int_env("AGENT_COMM_WORKERS", 8),
            stale_after_s=_int_env("AGENT_COMM_STALE_AFTER_S", 60),
            probe_s=_float_env("AGENT_COMM_PROBE_S", 15.0),
            inline_kb=_int_env("AGENT_COMM_INLINE_KB", DEFAULT_INLINE_KB),
            result_blob=_bool_env("AGENT_COMM_RESULT_BLOB", True),
            result_ttl=_int_env("AGENT_COMM_RESULT_TTL", 3600),
            result_max_mb=_int_env("AGENT_COMM_RESULT_MAX_MB", 16),
            stream_maxlen=_int_env("AGENT_COMM_STREAM_MAXLEN", 10_000),
            max_papers=_int_env("AGENT_COMM_MAX_PAPERS", 50),
            max_notes=_int_env("AGENT_COMM_MAX_NOTES", 200),
            stamp_secret=_str_env("AGENT_COMM_STAMP_SECRET", ""),
            stamp_window=_int_env("AGENT_COMM_STAMP_WINDOW", 3600),
            allow_unstamped=_bool_env("AGENT_COMM_ALLOW_UNSTAMPED", False),
        )

    @property
    def inline_bytes(self) -> int:
        return max(8, int(self.inline_kb)) * 1024

    def service_candidates(self) -> list[str]:
        if self.service_url:
            return [self.service_url]
        return list(DEFAULT_SERVICE_URLS)


# --------------------------------------------------------------------------- #
# Redis / 服务探测
# --------------------------------------------------------------------------- #
def connect(cfg: CommConfig):
    """建 Redis 客户端（同步）。没装 `redis` 时抛出可读的 `CommUnavailable`。"""
    try:
        import redis  # 延迟导入：没装依赖时其他功能仍可用
    except ImportError as exc:                       # pragma: no cover - 环境相关
        raise CommUnavailable("缺少 redis 依赖，请先 pip install redis") from exc
    try:
        return redis.Redis.from_url(
            cfg.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=max(10, cfg.block_ms / 1000 * 2 + 5),
            health_check_interval=30,
        )
    except Exception as exc:
        raise CommUnavailable(f"Redis 连接参数无效：{exc}") from exc


def _probe_service(cfg: CommConfig) -> dict:
    """通信服务 `/healthz`：按候选地址依次试，第一个应答的算数。"""
    entry: dict[str, Any] = {}
    for url in cfg.service_candidates():
        try:
            resp = httpx.get(url.rstrip("/") + "/healthz", timeout=2.0)
        except Exception as exc:
            entry = {"url": url, "ok": False, "error": str(exc)}
            continue
        entry = {"url": url, "ok": resp.status_code == 200,
                 "status_code": resp.status_code}
        try:
            entry["health"] = resp.json()
        except Exception:
            entry["body"] = (resp.text or "")[:300]
        break
    return entry


def _probe_redis(cfg: CommConfig) -> dict:
    """Redis：PING + 两条流长度 + 消费组积压。"""
    conn = None
    try:
        conn = connect(cfg)
        conn.ping()
        info: dict[str, Any] = {"ok": True, "url": mask_url(cfg.redis_url)}
        for name in (cfg.stream_in, cfg.stream_out):
            try:
                info[name] = conn.xlen(name)
            except Exception:
                info[name] = 0
        try:
            group = next((g for g in conn.xinfo_groups(cfg.stream_in)
                          if str(g.get("name")) == cfg.group), None)
        except Exception:
            group = None
        if group:
            info["group"] = {"name": cfg.group, "pending": group.get("pending"),
                             "lag": group.get("lag"),
                             "consumers": group.get("consumers")}
        return info
    except Exception as exc:
        return {"ok": False, "url": mask_url(cfg.redis_url), "error": str(exc)}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def service_status(cfg: CommConfig | None = None) -> dict:
    """体检：通信服务 HTTP `/healthz` + Redis 连通性 / 流长度 / 消费组积压。

    任何一侧不可用都只写进结果（`ok` 表示「至少一侧可用」），不抛异常 ——
    这个函数要能在前端 / 工具里随便调。
    """
    cfg = cfg or CommConfig.from_env()
    service, redis = _probe_service(cfg), _probe_redis(cfg)
    return {
        "ok": bool(service.get("ok") or redis.get("ok")),
        "config": {"redis_url": mask_url(cfg.redis_url),
                   "stream_in": cfg.stream_in,
                   "stream_out": cfg.stream_out,
                   "group": cfg.group,
                   "consumer": cfg.consumer,
                   "user": cfg.user or "(不参与准入)",
                   "owner_source": "legacy" if cfg.allow_unstamped else "gateway_stamp",
                   "stamp_window_s": cfg.stamp_window},
        "service": service,
        "redis": redis,
    }


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _load_json(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _size(obj: Any) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return 0


def _flag(payload: dict, key: str, default: bool = True) -> bool:
    """读 payload 里的布尔开关（兼容 `True/False` / `"1"` / `"false"` / `"no"`）。"""
    value = (payload or {}).get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _shrink_sections(data: dict) -> None:
    sections = data.get("sections")
    if isinstance(sections, dict):
        for sec in sections.values():
            if isinstance(sec, dict) and "text" in sec:
                sec["text"] = str(sec.get("text") or "")[:200]


def _shrink_notes(data: dict, keep: int) -> None:
    notes = data.get("notes")
    if isinstance(notes, dict):
        for key in ("sentence_notes", "free_notes", "highlights"):
            seq = notes.get(key)
            if isinstance(seq, list) and len(seq) > keep:
                notes[key] = seq[:keep]
    elif isinstance(notes, list) and len(notes) > keep:
        data["notes"] = notes[:keep]


def _drop(data: dict, key: str) -> None:
    data.pop(key, None)


def _drop_figure_data(data: dict) -> None:
    """去掉配图的 base64（只留文件 / 图注 / 字节数）—— 裁剪的最后手段。"""
    figures = data.get("figures")
    if isinstance(figures, dict):
        for item in figures.get("items") or []:
            if isinstance(item, dict):
                item.pop("data_url", None)
        figures["with_data"] = 0
        figures["trimmed"] = True


def _shrink_fulltext(data: dict, chars: int = 600) -> None:
    """把整篇纯文本截到 `chars` 个字符（段落列表也一起去掉）。"""
    fulltext = data.get("fulltext")
    if isinstance(fulltext, dict):
        text = str(fulltext.get("text") or "")
        if len(text) > chars:
            fulltext["text"] = text[:chars]
            fulltext["truncated"] = True
        fulltext.pop("paragraphs", None)


# 回包超限时按顺序试的裁剪步骤：从「去掉配图 base64」一路到「丢掉整块正文」，
# 每步之后重新量体积，够小就停。
_TRIM_STEPS: tuple[Callable[[dict], None], ...] = (
    _drop_figure_data,
    partial(_shrink_fulltext, chars=4000),
    _shrink_sections,
    partial(_shrink_fulltext, chars=800),
    partial(_shrink_notes, keep=40),
    partial(_shrink_notes, keep=10),
    partial(_drop, key="notes"),
    partial(_drop, key="fulltext"),
    partial(_drop, key="figures"),
    partial(_drop, key="sections"),
    partial(_drop, key="report"),
    partial(_drop, key="ai_summary"),
)

# 摘要（slim）里原样保留的标量字段
_SLIM_KEYS = ("ok", "action", "owner", "doc_id", "format", "version",
              "exported_at", "mode", "mode_requested", "fallback",
              "question", "total")


def _section_index(sections: Any) -> dict[str, Any] | None:
    """分节目录：只留标题 / 页 / 字数 / 块数（正文在完整结果里）。"""
    if not isinstance(sections, dict):
        return None
    index: dict[str, Any] = {}
    for name, sec in sections.items():
        if not isinstance(sec, dict):
            continue
        text, blocks = sec.get("text"), sec.get("blocks")
        index[name] = {
            "title": sec.get("title") or name,
            "pages": sec.get("pages") or [],
            "chars": len(text) if isinstance(text, str) else int(sec.get("chars") or 0),
            "blocks": len(blocks) if isinstance(blocks, list) else int(blocks or 0),
        }
    return index


# --------------------------------------------------------------------------- #
# 通信桥 worker
# --------------------------------------------------------------------------- #
class CommWorker:
    """一个 reader 侧消费者：取消息 → 动作 → 回包 → ACK。

    `store`：`records.Store` / `MongoStore`；`auth`：`auth.AuthStore`（**不再**用于准入，
    仅在旧模式 / 账号存在性校验时用）；`llm_factory(username)`：返回该账号的 `LlmClient`。

    账号完全来自通信服务的盖章（`envelope.owner` + `envelope.auth.mac`）。
    """

    def __init__(self, *, store, cfg: CommConfig | None = None, auth=None,
                 settings=None, llm_factory: Callable[[str], Any] | None = None,
                 username: str = "", redis_client=None):
        self.store = store
        self.cfg = cfg or CommConfig.from_env()
        if username:
            self.cfg.user = username
        self.auth = auth
        self.settings = settings
        self.llm_factory = llm_factory
        self.consumer = self.cfg.consumer or f"paper-reader-agent-{os.getpid()}"
        self._redis = redis_client
        self._group_ready = False
        # 正在处理中的消息 id：读自身 pending 时跳过，避免同一条被重复处理
        self._inflight: set[str] = set()
        self._state_lock = threading.Lock()
        # 盖章密钥缓存：空 = 尚未找到；每 60s 重试一次（库里有之后自动生效）
        self._stamp_secret: str = ""
        self._stamp_checked_at: float = 0.0
        self._stamp_lock = threading.Lock()   # 多线程并发处理时避免重复/错开读库
        self._warned_unstamped = False

    # ---------------- Redis ----------------
    @property
    def r(self):
        if self._redis is None:
            self._redis = connect(self.cfg)
        return self._redis

    def _ensure_group(self) -> None:
        if self._group_ready:
            return
        with self._state_lock:
            if self._group_ready:
                return
            try:
                self.r.xgroup_create(self.cfg.stream_in, self.cfg.group,
                                     id="0", mkstream=True)
                log.info("已创建消费组 %s/%s", self.cfg.stream_in, self.cfg.group)
            except Exception as exc:                     # BUSYGROUP = 已存在
                if "BUSYGROUP" not in str(exc):
                    raise
            self._group_ready = True

    def _ack(self, msg_id: str) -> None:
        try:
            self.r.xack(self.cfg.stream_in, self.cfg.group, msg_id)
        except Exception as exc:                     # ACK 失败就留给 XAUTOCLAIM
            log.warning("ACK 失败 %s：%s", msg_id, exc)

    def _exists(self, key: str) -> bool:
        """`agent:cancel:{rid}` / `agent:waiter:{rid}` 这类标记位是否存在。"""
        try:
            return bool(self.r.exists(key))
        except Exception:
            return False

    # ---------------- 过期消息 ----------------
    @staticmethod
    def _message_age(msg_id: str) -> float | None:
        """流消息的年龄（秒）：Redis Stream ID 的前半段就是写入时的毫秒时间戳。"""
        try:
            return max(0.0, time.time() - int(str(msg_id).split("-", 1)[0]) / 1000.0)
        except (TypeError, ValueError):
            return None

    def _is_stale(self, msg_id: str, env: dict) -> bool:
        """消息已超过调用方的等待时间（`timeout_s`，默认 120s，+ 宽限），该直接跳过。

        调用方早已走了：认真跑完只会让新请求排在陈年积压后面，还白烧 LLM 配额。
        """
        age = self._message_age(msg_id)
        if age is None:
            return False
        try:
            timeout = float((env or {}).get("timeout_s") or 0)
        except (TypeError, ValueError):
            timeout = 0.0
        limit = (timeout if timeout > 0 else 120.0) + max(0, int(self.cfg.stale_after_s))
        return age > limit

    def status(self) -> dict:
        info = service_status(self.cfg)
        info["owner_source"] = "legacy" if self.cfg.allow_unstamped else "gateway_stamp"
        info["stamp_ready"] = bool(self.stamp_secret())
        return info

    # ---------------- 网关盖章 -------------
    def stamp_secret(self) -> str:
        """盖章密钥：`AGENT_COMM_STAMP_SECRET` 优先，否则读同一个 Mongo 的 meta 文档。"""
        if self.cfg.stamp_secret:
            return self.cfg.stamp_secret.strip()
        if self._stamp_secret:
            return self._stamp_secret
        with self._stamp_lock:
            if self._stamp_secret:                 # 另一个线程已读到
                return self._stamp_secret
            now = time.time()
            if self._stamp_checked_at and now - self._stamp_checked_at < 60:
                return ""                          # 上次没读到：60s 内不重复打库
            self._stamp_checked_at = now
            secret = self._read_mongo_stamp_secret()
            if secret:
                self._stamp_secret = secret
                log.info("已从 MongoDB 读取网关盖章密钥（collection=%s）", _meta_collection())
            return secret

    def _read_mongo_stamp_secret(self) -> str:
        """从与通信服务共用的 MongoDB 里取盖章密钥（与 `AUTH_STAMP_SECRET` 同一份）。"""
        for holder in (self.auth, self.store):
            if holder is None:
                continue
            backend = getattr(holder, "backend", holder)     # AuthStore.backend
            mongo = getattr(backend, "m", None)              # MongoAuthBackend.m
            if mongo is None:
                continue
            try:
                doc = mongo.db()[_meta_collection()].find_one({"_id": STAMP_META_ID}) or {}
                secret = str(doc.get("secret") or "").strip()
                if secret:
                    return secret
            except Exception as exc:                          # 库不可用：交给调用方报错
                log.debug("读取盖章密钥失败：%s", exc)
        return ""

    # ---------------- 回包 ----------------
    def send_reply(self, rid: str, kind: str, data: Any,
                   trace_id: str = "", owner: str = "") -> dict:
        """把一条回包写入 `inside-out-stream`（dispatcher 会落到 `agent:reply:{rid}`）。

        `owner` 是网关盖章的归属账号；带 `owner` 时会用盖章密钥签 `sig`，
        通信服务 dispatcher 验不过就进 DLQ（挡住伪造回包）。
        """
        rid = str(rid or "").strip()
        if not rid:
            raise CommActionError("缺少 request_id")
        kind = str(kind or "final").strip()
        if kind not in REPLY_KINDS:
            raise CommActionError(f"kind 只能是 {'/'.join(REPLY_KINDS)}")
        body = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False,
                                                             default=str)
        fields = {"request_id": rid, "kind": kind, "data": body}
        if owner:
            fields["owner"] = str(owner)[:64]
        secret = self.stamp_secret()
        if secret:
            fields["sig"] = reply_sig(secret, request_id=rid, kind=kind,
                                      owner=owner, data=body)
        if trace_id:
            fields["trace_id"] = str(trace_id)[:64]
        msg_id = self.r.xadd(self.cfg.stream_out, fields,
                             maxlen=self.cfg.stream_maxlen, approximate=True)
        return {"ok": True, "request_id": rid, "kind": kind,
                "message_id": str(msg_id), "bytes": len(body.encode("utf-8")),
                "signed": bool(secret)}

    # ---------------- 完整结果（通信服务 Redis） ----------------
    def store_result(self, rid: str, payload: dict) -> dict:
        """把完整结果写进通信服务 Redis（`agent:result:{rid}`），返回引用元信息。

        大结果用 `gzip+base64`：通信服务客户端是 `decode_responses=True`，不能写二进制。
        """
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        limit = max(1, int(self.cfg.result_max_mb)) * 1024 * 1024
        if len(raw) > limit:
            raise CommActionError(
                f"导出结果 {len(raw) / 1048576:.2f}MB 超过上限 "
                f"{self.cfg.result_max_mb}MB（AGENT_COMM_RESULT_MAX_MB）")
        ttl = max(60, int(self.cfg.result_ttl))
        if len(raw) >= 32 * 1024:
            blob = base64.b64encode(gzip.compress(raw, compresslevel=6)).decode("ascii")
            encoding = "gzip+base64"
        else:
            blob, encoding = raw.decode("utf-8"), "json"
        meta = {"key": result_key(rid), "url": f"/v1/requests/{rid}/result",
                "bytes": len(raw), "stored_bytes": len(blob.encode("utf-8")),
                "encoding": encoding, "sha256": hashlib.sha256(raw).hexdigest(),
                "ttl": ttl}
        req = request_key(rid)
        self.r.set(meta["key"], blob, ex=ttl)
        self.r.hset(req, mapping={"result_bytes": str(meta["bytes"]),
                                  "result_stored_bytes": str(meta["stored_bytes"]),
                                  "result_encoding": encoding,
                                  "result_sha256": meta["sha256"],
                                  "result_at": str(int(time.time())),
                                  "result_ttl": str(ttl)})
        try:                                    # 只延长请求 hash 的 TTL，别缩回去
            if int(self.r.ttl(req)) < ttl:
                self.r.expire(req, ttl)
        except Exception:
            pass
        return meta

    def _slim_result(self, payload: dict, meta: dict) -> dict:
        """超限回包的「摘要」：元数据 + 笔记 / 分节索引 + report，正文全部省略。"""
        slim: dict[str, Any] = {k: payload[k] for k in _SLIM_KEYS if k in payload}
        if isinstance(payload.get("paper"), dict):
            slim["paper"] = payload["paper"]
        notes = payload.get("notes")
        if isinstance(notes, dict):
            slim["notes"] = {"stats": notes.get("stats") or {},
                             "preview": {k: list(notes.get(k) or [])[:3] for k in
                                         ("sentence_notes", "free_notes", "highlights")}}
        elif isinstance(notes, list):
            slim["notes"] = notes[:3]
        sections = _section_index(payload.get("sections"))
        if sections is not None:
            slim["sections"] = sections

        for key in ("report", "ai_summary", "sources", "answer", "papers"):
            if payload.get(key) is not None and _size(payload[key]) <= 12 * 1024:
                slim[key] = payload[key]
        # 整篇纯文本只给字数 / 目录 / 开头预览；配图只给文件 / 图注 / 字节数
        fulltext = payload.get("fulltext")
        if isinstance(fulltext, dict):
            body = str(fulltext.get("text") or "")
            slim["fulltext"] = {"chars": fulltext.get("chars") or len(body),
                                "plain": True, "bbox": False,
                                "truncated": bool(fulltext.get("truncated")),
                                "sections": fulltext.get("sections") or [],
                                "paragraphs": len(fulltext.get("paragraphs") or []),
                                "preview": body[:600]}
        figures = payload.get("figures")
        if isinstance(figures, dict):
            items = [{k: item.get(k) for k in
                      ("page", "file", "kind", "bytes", "mime", "caption")
                      if item.get(k) is not None}
                     for item in figures.get("items") or [] if isinstance(item, dict)]
            slim["figures"] = {"count": figures.get("count") or len(items),
                               "with_data": figures.get("with_data") or 0,
                               "bytes": figures.get("bytes") or 0,
                               "items": items[:20],
                               "skipped": figures.get("skipped") or []}

        for key in ("code_links", "reference_check"):
            value = payload.get(key)
            if value is not None and _size(value) <= 16 * 1024:
                slim[key] = value
        slim.update({
            "truncated": True,
            "trimmed": True,
            "result_ref": meta["key"],
            "result_url": meta["url"],
            "result_bytes": meta["bytes"],
            "result_sha256": meta["sha256"],
            "result_encoding": meta["encoding"],
            "hint": (f"完整结果已存入通信服务 Redis（{meta['key']}，TTL {meta['ttl']}s）；"
                     f"请 GET {meta['url']}?purge=true（SDK: fetch_result(request_id, "
                     f"purge=True)）取回完整论文文本 / 配图 base64 / 全部笔记，"
                     f"取完即删、不必等 TTL。"),
        })
        return slim

    def _share_oversized(self, rid: str, payload: dict,
                         full: dict | None = None) -> dict:
        """把完整结果落 Redis，消息只带回「摘要 + 引用」。

        传了 `full`（`export_notes` 的完整导出）就**总是**落库；否则超 `inline_kb` 才落。
        """
        if not self.cfg.result_blob:
            return payload
        if full is None and _size(payload) <= self.cfg.inline_bytes:
            return payload
        target = payload if full is None else full
        try:
            meta = self.store_result(rid, target)
        except CommActionError:
            raise
        except Exception as exc:                     # Redis 写失败：退回裁剪（有损）
            log.warning("完整结果写入 Redis 失败 rid=%s：%s；回退为裁剪回包", rid, exc)
            return payload
        log.info("结果 %d 字节（完整 %d 字节）已存 %s",
                 _size(payload), meta["bytes"], meta["key"])
        return self._slim_result(payload, meta)

    # ---------------- 消费 ----------------
    def _take_entries(self, n: int, block: int) -> list[tuple[str, dict]]:
        """读一批消息：先补读本消费者未 ACK 的 pending，再读新消息。

        跳过 `_inflight` 里的消息 —— `id="0"` 也会把正在处理的新消息读出来。
        """
        with self._state_lock:
            inflight = set(self._inflight)
        entries: list[tuple[str, dict]] = []
        try:
            own = self.r.xreadgroup(self.cfg.group, self.consumer,
                                    {self.cfg.stream_in: "0"}, count=n)
            for _stream, batch in own or []:
                entries.extend((str(mid), fields or {}) for mid, fields in batch
                               if str(mid) not in inflight)
        except Exception as exc:                     # 读 pending 失败不影响读新消息
            log.debug("读取自身 pending 失败：%s", exc)
        if len(entries) < n:
            resp = self.r.xreadgroup(self.cfg.group, self.consumer,
                                     {self.cfg.stream_in: ">"},
                                     count=n - len(entries),
                                     block=(block if block > 0 else None))
            for _stream, batch in resp or []:
                entries.extend((str(mid), fields or {}) for mid, fields in batch)
        return entries

    def poll_once(self, limit: int | None = None,
                  block_ms: int | None = None) -> dict:
        """取一批消息并**并发**处理（`block_ms<=0` 不阻塞，方便测试 / 一次性跑）。

        单条 `mode=agent` 导出 60~70s，串行排队必然超时；并发度 `AGENT_COMM_WORKERS`。
        """
        self._ensure_group()
        n = max(1, int(limit or self.cfg.batch))
        block = self.cfg.block_ms if block_ms is None else int(block_ms)
        entries = self._take_entries(n, block)
        with self._state_lock:
            self._inflight.update(mid for mid, _fields in entries)

        def run(entry: tuple[str, dict]) -> dict:
            msg_id, fields = entry
            try:
                return self.handle_entry(msg_id, fields)
            except Exception as exc:                 # 单条失败不影响同批其它消息
                log.warning("处理消息失败 %s：%s（未 ACK，下轮重试）", msg_id, exc)
                return {"request_id": str(fields.get("request_id") or ""),
                        "message_id": msg_id, "ok": False, "error": str(exc)}
            finally:
                with self._state_lock:
                    self._inflight.discard(msg_id)

        workers = max(1, int(self.cfg.workers))
        if workers <= 1 or len(entries) <= 1:
            items = [run(entry) for entry in entries]
        else:
            with ThreadPoolExecutor(max_workers=min(workers, len(entries)),
                                    thread_name_prefix="comm-worker") as pool:
                futures = [pool.submit(run, entry) for entry in entries]
                items = [future.result() for future in futures]
        return {"ok": True, "count": len(items), "items": items}

    def handle_entry(self, msg_id: str, fields: dict) -> dict:
        """处理一条流消息：ack（进度）→ 动作 → final/error 回包 → XACK。"""
        rid = str(fields.get("request_id") or "").strip()
        env = _load_json(fields.get("envelope"))
        intent = str(env.get("intent") or "")
        summary: dict[str, Any] = {"request_id": rid, "intent": intent,
                                   "message_id": msg_id, "ok": False}
        if not rid:
            self._ack(msg_id)
            return {**summary, "error": "消息缺少 request_id"}
        # 调用方早已超时 / 主动取消：只 ACK，不执行也不回包
        skipped = ("expired" if self._is_stale(msg_id, env)
                   else "cancelled" if self._exists(f"agent:cancel:{rid}") else "")
        if skipped:
            log.info("跳过 %s 消息 rid=%s", skipped, rid)
            self._ack(msg_id)
            return {**summary, "ok": True, "skipped": skipped}

        trace_id = str(fields.get("trace_id") or env.get("trace_id") or "")
        payload_in = env.get("payload") if isinstance(env.get("payload"), dict) else {}
        try:
            owner = self._resolve_owner(env, payload_in)
        except CommActionError as exc:
            # 未盖章 / 验章失败：只回一条错误（owner 为空），不执行任何动作、不碰任何数据
            log.warning("拒收消息 rid=%s：%s", rid, exc)
            try:
                sent = self.send_reply(rid, "error",
                                       {"ok": False, "error": str(exc),
                                        "request_id": rid, "intent": intent},
                                       trace_id=trace_id)
            except Exception as send_exc:
                log.warning("错误回包失败 rid=%s：%s", rid, send_exc)
                raise
            self._ack(msg_id)
            summary.update(ok=False, kind="error", error=str(exc),
                           bytes=sent.get("bytes"))
            return summary

        try:                                    # 回包通道不通就别 ACK，等下轮重试
            self.send_reply(rid, "ack",
                            {"stage": "received", "reader": self.consumer,
                             "waiting": self._exists(f"agent:waiter:{rid}"),
                             "intent": intent, "owner": owner},
                            trace_id=trace_id, owner=owner)
        except Exception as exc:
            log.warning("ACK 回包失败 rid=%s：%s", rid, exc)
            raise
        kind, payload = self._run_action(rid, intent, env, fields, owner=owner)
        try:
            sent = self.send_reply(rid, kind, payload, trace_id=trace_id, owner=owner)
        except Exception as exc:
            log.warning("结果回包失败 rid=%s：%s", rid, exc)
            raise
        self._ack(msg_id)
        summary.update(ok=bool(payload.get("ok")), kind=kind,
                       action=payload.get("action"), bytes=sent.get("bytes"))
        if payload.get("result_ref"):
            summary["result_ref"] = payload["result_ref"]
            summary["result_bytes"] = payload.get("result_bytes")
        return summary

    def _run_action(self, rid: str, intent: str, env: dict,
                    fields: dict, *, owner: str = "") -> tuple[str, dict]:
        """执行动作 → `(kind, 已经压到单条上限以内的回包内容)`。"""
        kind = "final"
        try:
            result = self.handle(env, fields, owner=owner or None)
        except CommActionError as exc:
            result, kind = {"ok": False, "error": str(exc)}, "error"
        except Exception as exc:                     # 兜底：不让单条消息炸掉消费者
            log.exception("处理消息失败 rid=%s", rid)
            result, kind = {"ok": False, "error": f"内部错误：{exc}"}, "error"

        payload = dict(result or {})
        full = payload.pop("_full", None)            # export_notes 的完整导出（不随消息发送）
        payload.setdefault("ok", kind == "final")
        payload.setdefault("request_id", rid)
        payload.setdefault("intent", intent)
        # 超过单条上限（或动作要求完整可得）：完整结果落通信服务 Redis，消息只带摘要 + 引用
        try:
            payload = self._share_oversized(rid, payload, full)
        except CommActionError as exc:               # 结果太大 / 参数问题：以 error 回包
            payload, kind = {"ok": False, "error": str(exc)}, "error"
        return kind, self._fit(payload)

    # ---------------- 动作分发 ----------------
    def handle(self, env: dict, fields: dict | None = None, *,
               owner: str | None = None) -> dict:
        """按 `payload.action` / `intent` 执行动作，返回要回包的内容。"""
        env = env if isinstance(env, dict) else {}
        payload = env.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if owner is None:
            owner = self._resolve_owner(env, payload)
        action = self._action_of(env, payload)
        if action == "status":
            return {"ok": True, "action": "status", "owner": owner,
                    "status": self.status()}
        if action == "export_notes":
            return self._export_notes(owner, payload)
        if action == "list_notes":
            return self._list_notes(owner, payload)
        return self._answer(owner, payload)

    @staticmethod
    def _action_of(env: dict, payload: dict) -> str:
        action = str(payload.get("action") or "").strip().lower()
        if action in ACTIONS:
            return action
        return ACTION_BY_INTENT.get(str(env.get("intent") or "").strip(), "answer")

    # ---------------- 账号 / 论文 ----------------
    def _resolve_owner(self, env: dict, payload: dict) -> str:
        """归属账号：**只读通信服务的盖章**，不自报、不兑底、不猜。

        `AGENT_COMM_ALLOW_UNSTAMPED=1`（仅本地调试）才回到旧行为，见 `_legacy_owner`。
        """
        owner, why = verify_gateway_stamp(env, self.stamp_secret(),
                                         max(60, int(self.cfg.stamp_window)))
        if owner:
            return owner
        if not self.cfg.allow_unstamped:
            if not self._warned_unstamped and why in ("unstamped", "no_stamp_secret"):
                self._warned_unstamped = True
                log.warning("拒收未盖章/无法验章的请求（%s）：准入只由通信服务负责；"
                            "本地调试可设 AGENT_COMM_ALLOW_UNSTAMPED=1", why)
            raise CommActionError(
                f"请求未通过通信服务盖章（{why}）：reader 只按网关校验的账号取数据，"
                "不再自行推断归属")
        return self._legacy_owner(env, payload)

    def _legacy_owner(self, env: dict, payload: dict) -> str:
        """旧模型的账号推断（`ptk_` / payload.user / AGENT_COMM_USER / 单账号自动）。

        仅供 `AGENT_COMM_ALLOW_UNSTAMPED=1` 的本地调试使用；生产路径禁止走这里。
        """
        key = str(payload.get("transfer_key") or env.get("transfer_key") or "").strip()
        if key.startswith("ptk_"):
            if not self.auth:
                raise CommActionError("未装配账号存储，无法校验 transfer key")
            who = self.auth.resolve_transfer_key(key)
            if not who:
                raise CommActionError("transfer key 无效或已被删除")
            return who["username"]
        name = str(payload.get("user") or payload.get("username")
                   or self.cfg.user or "").strip()
        if not name and self.auth:
            users = self.auth.list_users()
            if len(users) != 1:
                raise CommActionError(
                    f"无法确定账号：当前有 {len(users)} 个账号，"
                    "请在 payload 里带 user 或 ptk_ 转交 Key")
            name = users[0]["username"]
        if not name:
            raise CommActionError("无法确定账号：服务未装配账号存储")
        pub = self.auth.public_user(name) if self.auth else None
        if self.auth and not pub:
            raise CommActionError(f"账号不存在：{name}")
        return pub["username"] if pub else name

    def _papers(self, owner: str) -> list[dict]:
        rows: list[dict] = []
        for rec in self.store.list_for_user(owner):
            if records.perm_of(rec, owner):
                rows.append(rec)
        return rows

    def _resolve_paper(self, owner: str, payload: dict) -> dict:
        """按 `doc_id` / `paper_id` / `title` 定位论文；定位不到抛 `CommActionError`。"""
        doc_id = str(payload.get("doc_id") or payload.get("paper_id") or "").strip()
        if doc_id:
            rec = self.store.get_record(doc_id)
            if not rec or not records.perm_of(rec, owner):
                raise CommActionError(f"论文不存在或无权访问：{doc_id}")
            return rec

        papers = self._papers(owner)
        title = str(payload.get("title") or payload.get("paper") or "").strip()
        if title:
            return self._match_title(papers, title)
        if len(papers) == 1:
            return papers[0]
        if not papers:
            raise CommActionError("该账号还没有论文")
        preview = "、".join(str(r.get("title") or r.get("id")) for r in papers[:5])
        raise CommActionError(f"请提供 doc_id 或 title；可选：{preview}")

    @classmethod
    def _match_title(cls, papers: list[dict], title: str) -> dict:
        """按标题找论文：精确 → 模糊 → 包含，都不中抛 `CommActionError`。"""
        named = {cls._paper_title(r): r for r in papers}
        exact = [r for name, r in named.items()
                 if name.strip().lower() == title.lower()]
        if len(exact) == 1:
            return exact[0]
        close = difflib.get_close_matches(title, list(named), n=1, cutoff=0.45)
        if close:
            return named[close[0]]
        contain = [r for name, r in named.items() if title.lower() in name.lower()]
        if len(contain) == 1:
            return contain[0]
        preview = "、".join(list(named)[:5]) or "（无）"
        raise CommActionError(f"没找到标题匹配「{title}」的论文；可选：{preview}")

    @staticmethod
    def _paper_title(rec: dict) -> str:
        return str(rec.get("title") or rec.get("source") or rec.get("id") or "")

    # ---------------- 动作实现 ----------------
    def _export_notes(self, owner: str, payload: dict) -> dict:
        """导出某篇论文的笔记：消息里只给摘要，**完整 JSON 一定落 Redis**。

        完整导出 = `build_note_out_export()` 的原样结果，包含：五个分节的正文
        （`sections[*].text`）与逐段原格式块（`blocks`，含 bbox / 公式 / 表格 HTML）、
        **整篇纯文本**（`fulltext`，不含 bbox）、**配图 base64**（`figures[*].data_url`）、
        **代码仓链接**（`code_links`）、**参考文献检查**（`reference_check`）、
        全部句子笔记 / 独立笔记 / 高亮、机械报告、AI 整理。

        它通过 `_full` 交给 `handle_entry` 写进 `agent:result:{rid}`，不随消息发送；
        科研 agent 用 `GET /v1/requests/{rid}/result`（SDK `fetch_result`）取回，
        所以**不会再出现「取出来的笔记不是全部」**。

        payload 开关：

        * `mode`：`mechanical`（默认，纯本地）/ `agent`（用该账号的 LLM 做联网核验 +
          分析报告，慢且耗配额）；
        * `allow_fallback`（默认 true）：`mode=agent` 但该账号没配 LLM / LLM 调用失败时，
          降级为机械导出并在回包里标 `fallback`；设 false 则直接回 error；
        * `include_images`（默认 true）、`include_fulltext`（默认 true）、
          `image_budget_mb`（配图 base64 总预算，默认取 `AGENT_COMM_RESULT_MAX_MB` 的一半）；
        * `refresh_references`（默认 false）：参考文献核验结果默认**按论文缓存复用**
          （一次全量核验、之后只补查没结论的），传 true 强制重新联网核验。
        """
        rec = self._resolve_paper(owner, payload)
        doc_id = str(rec.get("id") or "")
        try:
            doc = self.store.read_doc(doc_id)
        except FileNotFoundError as exc:
            raise CommActionError(f"版式数据缺失：{doc_id}") from exc
        anno = self.store.get_annotations(doc_id)
        export, requested, fallback = self._run_export(
            owner, payload, rec=rec, doc=doc, anno=anno)

        # 私有字段 `_full`：由 handle_entry 写进 agent:result:{rid}，不随消息发送
        full = {**export, "ok": True, "action": "export_notes", "owner": owner,
                "doc_id": doc_id, "mode_requested": requested,
                "fallback": fallback or None}
        message = {
            "ok": True, "action": "export_notes", "owner": owner,
            "doc_id": doc_id, "mode_requested": requested,
            "mode": export.get("mode"), "format": export.get("format"),
            "version": export.get("version"),
            "exported_at": export.get("exported_at"),
            "paper": export.get("paper") or {},
            "notes": export.get("notes") or {},
            "report": export.get("report") or {},
            "ai_summary": export.get("ai_summary"),
            "sections": _section_index(export.get("sections")) or {},
            "fulltext": export.get("fulltext") or {},
            "figures": export.get("figures") or {},
            "code_links": export.get("code_links") or [],
            "reference_check": export.get("reference_check") or {},
            "hint": ("完整导出（整篇纯文本 + 配图 base64 + 原格式块 + 全部笔记 + "
                     "参考文献检查 + 报告）见 result_url："
                     "GET /v1/requests/{rid}/result 或 SDK fetch_result(request_id)。"),
            "_full": full,
        }
        if fallback:
            message["fallback"] = fallback
        return message

    def _run_export(self, owner: str, payload: dict, *, rec: dict, doc: dict,
                    anno: dict) -> tuple[dict, str, str]:
        """按 `payload.mode` 跑导出，返回 `(导出结果, 请求的 mode, fallback 原因)`。

        `mode=agent` 但账号没配 LLM / LLM 调用失败时按 `allow_fallback` 降级为
        mechanical（回包里标 `fallback`），设 false 则直接报错。
        """
        requested = str(payload.get("mode") or payload.get("export_mode")
                        or "mechanical").strip().lower()
        if requested not in ("mechanical", "agent"):
            raise CommActionError("mode 只能是 mechanical 或 agent")
        allow_fallback = _flag(payload, "allow_fallback", True)

        llm, fallback = None, ""
        if requested == "agent":
            llm = self._llm_for(owner)
            if llm is None:
                if not allow_fallback:
                    raise CommActionError(
                        f"agent 导出需要 LLM：账号 {owner} 没有可用的模型配置")
                fallback = "llm_unavailable"
                log.warning("账号 %s 没有可用 LLM，导出降级为 mechanical", owner)

        def build(mode: str, client=None) -> dict:
            return self._build_export(owner, payload, mode=mode, llm=client,
                                      rec=rec, doc=doc, anno=anno)

        started = time.monotonic()
        if fallback:
            return build("mechanical"), requested, fallback
        try:
            export = build(requested, llm)
        except LlmUnavailable as exc:
            if requested != "agent" or not allow_fallback:
                raise CommActionError(f"agent 导出失败：{exc}") from exc
            log.warning("agent 导出失败，降级为 mechanical：%s", exc)
            return build("mechanical"), requested, f"llm_error: {exc}"
        if requested == "agent":
            log.info("agent 导出完成 doc=%s 用时 %.1fs",
                     rec.get("id"), time.monotonic() - started)
        return export, requested, ""

    def _build_export(self, owner: str, payload: dict, *, mode: str, llm,
                      rec: dict, doc: dict, anno: dict) -> dict:
        """调 `build_note_out_export()`：分节正文 / 纯文本 / 配图 / 报告一次给全。"""
        scholar = None
        if mode == "agent" and self.settings is not None:
            try:
                scholar = self.settings.scholar_merged(owner)
            except Exception as exc:              # 学术库配置读不到不影响导出
                log.info("读取账号 %s 的学术库配置失败：%s", owner, exc)
        budget = payload.get("image_budget_mb")
        default_mb = max(1.0, self.cfg.result_max_mb * 0.5)
        try:
            budget_mb = float(budget) if budget else default_mb
        except (TypeError, ValueError):
            budget_mb = default_mb
        return build_note_out_export(
            doc=doc, rec=rec, anno=anno, mode=mode, llm=llm,
            username=owner,
            instructions=str(payload.get("instructions") or ""),
            scholar=scholar,
            store=self.store, doc_id=str(rec.get("id") or ""),
            with_fulltext=_flag(payload, "include_fulltext", True),
            with_images=_flag(payload, "include_images", True),
            image_budget_mb=budget_mb,
            image_max_mb=max(1.0, min(8.0, budget_mb)),
            # 参考文献核验结果按论文缓存：默认复用，`refresh_references=true` 强制重查
            refresh_references=_flag(payload, "refresh_references", False),
        )

    def _list_notes(self, owner: str, payload: dict) -> dict:
        """论文列表 + 阅读笔记清单（可限定某篇）。"""
        rec = None
        if any(payload.get(k) for k in ("doc_id", "paper_id", "title", "paper")):
            rec = self._resolve_paper(owner, payload)
        papers = [rec] if rec else self._papers(owner)[: max(1, self.cfg.max_papers)]
        rows: list[dict] = []
        items: list[dict] = []
        total = 0
        for r in papers:
            doc_id, title = str(r.get("id") or ""), self._paper_title(r)
            try:
                anno = self.store.get_annotations(doc_id)
            except Exception:
                anno = {}
            notes = list(anno.get("notes") or [])
            free = list(anno.get("free_notes") or [])
            total += len(notes) + len(free)
            rows.append({"doc_id": doc_id, "title": title,
                         "notes": len(notes) + len(free),
                         "highlights": len(anno.get("highlights") or {}),
                         "last_read_at": r.get("last_read_at")})
            for kind, seq in (("sentence", notes), ("free", free)):
                room = max(0, self.cfg.max_notes - len(items))
                items.extend(self._note_item(doc_id, title, kind, n)
                             for n in seq[:room])
        return {"ok": True, "action": "list_notes", "owner": owner,
                "papers": rows, "notes": items, "total": total,
                "truncated": total > len(items)}

    @staticmethod
    def _note_item(doc_id: str, paper: str, kind: str, note: dict) -> dict:
        """`list_notes` 里一条笔记的形状（句子笔记带 `block_id`，独立笔记带 `title`）。"""
        if kind == "sentence":
            return {"doc_id": doc_id, "paper": paper, "kind": kind,
                    "block_id": note.get("block_id"),
                    "text": str(note.get("text") or ""),
                    "by": note.get("by") or "",
                    "created_at": note.get("created_at")}
        return {"doc_id": doc_id, "paper": paper, "kind": kind,
                "title": note.get("title") or "",
                "text": str(note.get("text") or ""),
                "by": note.get("by") or "",
                "created_at": note.get("created_at")}

    def _llm_for(self, owner: str):
        if not self.llm_factory:
            return None
        try:
            llm = self.llm_factory(owner)
        except Exception as exc:
            log.warning("装配账号 %s 的 LLM 失败：%s", owner, exc)
            return None
        return llm if getattr(llm, "ready", False) else None

    def _build_context(self, owner: str, rec: dict | None,
                       limit_chars: int = 6000) -> tuple[str, list[dict], list[dict]]:
        """拼回答用的资料：论文摘要 / 正文节选（单篇时）+ 笔记 + AI 整理。"""
        papers = [rec] if rec else self._papers(owner)[:5]
        lines: list[str] = []
        entries: list[dict] = []

        def add(doc_id: str, title: str, kind: str, text: Any,
                prefix: str, limit: int = 0) -> None:
            """一段资料：进 prompt 的一行（可截断）+ 一条完整可引用条目。"""
            body = str(text or "").strip()
            if not body:
                return
            lines.append(prefix + (body[:limit] if limit else body))
            entries.append({"doc_id": doc_id, "title": title,
                            "kind": kind, "text": body})

        for r in papers:
            doc_id, title = str(r.get("id") or ""), self._paper_title(r)
            lines.append(f"## {title}")
            if rec is not None:                    # 只有指定了具体某一篇才带正文
                try:
                    sections = build_sections(self.store.read_doc(doc_id), title=title)
                except Exception as exc:
                    log.info("读取 %s 的版式失败（回答只用笔记）：%s", doc_id, exc)
                    sections = {}
                if sections:
                    add(doc_id, title, "摘要",
                        (sections.get("abstract") or {}).get("text"),
                        "摘要：", limit=1200)
                    main = str((sections.get("main_text") or {}).get("text") or "").strip()
                    add(doc_id, title, "正文", main[:1500], "正文节选：")
            try:
                anno = self.store.get_annotations(doc_id)
            except Exception:
                anno = {}
            for n in (anno.get("notes") or [])[:40]:
                add(doc_id, title, "笔记", n.get("text"), "- 笔记：")
            for n in (anno.get("free_notes") or [])[:20]:
                label = str(n.get("title") or "无标题")
                add(doc_id, title, "独立笔记", n.get("text"),
                    f"- 独立笔记（{label}）：")
            ai = r.get("ai")
            summary = ai.get("summary") if isinstance(ai, dict) else None
            if isinstance(summary, dict):
                add(doc_id, title, "AI 整理", summary.get("overview"),
                    "AI 整理：", limit=800)
        text = "\n".join(lines)
        if len(text) > limit_chars:
            text = text[:limit_chars] + "\n…（资料已截断）"
        return text, [{"doc_id": str(r.get("id") or ""),
                       "title": self._paper_title(r)} for r in papers], entries

    @staticmethod
    def _mechanical_answer(question: str, entries: list[dict],
                           sources: list[dict]) -> str:
        tokens = [t for t in re.split(r"[\s,，。;；:：!?！？()（）\[\]【】\"'“”]+",
                                      (question or "").lower())
                  if len(t) >= 2]
        scored: list[tuple[int, dict]] = []
        for entry in entries:
            low = str(entry.get("text") or "").lower()
            score = sum(low.count(t) for t in tokens) if tokens else 0
            if score:
                scored.append((score, entry))
        scored.sort(key=lambda pair: -pair[0])
        if scored:
            top = [f"- [{e['kind']}·{e['title']}] {str(e['text'])[:300]}"
                   for _, e in scored[:5]]
            return ("当前账号没有可用的 LLM，按关键词从笔记里找到这些相关内容：\n"
                    + "\n".join(top))
        if sources:
            return ("当前账号没有可用的 LLM，也没在笔记里找到与问题直接相关的内容。"
                    "涉及的论文：" + "、".join(s["title"] for s in sources[:5]))
        return "当前账号没有可用的 LLM，且还没有任何笔记可供检索。"

    def _answer(self, owner: str, payload: dict) -> dict:
        # `claim`：`paper.insight.validate` 的载荷（要核验/评价的一句论断），当成问题用
        question = str(payload.get("question") or payload.get("query")
                       or payload.get("claim") or payload.get("text") or "").strip()
        rec = None
        if any(payload.get(k) for k in ("doc_id", "paper_id", "title", "paper")):
            rec = self._resolve_paper(owner, payload)
        context, sources, entries = self._build_context(owner, rec)
        answer, mode = "", "mechanical"
        llm = self._llm_for(owner)
        if llm is not None:
            prompt = (f"问题：{question or '请概括这篇论文的核心内容与我的笔记要点'}\n\n"
                      f"资料：\n{context}")
            try:
                answer = str(llm.chat(ANSWER_SYSTEM,
                                      [{"role": "user", "content": prompt}],
                                      temperature=0.2) or "").strip()
                mode = "llm"
            except LlmUnavailable as exc:
                log.info("LLM 不可用，改用机械回答：%s", exc)
            except Exception as exc:
                log.warning("LLM 回答失败，改用机械回答：%s", exc)
        if not answer:
            answer = self._mechanical_answer(question, entries, sources)
            mode = "mechanical"
        return {"ok": True, "action": "answer", "owner": owner,
                "question": question, "answer": answer, "mode": mode,
                "sources": sources}

    # ---------------- 回包裁剪 ----------------
    def _fit(self, payload: dict) -> dict:
        """把结果压到 `inline_kb` 以内（dispatcher 超 64KB 会直接回错误）。"""
        try:
            data = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:
            return {"ok": False, "error": "结果无法序列化"}
        if _size(data) <= self.cfg.inline_bytes:
            return data
        data["trimmed"] = True
        for step in _TRIM_STEPS:
            step(data)
            if _size(data) <= self.cfg.inline_bytes:
                return data
        return {"ok": bool(data.get("ok", True)), "trimmed": True,
                "action": data.get("action"), "owner": data.get("owner"),
                "doc_id": data.get("doc_id"),
                "truncated": bool(data.get("truncated")),
                "result_ref": data.get("result_ref"),
                "result_url": data.get("result_url"),
                "result_bytes": data.get("result_bytes"),
                "result_sha256": data.get("result_sha256"),
                "error": f"结果超过通信服务单条上限（{self.cfg.inline_kb} KB）",
                "hint": data.get("hint") or "请缩小查询范围（指定 doc_id/title）或改用 paper API"}

    # ---------------- 常驻轮询 ----------------
    def _blocked_reason(self, state: dict, require_service: bool) -> str:
        """这一轮能不能消费：返回等待原因，空串表示服务与 Redis 都在。"""
        if require_service and not state["service"].get("ok"):
            return (f"通信服务未响应（{state['service'].get('url')}："
                    f"{state['service'].get('error') or '连接失败'}），暂停轮询")
        if not state["redis"].get("ok"):
            return f"Redis 不可用（{state['redis'].get('error')}），暂停轮询"
        return ""

    def run_forever(self, *, stop_event: threading.Event | None = None,
                    probe_interval: float | None = None,
                    require_service: bool = True) -> int:
        """常驻轮询：**通信服务在才消费**，不在就等着（每 `probe_interval` 秒重探）。

        判定顺序（每轮都重新探测，所以服务中途启动 / 挂掉都能自愈）：

        1. 通信服务 `/healthz` 不通 → 不取消息（即使 Redis 里有积压）；
        2. 服务在但 Redis 不通 → 等待；
        3. 两边都在 → 消费 `probe_interval` 秒，然后回到 1 重新探测。

        Ctrl+C、`stop_event` 置位或应用 shutdown 都会让循环退出（返回 0）。
        """
        stop = stop_event or threading.Event()
        interval = max(0.2, float(probe_interval if probe_interval is not None
                                   else self.cfg.probe_s))
        logged = ""           # 最近一次已打印的等待原因（同一原因只打一次，避免刷屏）
        while not stop.is_set():
            waiting = self._blocked_reason(self.status(), require_service)
            if waiting:
                if waiting != logged:
                    log.warning("%s；每 %ss 重试", waiting, interval)
                    logged = waiting
                stop.wait(interval)
                continue
            logged = ""

            if not getattr(self, "_announced", False):
                log.info("通信服务在线，开始消费 %s（group=%s consumer=%s，"
                         "每 %ss 重新探测）", self.cfg.stream_in, self.cfg.group,
                         self.consumer, interval)
                self._announced = True
            deadline = time.monotonic() + interval
            while not stop.is_set() and time.monotonic() < deadline:
                try:
                    result = self.poll_once(block_ms=min(self.cfg.block_ms, 2000))
                    if result["count"]:
                        log.info("已处理 %d 条消息", result["count"])
                except KeyboardInterrupt:
                    stop.set()
                    break
                except Exception as exc:          # Redis 抖动：稍等后重新探测
                    log.exception("消费失败：%s", exc)
                    stop.wait(2)
                    break
        log.info("通信桥已停止")
        return 0


# --------------------------------------------------------------------------- #
# 自动托管：应用启动时探测通信服务，存在才轮询
# --------------------------------------------------------------------------- #
class CommSupervisor:
    """把 `CommWorker.run_forever()` 放到**后台守护线程**（应用 startup 时用）。"""

    def __init__(self, worker: CommWorker, *, probe_interval: float | None = None):
        self.worker = worker
        self.probe_interval = probe_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> "CommSupervisor":
        if self.running:
            return self
        self._thread = threading.Thread(target=self._run, name="agent-comm",
                                        daemon=True)
        self._thread.start()
        self._started_at = time.time()
        return self

    def _run(self) -> None:
        try:
            self.worker.run_forever(stop_event=self._stop,
                                    probe_interval=self.probe_interval)
        except Exception:                     # 线程里不抛栈到解释器
            log.exception("通信桥线程异常退出")

    def stop(self, timeout: float = 5.0) -> bool:
        """让后台线程退出（`poll_once` 最多阻塞 block_ms，通常 1~2 秒内结束）。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        return not self.running

    def state(self) -> dict:
        cfg = self.worker.cfg
        return {"running": self.running, "consumer": self.worker.consumer,
                "stream_in": cfg.stream_in, "stream_out": cfg.stream_out,
                "group": cfg.group, "batch": cfg.batch, "workers": cfg.workers,
                "probe_interval_s": self.probe_interval or cfg.probe_s,
                "started_at": (time.strftime("%Y-%m-%dT%H:%M:%S",
                                             time.localtime(self._started_at))
                               if self._started_at else None)}


def start_auto(*, store, auth=None, settings=None,
               llm_factory: Callable[[str], Any] | None = None,
               cfg: CommConfig | None = None,
               probe_interval: float | None = None) -> CommSupervisor | None:
    """**应用启动时调用**：自动探测 agent_comm_service，存在才轮询。

    `AGENT_COMM_AUTO=0` 不启动；消费者名默认带进程号（`paper-reader-agent-<pid>`），
    多 worker / 重启不会互抢 pending；启动不阻塞、不抛异常。
    """
    cfg = cfg or CommConfig.from_env()
    if not _bool_env("AGENT_COMM_AUTO", True):
        log.info("AGENT_COMM_AUTO=0：通信桥不自动启动（可用 tools/comm_worker.py 手动跑）")
        return None
    worker = CommWorker(store=store, auth=auth, settings=settings,
                        llm_factory=llm_factory, cfg=cfg)
    if not env_value("AGENT_COMM_CONSUMER"):
        worker.consumer = f"{cfg.consumer}-{os.getpid()}"
    supervisor = CommSupervisor(worker, probe_interval=probe_interval).start()
    log.info("通信桥已启动（后台探测 %s，consumer=%s）",
             cfg.service_url or "/".join(cfg.service_candidates()), worker.consumer)
    return supervisor


# --------------------------------------------------------------------------- #
# 技能声明（给统一 Agent 运行时用：LLM 可主动体检 / 取消息 / 回包）
# --------------------------------------------------------------------------- #
def _tool(name: str, description: str, parameters: dict | None = None) -> dict:
    """一条 OpenAI 风格的 function 声明。"""
    return {"type": "function",
            "function": {"name": name, "description": description,
                         "parameters": parameters or {"type": "object",
                                                      "properties": {}}}}


COMM_TOOLS = [
    _tool("comm_status", "检查 agent_comm_service 是否在线、Redis 队列长度与积压，"
                         "以及当前消费组状态。"),
    _tool("comm_poll", "从 outside-in-stream 取一批待处理消息，按内容执行动作"
                       "（导出笔记 / 笔记列表 / 回答）并写回 inside-out-stream。",
          {"type": "object", "properties": {
              "limit": {"type": "integer", "minimum": 1, "maximum": 32,
                        "description": "本次最多取几条，默认 4"},
              "block_ms": {"type": "integer", "minimum": 0, "maximum": 30000,
                           "description": "阻塞时长（毫秒），0 表示不等待"}}}),
    _tool("comm_reply", "向某个 request_id 的回传流发送一条消息；"
                        "kind 只能是 ack / progress / final / error / cancelled。",
          {"type": "object",
           "properties": {"request_id": {"type": "string"},
                          "kind": {"type": "string", "enum": list(REPLY_KINDS)},
                          "data": {"type": "object",
                                   "description": "要回传的 JSON 内容"}},
           "required": ["request_id", "kind", "data"]}),
]

SKILL = Skill(
    name="agent_comm",
    title="通信桥（agent_comm_service）",
    description=("检查通信服务与 Redis 队列，取待处理消息并按内容回包："
                 "导出某篇论文的笔记 / 阅读笔记列表 / 返回回答。"),
    system_prompt=(
        "你在使用「通信桥」技能与 agent_comm_service 交互。"
        "先调用 comm_status 确认服务和队列可用；再用 comm_poll 取消息，"
        "工具本身会按消息内容执行动作并把结果写回通信服务（你不需要再手动 comm_reply，"
        "除非用户明确要求单独回一条消息）。"
        "若队列为空就直接说明；报错时把错误原文告诉用户并给出排查建议"
        "（服务是否启动、Redis 是否可连、payload 里是否带了 doc_id/title/user）。"
        "不要编造队列内容。"
    ),
    tools=tuple(COMM_TOOLS),
)


def _completion_with_text(llm) -> Callable[..., Any]:
    """`blocking_completion` + 把最终正文也当 delta 发出来。

    `run_tool_loop` 只累计 `delta`，而阻塞模式只产 `completion`，收尾那段会丢。
    """
    base = blocking_completion(llm)

    def complete(system: str, messages: list[dict], tools: list[dict],
                 temperature: float):
        for event in base(system, messages, tools, temperature):
            if event.get("type") == "completion":
                message = event.get("message") or {}
                text = message.get("content")
                if isinstance(text, str) and text.strip():
                    yield {"type": "delta", "text": text}
            yield event

    return complete


def run_comm_agent(*, worker: CommWorker, llm=None, prompt: str = "",
                   max_rounds: int = 4, max_calls: int = 8) -> dict:
    """让 LLM 用本技能跑一轮：体检 / 取消息 / 回包，返回 `{text, events}`。"""
    if llm is None:
        raise LlmUnavailable("通信桥技能需要一个可用的 LLM（用于编排工具调用）")

    def execute(name: str, args: dict) -> dict:
        if name == "comm_status":
            return worker.status()
        if name == "comm_poll":
            return worker.poll_once(limit=args.get("limit"),
                                    block_ms=args.get("block_ms"))
        if name == "comm_reply":
            return worker.send_reply(str(args.get("request_id") or ""),
                                     str(args.get("kind") or "final"),
                                     args.get("data") or {})
        return {"ok": False, "message": "不支持的工具"}

    events: list[dict] = []
    for event in run_tool_loop(
        complete=_completion_with_text(llm),
        system=SKILL.system_prompt,
        messages=[{"role": "user",
                   "content": prompt or "检查通信服务并处理待办消息"}],
        skill=SKILL,
        execute_tool=execute,
        max_rounds=max_rounds,
        max_calls=max_calls,
        max_per_round=2,
        temperature=0.2,
        final_reply=True,
    ):
        events.append(event)
    text = "".join(str(e.get("text") or "") for e in events
                   if e.get("type") == "delta")
    return {"ok": True, "text": text, "events": events}


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _build_worker(cfg: CommConfig) -> CommWorker:
    """装配存储 / 账号 / LLM（延迟 import，避免服务进程加载本模块时就连库）。"""
    root = Path(__file__).resolve().parents[3]
    from ...storage import build as build_storage
    from ..llm import LlmClient

    store, auth, settings, backend, _info = build_storage(root / "data",
                                                          root / "config.json")

    def llm_for(username: str):
        return LlmClient(config_file=settings.translator_config(username))

    log.info("通信桥存储后端：%s", backend)
    return CommWorker(store=store, auth=auth, settings=settings, cfg=cfg,
                      llm_factory=llm_for)


def _parse_args(cfg: CommConfig, argv: list[str] | None):
    ap = argparse.ArgumentParser(
        description="agent_comm_service 通信桥：消费 Redis Streams 并按内容回包")
    ap.add_argument("--status", action="store_true", help="只检查服务/队列状态")
    ap.add_argument("--once", action="store_true", help="只取一批消息处理完就退出")
    ap.add_argument("--limit", type=int, default=cfg.batch, help="一次取几条")
    ap.add_argument("--block-ms", type=int, default=cfg.block_ms,
                    help="阻塞时长（毫秒），0 表示不等待")
    ap.add_argument("--workers", type=int, default=cfg.workers,
                    help="同一批消息的并发处理数（导出/回答都是网络等待）")
    ap.add_argument("--probe-s", type=float, default=cfg.probe_s,
                    help="常驻时多少秒重新探测一次通信服务")
    ap.add_argument("--user", default=cfg.user,
                    help="旧模型的兑底账号（仅 AGENT_COMM_ALLOW_UNSTAMPED=1 时生效）")
    ap.add_argument("--redis-url", default=cfg.redis_url)
    ap.add_argument("--service-url", default=cfg.service_url)
    ap.add_argument("--inspect", metavar="JSON",
                    help="调试：直接处理一段消息信封 JSON（不回包；需带网关盖章，"
                         "或设 AGENT_COMM_ALLOW_UNSTAMPED=1 走旧行为）")
    return ap.parse_args(argv)


def _inspect(cfg: CommConfig, raw: str) -> int:
    """`--inspect JSON`：不起 Redis，直接跑一次 `handle()` 看回包内容。"""
    try:
        env = json.loads(raw)
    except Exception as exc:
        print(f"JSON 解析失败：{exc}")
        return 1
    _print(_build_worker(cfg).handle(env, {}))
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口：`--status` 体检 / `--once` 处理一批 / `--inspect` 调试 / 缺省常驻。"""
    cfg = CommConfig.from_env()
    args = _parse_args(cfg, argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg.user = args.user or cfg.user
    cfg.redis_url = args.redis_url or cfg.redis_url
    cfg.service_url = args.service_url or cfg.service_url
    cfg.workers = max(1, int(args.workers or cfg.workers))

    if args.status:
        _print(service_status(cfg))
        return 0
    if args.inspect:
        return _inspect(cfg, args.inspect)

    worker = _build_worker(cfg)
    if args.once:
        _print(worker.poll_once(limit=args.limit, block_ms=args.block_ms))
        return 0
    try:
        return worker.run_forever(probe_interval=args.probe_s)
    except KeyboardInterrupt:
        log.info("已停止消费")
        return 0


__all__ = [
    "CommConfig", "CommWorker", "CommSupervisor", "start_auto",
    "CommUnavailable", "CommActionError", "verify_gateway_stamp", "stamp_mac",
    "reply_sig",
    "SKILL", "COMM_TOOLS", "run_comm_agent", "service_status", "connect",
    "main",
]
