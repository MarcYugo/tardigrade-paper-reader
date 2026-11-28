#!/usr/bin/env python3
"""agent_comm_service 并发压力测试。

模拟两类角色：
  * 科研 agent —— 用 sdk.AgentCommClient 提交请求，然后长轮询等待完整结果；
  * reader agent —— 消费 outside-in-stream，按 kind 回写 inside-out-stream。

默认流程（scientific 模式，自包含，无需真实 reader agent）：
  1. 预热：1 个请求走完整链路，验证鉴权 / intent / reader 是否正常；
  2. 每个并发档位 N：
     a. N 个协程同时提交 N 个请求（429 自动对齐到下一秒重试，避免把配额当失败）；
     b. N 个协程同时进入 /wait 长轮询，并确认所有 waiter 标记已在 Redis 中登记；
     c. 放行被"闸门"挡住的 mock reader，开始处理（默认每单 200ms）；
     d. 统计提交延迟、等待延迟、成功率、吞吐、队列积压；
  3. 依据"成功率 >= SLO 且 p99 等待 <= 预算"判定每档是否稳定，给出最大稳定并发。

reader 模式（--mode reader）单独测 reader 侧并发：
  1. 先批量提交 N 个请求并排队（此时没有 reader 在消费）；
  2. 再启动 R 个 reader 并发消费，直到所有请求完成；
  3. 统计 drain 时间、req/s、dispatcher 消息吞吐(msg/s)、handler_error 与残余 pending，
     通过 --reader-levels 8,16,32,64 观察加 reader 是否能继续提升吞吐。

mixed 模式（--mode mixed）测两方同时工作：
  对 --mixed-levels（科研侧 N）× --mixed-reader-levels（reader 侧 R）做矩阵，
  每个组合里 N 个科研 agent 同时提交并等待、R 个 reader 同时消费回写，
  观察联合稳定性、端到端 p99，以及增大 R 是否能降低 p99。

用法：
    python tests/stress_test.py
    python tests/stress_test.py --levels 1,8,32,64,128 --rounds 3 --api-key change-me
    python tests/stress_test.py --mode reader --api-key change-me
    python tests/stress_test.py --mode mixed --api-key change-me
    python tests/stress_test.py --mode both --api-key change-me
    python tests/stress_test.py --no-mock-reader          # 已有真实 reader agent 在跑
    python tests/stress_test.py --submit-qps 15 --json-out stress.json

说明：
  * 服务端 QUOTA_QPS 限制的是"提交速率"，不影响长轮询并发；脚本把 429 计入重试，
    若重试次数很多，说明配额是当前系统的瓶颈，可用 --submit-qps 节流以隔离配额影响。
  * mock reader 默认被闸门挡住，保证 N 个长轮询真正同时挂起，测的是通信模块的并发承载；
    配合 --no-gate 可以得到完全流水线式的端到端吞吐。
  * 只用 httpx + redis + sdk，不依赖 app 包（压测机无需安装 FastAPI/pydantic）。
  * 服务端每个长轮询会占住 1 个 Redis 连接；redis-py 8.x 连接池默认只有 100 个，
    因此并发接近/超过 100 时会看到 500（MaxConnectionsError），这是真实的容量上限。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import pathlib
import platform
import random
import sys
import time
import urllib.parse
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import redis.asyncio as aioredis  # noqa: E402
from redis.exceptions import ResponseError, TimeoutError as RedisTimeoutError  # noqa: E402

from sdk.client import AgentCommClient, AgentCommError  # noqa: E402

# 与服务端 env_load.py 的默认值保持一致
DEFAULT_STREAM_IN_RAW = "outside-in-raw"
DEFAULT_STREAM_IN = "outside-in-stream"
DEFAULT_STREAM_OUT = "inside-out-stream"
DEFAULT_READER_GROUP = "outside-in-workers"
WAITER_KEY = "agent:waiter:{rid}"

TABLE_HEADER = (
    f"{'conc':>4} {'req':>4} {'ok':>4} {'fail':>4} {'succ%':>6} {'rps':>6} "
    f"{'sub95':>6} {'p50':>6} {'p99':>6} {'max':>6} {'429':>4} {'waiters':>7} {'ok?':>3}"
)

READER_TABLE_HEADER = (
    f"{'readers':>7} {'reqs':>5} {'ok':>4} {'fail':>4} {'succ%':>6} {'drain':>7} "
    f"{'req/s':>6} {'msg/s':>6} {'p99':>6} {'err':>4} {'pend':>5} {'ok?':>3}"
)

MIXED_TABLE_HEADER = (
    f"{'readers':>7} {'conc':>4} {'req':>4} {'ok':>4} {'fail':>4} {'succ%':>6} {'rps':>6} "
    f"{'sub95':>6} {'p99':>6} {'max':>6} {'waiters':>7} {'ok?':>3}"
)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    base_url: str
    redis_url: str
    agent_ids: list[str]
    api_key: str
    secret: str | None
    levels: list[int]
    rounds: int
    intent: str
    intents: list[str]
    transfer_key: str | None
    paper_id: str | None
    payload_extra: dict[str, Any]
    request_timeout: int
    wait_timeout: float
    poll_window: int
    http_timeout: float
    warmup_timeout: float
    slo: float
    p99_budget: float
    stop_on_fail: bool
    submit_qps: float
    retry_throttled: int
    level_timeout: float
    barrier_timeout: float
    mock_reader: bool
    gate: bool
    gate_timeout: float
    reader_workers: int
    reader_batch: int
    reader_delay_ms: float
    reader_jitter: float
    reader_error_rate: float
    reader_progress_ratio: float
    reader_require_waiter: bool
    reader_messages_per_request: int
    submit_concurrency: int
    mode: str
    server_spec: str
    reader_levels: list[int]
    reader_requests: int
    reader_timeout: float
    fail_under_readers: int
    mixed_levels: list[int]
    mixed_reader_levels: list[int]
    stream_in_raw: str
    stream_in: str
    stream_out: str
    reader_group: str
    stream_maxlen: int
    max_connections: int
    cleanup: bool
    json_out: str | None
    record_jsonl: str | None
    record_max_chars: int
    fail_under: int
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class Cycle:
    """一次"提交 + 等待"的完整记录。"""

    rid: str
    level: int
    ok: bool = False
    kind: str = ""              # 失败分类；ok=True 时为空
    submit_ms: float = 0.0
    wait_s: float = 0.0
    throttle_retries: int = 0
    detail: str = ""
    submit_at: float = 0.0        # perf_counter，用于 reader 模式计算完成时延
    intent: str = ""
    round_idx: int = 0            # 第几轮（同一档位内）
    status: str = ""              # 服务端返回的请求状态


@dataclass
class PassRaw:
    cycles: list[Cycle]
    admitted: int
    admit_s: float
    wait_elapsed_s: float
    waiters_ok: int
    waiters_total: int
    barrier_ok: bool
    pending: dict[str, int]


@dataclass
class LevelResult:
    level: int
    requested: int
    admitted: int
    ok: int
    success_rate: float
    throughput: float
    failures: dict[str, int]
    submit_p50_ms: float
    submit_p95_ms: float
    wait_p50_s: float
    wait_p95_s: float
    wait_p99_s: float
    wait_max_s: float
    throttle_retries: int
    waiters_ok: int
    waiters_total: int
    pending: dict[str, int]
    stable: bool
    reason: str


@dataclass
class ReaderLevelResult:
    """reader 侧单档结果：R 个并发 reader 消费一批已排队的请求。"""

    readers: int
    requested: int
    admitted: int
    ok: int
    success_rate: float
    failures: dict[str, int]
    submit_s: float
    drain_s: float
    rps: float
    mps: float
    expected_messages: int
    dispatched_messages: int
    handler_errors: int
    p50_s: float
    p99_s: float
    pending: dict[str, int]
    stable: bool
    reason: str


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def pct(values: list[float], q: float) -> float:
    """线性插值分位数；空列表返回 0。"""
    if not values:
        return 0.0
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def _short(text: Any, limit: int = 200) -> str:
    s = str(text).replace("\n", " ")
    return s[:limit]


def _classify_http(status: int) -> str:
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 429:
        return "throttled"
    return f"http_{status}"


def _dotenv_value(key: str) -> str | None:
    """从仓库根目录的 .env 读取一个值（不引入 python-dotenv 依赖）。"""
    path = ROOT / ".env"
    if not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except Exception:
        return None
    return None


def _secret_for(agent_id: str) -> str:
    raw = os.getenv("AGENT_SECRETS") or _dotenv_value("AGENT_SECRETS") or ""
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        return ""
    return str(data.get(agent_id) or "")


def _cpu_info() -> tuple[str | None, int | None, float | None]:
    """从 /proc/cpuinfo 读取 (型号, 物理核数, 当前 MHz)；非 Linux 返回空。"""
    model: str | None = None
    mhz: float | None = None
    pairs: set[tuple[str, str]] = set()
    try:
        text = pathlib.Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None, None, None

    physical_id: str | None = None
    core_id: str | None = None
    for line in text.splitlines():
        if not line.strip():
            if physical_id is not None and core_id is not None:
                pairs.add((physical_id, core_id))
            physical_id = core_id = None
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "model name" and model is None:
            model = value
        elif key in ("hardware", "processor") and model is None and not value.isdigit():
            model = value
        elif key == "physical id":
            physical_id = value
        elif key == "core id":
            core_id = value
        elif key == "cpu mhz" and mhz is None:
            try:
                mhz = float(value)
            except ValueError:
                pass
    if physical_id is not None and core_id is not None:
        pairs.add((physical_id, core_id))
    return model, (len(pairs) or None), mhz


def _mem_info() -> tuple[float | None, float | None]:
    """从 /proc/meminfo 读取 (总量 GiB, 可用 GiB)。"""
    try:
        text = pathlib.Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None, None
    total_kb: int | None = None
    avail_kb: int | None = None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            total_kb = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            avail_kb = int(line.split()[1])

    def to_gb(kb: int | None) -> float | None:
        return round(kb / 1024 / 1024, 1) if kb is not None else None

    return to_gb(total_kb), to_gb(avail_kb)


def _device_info(cfg: Config) -> dict[str, Any]:
    """采集压测机规格；若服务地址是本机，则同样适用于服务端。"""
    model, physical, mhz = _cpu_info()
    total_gb, avail_gb = _mem_info()
    host = (urllib.parse.urlsplit(cfg.base_url).hostname or "").lower()
    same_host = host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")
    return {
        "role": "load-generator",
        "same_host_as_service": same_host,
        "hostname": platform.node() or "-",
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": platform.python_version(),
        "cpu_model": model,
        "cpu_physical_cores": physical,
        "cpu_logical_cores": os.cpu_count(),
        "cpu_mhz": mhz,
        "mem_total_gb": total_gb,
        "mem_available_gb": avail_gb,
        "server_spec": cfg.server_spec,
    }


def _format_device(info: dict[str, Any]) -> str:
    model = info.get("cpu_model") or "unknown CPU"
    physical = info.get("cpu_physical_cores")
    logical = info.get("cpu_logical_cores")
    cores = f"{physical}C/{logical}T" if physical else f"{logical}T"
    mem = (
        f"{info['mem_total_gb']} GiB"
        if info.get("mem_total_gb") is not None
        else "内存未知"
    )
    if info.get("mem_available_gb") is not None:
        mem += f"（可用 {info['mem_available_gb']} GiB）"
    return f"CPU {cores} {model} | 内存 {mem} | {info.get('os', '-')}"


class Pacer:
    """全局提交节流：qps<=0 表示不限速。"""

    def __init__(self, qps: float) -> None:
        self._interval = (1.0 / qps) if qps and qps > 0 else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if not self._interval:
            return
        async with self._lock:
            now = time.monotonic()
            at = max(now, self._next)
            self._next = at + self._interval
            delay = at - now
        if delay > 0:
            await asyncio.sleep(delay)


class GateRegistry:
    """同一进程内按 key 管理 asyncio.Event，用于"闸门"住 mock reader。"""

    def __init__(self) -> None:
        self._gates: dict[str, asyncio.Event] = {}

    def create(self, key: str) -> asyncio.Event:
        gate = asyncio.Event()
        self._gates[key] = gate
        return gate

    def get(self, key: str) -> asyncio.Event | None:
        return self._gates.get(key)

    def drop(self, key: str) -> None:
        self._gates.pop(key, None)


# --------------------------------------------------------------------------- #
# mock reader agent
# --------------------------------------------------------------------------- #
async def _ensure_reader_group(r: aioredis.Redis, cfg: Config) -> None:
    # 用 $ 创建，避免第一次运行时重放历史消息
    try:
        await r.xgroup_create(cfg.stream_in, cfg.reader_group, id="$", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _handle_reader_message(
    cfg: Config,
    r: aioredis.Redis,
    msg_id: str,
    fields: dict,
    registry: GateRegistry,
    stats: Counter,
    consumer: str,
) -> None:
    rid = (fields.get("request_id") or "").strip()
    if not rid:
        stats["bad_messages"] += 1
        await r.xack(cfg.stream_in, cfg.reader_group, msg_id)
        return

    try:
        envelope = json.loads(fields.get("envelope") or "{}")
    except Exception:
        envelope = {}
    payload = envelope.get("payload") if isinstance(envelope, dict) else {}
    if not isinstance(payload, dict):
        payload = {}

    t0 = time.perf_counter()
    # 闸门：让请求先攒在 reader 侧，等全部长轮询就位后再统一放行
    gate_key = payload.get("stress_gate")
    if gate_key:
        gate = registry.get(str(gate_key))
        if gate is not None:
            try:
                await asyncio.wait_for(gate.wait(), timeout=cfg.gate_timeout)
            except asyncio.TimeoutError:
                stats["gate_timeouts"] += 1

    if cfg.reader_require_waiter:
        if not await r.exists(WAITER_KEY.format(rid=rid)):
            stats["skipped_no_waiter"] += 1
            await r.xack(cfg.stream_in, cfg.reader_group, msg_id)
            return

    # 固定消息数：1=final；2=ack+final；>=3 额外补 progress。
    # 用于单独压测 reader -> inside-out-stream -> dispatcher 这条回传链路。
    messages = max(1, cfg.reader_messages_per_request)
    if messages >= 2:
        await r.xadd(
            cfg.stream_out,
            {"request_id": rid, "kind": "ack", "data": json.dumps({"rid": rid})},
            maxlen=cfg.stream_maxlen,
            approximate=True,
        )

    delay = cfg.reader_delay_ms / 1000.0
    if delay > 0:
        jitter = max(0.0, cfg.reader_jitter)
        delay *= random.uniform(max(0.0, 1.0 - jitter), 1.0 + jitter)
        await asyncio.sleep(delay)

    for _ in range(max(0, messages - 2)):
        progress = {"rid": rid, "stage": "reading"}
        await r.xadd(
            cfg.stream_out,
            {"request_id": rid, "kind": "progress", "data": json.dumps(progress, ensure_ascii=False)},
            maxlen=cfg.stream_maxlen,
            approximate=True,
        )

    if cfg.reader_progress_ratio > 0 and random.random() < cfg.reader_progress_ratio:
        progress = {"rid": rid, "stage": "reading"}
        await r.xadd(
            cfg.stream_out,
            {"request_id": rid, "kind": "progress", "data": json.dumps(progress, ensure_ascii=False)},
            maxlen=cfg.stream_maxlen,
            approximate=True,
        )

    if random.random() < cfg.reader_error_rate:
        kind = "error"
        data: dict[str, Any] = {"rid": rid, "reason": "simulated_reader_error"}
        stats["error_emitted"] += 1
    else:
        kind = "final"
        data = {
            "rid": rid,
            "answer": f"stress-final-{rid[:12]}",
            "reader": consumer,
            "claim": payload.get("claim"),
            "processed_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }

    await r.xadd(
        cfg.stream_out,
        {"request_id": rid, "kind": kind, "data": json.dumps(data, ensure_ascii=False)},
        maxlen=cfg.stream_maxlen,
        approximate=True,
    )
    await r.xack(cfg.stream_in, cfg.reader_group, msg_id)
    stats["processed"] += 1


async def _reader_loop(
    cfg: Config,
    r: aioredis.Redis,
    idx: int,
    registry: GateRegistry,
    stats: Counter,
    stop: asyncio.Event,
    batch: int | None = None,
) -> None:
    consumer = f"stress-reader-{cfg.run_id}-{idx}-{uuid.uuid4().hex[:4]}"
    count = max(1, batch if batch is not None else cfg.reader_batch)
    while not stop.is_set():
        try:
            resp = await r.xreadgroup(
                cfg.reader_group,
                consumer,
                {cfg.stream_in: ">"},
                count=count,
                block=200,
            )
        except RedisTimeoutError:
            continue
        except asyncio.CancelledError:
            raise
        except Exception:
            stats["read_errors"] += 1
            await asyncio.sleep(0.5)
            continue

        for _stream, entries in resp or []:
            for msg_id, fields in entries:
                try:
                    await _handle_reader_message(cfg, r, msg_id, fields, registry, stats, consumer)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    stats["process_errors"] += 1
                    with contextlib.suppress(Exception):
                        await r.xack(cfg.stream_in, cfg.reader_group, msg_id)


def _spawn_readers(
    cfg: Config,
    r: aioredis.Redis,
    n: int,
    registry: GateRegistry,
    stats: Counter,
    stop: asyncio.Event,
    batch: int | None = None,
) -> list[asyncio.Task]:
    return [
        asyncio.create_task(_reader_loop(cfg, r, i, registry, stats, stop, batch))
        for i in range(max(1, n))
    ]


async def _stop_readers(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# --------------------------------------------------------------------------- #
# reader 回复记录：逐条写 JSONL，不占内存
# --------------------------------------------------------------------------- #
class ReplyRecorder:
    def __init__(self, path: str, max_chars: int = 8000) -> None:
        self.path = pathlib.Path(path)
        self.max_chars = max_chars
        self.count = 0
        self.by_type: Counter = Counter()
        self.by_intent: Counter = Counter()
        self._fh: Any = None

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        print(f"[record] reader 回复逐条写入：{self.path}")

    def _trim(self, data: Any) -> Any:
        """按字符上限裁剪，但**优先丢分节正文与图片 base64**、尽量保留笔记全量。

        reader 的大结果（笔记全量导出）在消息里只是摘要，压测脚本会先
        `fetch_result()` 取回完整内容；这里只做体积保护：

        1. 先丢掉**配图 base64**（`figures[*].data_url`，通常占 90% 体积）——
           只留 `page/file/kind/caption/bytes`，并标记 `data_omitted`；
        2. 再丢掉**整篇纯文本**（`fulltext.text`）——只留 `chars/sections/preview`；
        3. 然后去 `sections[*].text/blocks/entries`；
        4. 再把笔记列表按条数截断（每条内容保持完整），最后才退化为纯文本预览。
        """
        if data is None or not self.max_chars:
            return data
        try:
            text = json.dumps(data, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(data)
        if len(text) <= self.max_chars:
            return data
        try:
            slim = json.loads(text)
        except Exception:
            return {"truncated": True, "chars": len(text), "preview": text[: self.max_chars]}
        if not isinstance(slim, dict):
            return {"truncated": True, "chars": len(text), "preview": text[: self.max_chars]}

        original = len(text)

        # 1) 配图 base64（最大头）：只留元信息
        figures = slim.get("figures")
        if isinstance(figures, dict):
            dropped = 0
            for item in figures.get("items") or []:
                if isinstance(item, dict) and item.pop("data_url", None):
                    dropped += 1
            if dropped:
                figures["data_omitted"] = dropped

        # 2) 整篇纯文本：只留字数 + 预览
        fulltext = slim.get("fulltext")
        if isinstance(fulltext, dict):
            body = str(fulltext.get("text") or "")
            if body:
                fulltext["chars"] = len(body)
                fulltext["preview"] = body[:600]
                fulltext.pop("text", None)
                fulltext["trimmed"] = True
            fulltext.pop("paragraphs", None)

        # 3) 分节正文 / 块
        sections = slim.get("sections")
        if isinstance(sections, dict):
            for sec in sections.values():
                if not isinstance(sec, dict):
                    continue
                if isinstance(sec.get("text"), str):
                    sec["chars"] = len(sec["text"])
                if isinstance(sec.get("blocks"), list):
                    sec["blocks"] = len(sec["blocks"])
                for key in ("text", "entries"):
                    sec.pop(key, None)

        notes = slim.get("notes")
        for keep in (200, 80, 30, 10, 3, 1):
            if len(json.dumps(slim, ensure_ascii=False, default=str)) <= self.max_chars:
                break
            for holder in (notes, slim if not isinstance(notes, dict) else None):
                if isinstance(holder, dict):
                    for key in ("sentence_notes", "free_notes", "highlights"):
                        seq = holder.get(key)
                        if isinstance(seq, list) and len(seq) > keep:
                            holder[key] = seq[:keep]
                elif isinstance(holder, list) and len(holder) > keep:
                    del holder[keep:]

        slim["_trimmed"] = {
            "original_chars": original,
            "note": ("已去掉配图 base64 / fulltext 正文 / sections 正文与块；"
                     "notes 尽量逐条保留（必要时按条数截断）"),
        }
        body = json.dumps(slim, ensure_ascii=False, default=str)
        if len(body) <= self.max_chars:
            return slim
        return {"truncated": True, "chars": original, "preview": body[: self.max_chars]}

    def write(
        self,
        cfg: Config,
        cyc: Cycle,
        status: str = "",
        reply_type: str = "",
        data: Any = None,
    ) -> None:
        if self._fh is None:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": cfg.run_id,
            "level": cyc.level,
            "round": cyc.round_idx,
            "intent": cyc.intent,
            "request_id": cyc.rid,
            "ok": cyc.ok,
            "kind": cyc.kind,
            "status": status,
            "reply_type": reply_type,
            "submit_ms": round(cyc.submit_ms, 2),
            "wait_s": round(cyc.wait_s, 4),
            "detail": cyc.detail,
            "reply": self._trim(data),
        }
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self.count += 1
        self.by_type[reply_type or "-"] += 1
        self.by_intent[f"{cyc.intent or '-'} / {'ok' if cyc.ok else (cyc.kind or 'fail')}"] += 1
        if self.count % 100 == 0:
            self._fh.flush()

    def close(self) -> None:
        if self._fh is None:
            return
        self._fh.flush()
        self._fh.close()
        self._fh = None
        summary = {
            "file": str(self.path),
            "total": self.count,
            "by_reply_type": dict(self.by_type),
            "by_intent_outcome": dict(self.by_intent),
        }
        out = self.path.with_suffix(".summary.json")
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[record] 已记录 {self.count} 条 reader 回复 → {self.path}")
        print(f"[record] 汇总 → {out}")


_recorder: ReplyRecorder | None = None


def _set_recorder(rec: ReplyRecorder | None) -> None:
    global _recorder
    _recorder = rec


def _close_recorder() -> None:
    global _recorder
    if _recorder is not None:
        _recorder.close()
        _recorder = None


def _record_reply(
    cfg: Config, cyc: Cycle, status: str = "", reply_type: str = "", data: Any = None
) -> None:
    if _recorder is not None:
        _recorder.write(cfg, cyc, status, reply_type, data)


# --------------------------------------------------------------------------- #
# 科研 agent 侧：提交 / 等待
# --------------------------------------------------------------------------- #
async def _sleep_to_next_window(cfg: Config) -> None:
    """对齐到下一个整秒再重试，避开固定窗口配额。"""
    now = time.time()
    delay = math.ceil(now) - now + 0.02 + random.uniform(0.0, 0.08)
    await asyncio.sleep(max(0.02, delay))


async def _submit_one(
    cfg: Config,
    client: AgentCommClient,
    pacer: Pacer,
    level: int,
    idx: int,
    gate_key: str,
) -> tuple[str | None, Cycle]:
    # 多个 intent 按 worker 序号轮询混用（8/16/32/64 档位下两种请求比例会自然接近 1:1）
    intent = cfg.intents[idx % len(cfg.intents)] if cfg.intents else cfg.intent
    payload: dict[str, Any] = {
        "claim": f"stress level={level} worker={idx}",
        "paper_id": cfg.paper_id or f"stress-{uuid.uuid4().hex[:10]}",
        "stress_gate": gate_key,
        "stress_level": level,
        "meta": {"run": cfg.run_id, "level": level, "worker": idx, "intent": intent},
    }
    payload.update(cfg.payload_extra)
    retries = 0
    for attempt in range(cfg.retry_throttled + 1):
        await pacer.wait()
        t0 = time.perf_counter()
        try:
            resp = await client.submit(
                intent,
                payload=payload,
                transfer_key=cfg.transfer_key,
                timeout_s=cfg.request_timeout,
            )
            return resp["request_id"], Cycle(
                rid=resp["request_id"],
                level=level,
                intent=intent,
                submit_ms=(time.perf_counter() - t0) * 1000.0,
                throttle_retries=retries,
                submit_at=t0,
            )
        except AgentCommError as exc:
            submit_ms = (time.perf_counter() - t0) * 1000.0
            if exc.status == 429 and attempt < cfg.retry_throttled:
                retries += 1
                await _sleep_to_next_window(cfg)
                continue
            return None, Cycle(
                rid="",
                level=level,
                intent=intent,
                kind=_classify_http(exc.status),
                submit_ms=submit_ms,
                throttle_retries=retries,
                detail=_short(exc.detail),
            )
        except (httpx.HTTPError, OSError) as exc:
            return None, Cycle(
                rid="",
                level=level,
                intent=intent,
                kind="network",
                submit_ms=(time.perf_counter() - t0) * 1000.0,
                throttle_retries=retries,
                detail=_short(repr(exc)),
            )
    return None, Cycle(rid="", level=level, intent=intent, kind="throttled", throttle_retries=retries)


async def _wait_one(cfg: Config, client: AgentCommClient, cyc: Cycle, rid: str) -> Cycle:
    t0 = time.perf_counter()
    try:
        result = await client.wait(rid, timeout_s=cfg.wait_timeout, window=cfg.poll_window)
    except AgentCommError as exc:
        cyc.kind = _classify_http(exc.status)
        cyc.detail = _short(exc.detail)
        cyc.wait_s = time.perf_counter() - t0
        _record_reply(cfg, cyc)
        return cyc
    except (httpx.HTTPError, OSError) as exc:
        cyc.kind = "network"
        cyc.detail = _short(repr(exc))
        cyc.wait_s = time.perf_counter() - t0
        _record_reply(cfg, cyc)
        return cyc

    cyc.wait_s = time.perf_counter() - t0
    status = str(result.get("status") or "")
    cyc.status = status
    items = result.get("items") or []
    final = None
    last_item: dict[str, Any] | None = None
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        if last_item is None:
            last_item = item
        if item.get("type") == "final":
            final = item
            break

    # final 回传项是权威信号：即使 status 尚未刷新，也应视为成功
    if final is not None:
        data = final.get("data")
        if isinstance(data, dict) and data.get("rid") and str(data["rid"]) != rid:
            cyc.kind = "bad_payload"
            cyc.detail = f"rid mismatch: {data.get('rid')}"
        else:
            cyc.ok = True
            # 大结果（笔记全量导出等）：消息里只是摘要，完整内容在
            # agent:result:{rid}，用 fetch_result() 取回后再记录；
            # purge=True：压测是「拿到就算完」，取完立即删，别让几 MB 结果占着 Redis 到 TTL
            if isinstance(data, dict) and (data.get("result_url") or data.get("result_ref")):
                t_hydrate = time.perf_counter()
                try:
                    full = await client.fetch_result(rid, purge=True)
                except (AgentCommError, httpx.HTTPError, OSError) as exc:
                    note = f"hydrate_failed: {_short(repr(exc), 160)}"
                    full = None
                else:
                    note = f"hydrate {time.perf_counter() - t_hydrate:.3f}s (purged)"
                cyc.detail = "; ".join(x for x in (cyc.detail, note) if x)
                if full is not None:
                    final = {**final, "data": full}
                    last_item = final
    elif status in ("error", "cancelled", "quarantined", "rejected"):
        cyc.kind = f"status:{status}"
    elif status == "done":
        cyc.kind = "no_final"
    else:
        cyc.kind = "timeout"

    # 记录 reader 的实际回包（final / error / progress 都存）
    _record_reply(
        cfg,
        cyc,
        status,
        str((last_item or {}).get("type") or ""),
        (last_item or {}).get("data"),
    )
    return cyc


async def _wait_for_waiters(
    cfg: Config,
    r: aioredis.Redis,
    rids: list[str],
    tasks: dict[str, asyncio.Task],
    timeout_s: float,
) -> list[str]:
    """轮询 Redis，直到所有 request 的 waiter 标记就绪（或超时）。"""
    pending = list(rids)
    deadline = time.perf_counter() + timeout_s
    while pending and time.perf_counter() < deadline:
        pending = [rid for rid in pending if not tasks[rid].done()]
        if not pending:
            return []
        pipe = r.pipeline()
        for rid in pending:
            pipe.exists(WAITER_KEY.format(rid=rid))
        try:
            flags = await pipe.execute()
        except Exception:
            flags = [0] * len(pending)
        pending = [rid for rid, ok in zip(pending, flags) if not ok]
        if pending:
            await asyncio.sleep(0.02)
    return pending


async def _submit_many(
    cfg: Config,
    clients: list[AgentCommClient],
    pacer: Pacer,
    count: int,
) -> tuple[list[tuple[str | None, Cycle]], float]:
    """reader 模式：批量提交但只排队，不等结果。"""
    sem = asyncio.Semaphore(max(1, cfg.submit_concurrency))

    async def one(i: int) -> tuple[str | None, Cycle]:
        async with sem:
            # gate_key 为空字符串：reader 不会挂起，直接消费
            return await _submit_one(cfg, clients[i % len(clients)], pacer, 0, i, "")

    t0 = time.perf_counter()
    tasks = [asyncio.create_task(one(i)) for i in range(count)]
    done, pending = await asyncio.wait(tasks, timeout=cfg.level_timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    results: list[tuple[str | None, Cycle]] = []
    for task in tasks:
        if task in done and not task.cancelled() and task.exception() is None:
            results.append(task.result())
        else:
            results.append(
                (None, Cycle(rid="", level=0, kind="timeout", detail="submit phase timeout"))
            )
    return results, time.perf_counter() - t0


async def _wait_all_terminal(
    cfg: Config,
    r: aioredis.Redis,
    rids: list[str],
    base_ts: float,
    timeout_s: float,
) -> tuple[dict[str, float], dict[str, str], list[str]]:
    """轮询请求状态，返回 (完成时延, 终态, 仍未完成)；时延相对 base_ts。"""
    deadline = time.perf_counter() + timeout_s
    pending = list(rids)
    offsets: dict[str, float] = {}
    statuses: dict[str, str] = {}
    terminal = {"done", "error", "cancelled", "quarantined", "rejected"}

    while pending and time.perf_counter() < deadline:
        pipe = r.pipeline()
        for rid in pending:
            pipe.hget(f"agent:req:{rid}", "status")
        try:
            rows = await pipe.execute()
        except Exception:
            rows = [None] * len(pending)

        still: list[str] = []
        now = time.perf_counter()
        for rid, status in zip(pending, rows):
            if status in terminal:
                offsets[rid] = now - base_ts
                statuses[rid] = str(status)
            else:
                still.append(rid)
        pending = still
        if pending:
            await asyncio.sleep(0.05)
    return offsets, statuses, pending


# --------------------------------------------------------------------------- #
# 单档执行 + 汇总
# --------------------------------------------------------------------------- #
async def run_pass(
    cfg: Config,
    level: int,
    rnd: int,
    pacer: Pacer,
    clients: list[AgentCommClient],
    registry: GateRegistry,
    r: aioredis.Redis | None,
) -> PassRaw:
    gate_key = f"{cfg.run_id}:{level}:{rnd}:{uuid.uuid4().hex[:6]}"
    gate = registry.create(gate_key) if (cfg.mock_reader and cfg.gate) else None
    client_for = [clients[i % len(clients)] for i in range(level)]

    # ---- 阶段 1：同时提交 ----
    t_admit0 = time.perf_counter()
    try:
        submitted = await asyncio.wait_for(
            asyncio.gather(
                *[_submit_one(cfg, client_for[i], pacer, level, i, gate_key) for i in range(level)]
            ),
            timeout=cfg.level_timeout,
        )
    except asyncio.TimeoutError:
        if gate is not None:
            gate.set()
        registry.drop(gate_key)
        cycles = [
            Cycle(rid="", level=level, kind="timeout", detail="admission phase timeout")
            for _ in range(level)
        ]
        for cyc in cycles:
            cyc.round_idx = rnd
            _record_reply(cfg, cyc)
        return PassRaw(cycles, 0, cfg.level_timeout, 0.0, 0, 0, False, {})
    admit_s = time.perf_counter() - t_admit0

    cycles: list[Cycle] = []
    admitted: list[tuple[str, Cycle]] = []
    for rid, cyc in submitted:
        cyc.round_idx = rnd
        if rid:
            admitted.append((rid, cyc))
        else:
            _record_reply(cfg, cyc)      # 提交就被拒（鉴权/配额/校验）也记一条
            cycles.append(cyc)

    # 真正的积压看 pending，而不是 XLEN（stream 只删旧条目、不随 ACK 缩短）
    pending = await _fetch_pending(cfg, r)

    # ---- 阶段 2：同时长轮询，等 waiter 全部就位后放行 reader ----
    wait_tasks = {
        rid: asyncio.create_task(_wait_one(cfg, client_for[i], cyc, rid))
        for i, (rid, cyc) in enumerate(admitted)
    }
    waiters_ok = 0
    barrier_ok = True
    t_wait0 = time.perf_counter()
    try:
        if gate is not None and r is not None and wait_tasks:
            await asyncio.sleep(0)       # 先把 wait 请求发出去
            missing = await _wait_for_waiters(
                cfg, r, list(wait_tasks), wait_tasks, cfg.barrier_timeout
            )
            barrier_ok = not missing
            waiters_ok = len(wait_tasks) - len(missing)
            gate.set()
        if wait_tasks:
            await asyncio.wait_for(
                asyncio.gather(*wait_tasks.values()), timeout=cfg.level_timeout
            )
    except asyncio.TimeoutError:
        for _rid, cyc in admitted:
            if not cyc.ok and not cyc.kind:
                cyc.kind = "timeout"
                cyc.detail = "wait phase timeout"
                if cyc.wait_s <= 0:
                    cyc.wait_s = time.perf_counter() - t_wait0
    finally:
        if gate is not None:
            gate.set()
        for task in wait_tasks.values():
            if not task.done():
                task.cancel()
        if wait_tasks:
            await asyncio.gather(*wait_tasks.values(), return_exceptions=True)
        registry.drop(gate_key)
    wait_elapsed = time.perf_counter() - t_wait0

    cycles.extend(cyc for _rid, cyc in admitted)
    return PassRaw(
        cycles=cycles,
        admitted=len(admitted),
        admit_s=admit_s,
        wait_elapsed_s=wait_elapsed,
        waiters_ok=waiters_ok,
        waiters_total=len(admitted),
        barrier_ok=barrier_ok,
        pending=pending,
    )


def summarize_level(cfg: Config, level: int, passes: list[PassRaw]) -> LevelResult:
    cycles = [c for p in passes for c in p.cycles]
    requested = len(cycles)
    ok = sum(1 for c in cycles if c.ok)
    admitted = sum(p.admitted for p in passes)
    retries = sum(c.throttle_retries for c in cycles)

    failures = Counter(c.kind or "unknown" for c in cycles if not c.ok)
    submit_vals = [c.submit_ms for c in cycles if c.submit_ms > 0]
    wait_vals = [c.wait_s for c in cycles if c.wait_s > 0]

    elapsed = sum(p.admit_s + p.wait_elapsed_s for p in passes) or 1e-9
    success_rate = (ok / requested) if requested else 0.0
    p99 = pct(wait_vals, 0.99)

    pending: dict[str, int] = {}
    for p in passes:
        for name, length in p.pending.items():
            pending[name] = max(pending.get(name, 0), length)

    reasons: list[str] = []
    stable = requested > 0 and success_rate >= cfg.slo
    if requested and success_rate < cfg.slo:
        reasons.append(f"成功率 {success_rate * 100:.1f}% < {cfg.slo * 100:.0f}%")
    if cfg.p99_budget > 0 and wait_vals and p99 > cfg.p99_budget:
        stable = False
        reasons.append(f"p99 {p99:.2f}s > {cfg.p99_budget:.1f}s")
    if not cycles:
        stable = False
        reasons.append("无有效样本")
    if failures and reasons:
        top = ", ".join(f"{k}={v}" for k, v in failures.most_common(3))
        reasons.append(f"主要失败: {top}")

    return LevelResult(
        level=level,
        requested=requested,
        admitted=admitted,
        ok=ok,
        success_rate=success_rate,
        throughput=ok / elapsed,
        failures=dict(failures),
        submit_p50_ms=pct(submit_vals, 0.50),
        submit_p95_ms=pct(submit_vals, 0.95),
        wait_p50_s=pct(wait_vals, 0.50),
        wait_p95_s=pct(wait_vals, 0.95),
        wait_p99_s=p99,
        wait_max_s=max(wait_vals) if wait_vals else 0.0,
        throttle_retries=retries,
        waiters_ok=sum(p.waiters_ok for p in passes),
        waiters_total=sum(p.waiters_total for p in passes),
        pending=pending,
        stable=stable,
        reason="；".join(reasons),
    )


# --------------------------------------------------------------------------- #
# reader 侧：R 个并发 reader 消费一批已排队的请求
# --------------------------------------------------------------------------- #
async def run_reader_level(
    cfg: Config,
    readers: int,
    pacer: Pacer,
    clients: list[AgentCommClient],
    registry: GateRegistry,
    r: aioredis.Redis,
    http_client: httpx.AsyncClient,
    reader_stats: Counter,
) -> ReaderLevelResult:
    metrics_before = await _fetch_json(http_client, f"{cfg.base_url}/metrics")

    # 先把请求全部排进队列（此时没有 reader 在消费），再启动 R 个 reader 测吞吐
    submitted, submit_s = await _submit_many(cfg, clients, pacer, cfg.reader_requests)
    admitted = [(rid, cyc) for rid, cyc in submitted if rid]
    cycles = [cyc for rid, cyc in submitted if not rid]

    stop = asyncio.Event()
    base = time.perf_counter()
    # 一次只取 1 条，让 R 个 reader 均摊积压，才是真正的“并发 reader”
    tasks = _spawn_readers(cfg, r, readers, registry, reader_stats, stop, batch=1)
    await asyncio.sleep(0.05)

    try:
        offsets, statuses, leftover = await _wait_all_terminal(
            cfg, r, [rid for rid, _ in admitted], base, cfg.reader_timeout
        )
        drain_s = time.perf_counter() - base
        await asyncio.sleep(0.2)          # 给 out-stream 最后几条回传一点落地时间
    finally:
        await _stop_readers(tasks)
    pending = await _fetch_pending(cfg, r)
    metrics_after = await _fetch_json(http_client, f"{cfg.base_url}/metrics")
    delta = _counter_delta(metrics_before, metrics_after)

    ok = sum(1 for status in statuses.values() if status == "done")
    failures = Counter(c.kind for c in cycles if not c.ok and c.kind)
    for status in statuses.values():
        if status != "done":
            failures[f"status:{status}"] += 1
    if leftover:
        failures["timeout"] += len(leftover)

    requested = cfg.reader_requests
    admitted_n = len(admitted)
    success_rate = (ok / requested) if requested else 0.0
    pending_total = sum(pending.values())
    dispatched = sum(
        v for k, v in delta.items()
        if k in ("dispatch.final", "dispatch.ack", "dispatch.progress", "dispatch.error")
    )
    handler_errors = sum(v for k, v in delta.items() if "handler_error" in k)
    expected = admitted_n * max(1, cfg.reader_messages_per_request)
    latencies = list(offsets.values())

    reasons: list[str] = []
    stable = requested > 0 and success_rate >= cfg.slo
    if requested and success_rate < cfg.slo:
        reasons.append(f"成功率 {success_rate * 100:.1f}% < {cfg.slo * 100:.0f}%")
    if leftover:
        stable = False
        reasons.append(f"{len(leftover)} 个请求超过 reader_timeout")
    if handler_errors:
        stable = False
        reasons.append(f"dispatch handler_error={handler_errors}")
    if pending_total:
        stable = False
        reasons.append(f"结束时 pending={pending_total}")
    if failures and reasons:
        top = ", ".join(f"{k}={v}" for k, v in failures.most_common(3))
        reasons.append(f"主要失败: {top}")

    return ReaderLevelResult(
        readers=readers,
        requested=requested,
        admitted=admitted_n,
        ok=ok,
        success_rate=success_rate,
        failures=dict(failures),
        submit_s=submit_s,
        drain_s=drain_s,
        rps=(ok / max(drain_s, 1e-9)),
        mps=(dispatched / max(drain_s, 1e-9)),
        expected_messages=expected,
        dispatched_messages=dispatched,
        handler_errors=handler_errors,
        p50_s=pct(latencies, 0.50),
        p99_s=pct(latencies, 0.99),
        pending=pending,
        stable=stable,
        reason="；".join(reasons),
    )


def _print_reader_level(lr: ReaderLevelResult) -> None:
    pending_total = sum(lr.pending.values())
    print(
        f"{lr.readers:>7} {lr.requested:>5} {lr.ok:>4} {lr.requested - lr.ok:>4} "
        f"{lr.success_rate * 100:>6.1f} {lr.drain_s:>7.2f} {lr.rps:>6.1f} {lr.mps:>6.1f} "
        f"{lr.p99_s:>6.2f} {lr.handler_errors:>4} {pending_total:>5} "
        f"{'YES' if lr.stable else 'NO':>3}",
        flush=True,
    )
    extra: list[str] = []
    if lr.failures:
        extra.append("失败: " + ", ".join(f"{k}={v}" for k, v in sorted(lr.failures.items())))
    extra.append(f"回传 {lr.dispatched_messages}/{lr.expected_messages} 条")
    if lr.reason:
        extra.append("原因: " + lr.reason)
    print("        ↳ " + " | ".join(extra), flush=True)


def _print_reader_summary(
    cfg: Config,
    results: list[ReaderLevelResult],
    max_stable: int,
    first_unstable: ReaderLevelResult | None,
) -> None:
    print("\n" + "=" * 78)
    print("reader 侧结论（R 个并发 reader 消费积压请求）")
    print("=" * 78)
    print(f"最大稳定 reader 并发: {max_stable if max_stable else '无'}")
    if first_unstable is not None:
        print(f"首个不稳定档位: {first_unstable.readers}（{first_unstable.reason}）")
    if results:
        peak = max(results, key=lambda x: x.rps)
        print(f"峰值吞吐: {peak.rps:.1f} req/s / {peak.mps:.1f} msg/s（readers={peak.readers}）")
        if len(results) >= 2:
            gain = (results[-1].rps - results[-2].rps) / max(results[-2].rps, 1e-9)
            if gain > 0.10:
                print(
                    f"reader 仍未饱和：最后一档吞吐较前一档 +{gain * 100:.0f}%，"
                    "继续提高 --reader-levels 可继续加压。"
                )
            else:
                target = peak.rps * 0.95
                knee = next((x.readers for x in results if x.rps >= target), peak.readers)
                print(
                    f"reader 接近饱和，推荐并发 {knee}（达到峰值 95% 的最小档位）；"
                    "再加 reader 收益有限。"
                )

    hints: list[str] = []
    if any(r.handler_errors for r in results):
        hints.append(
            "出现 dispatch handler_error：inside-out 消费并发不足，调 WORKER_CONCURRENCY/READ_BATCH，"
            "或排查 Redis 连接池（每个长轮询 + 消费者都占连接）。"
        )
    if any(r.pending.get(cfg.stream_in_raw, 0) for r in results):
        hints.append("outside-in-raw 仍有 pending：guard 深检消费者是瓶颈，调 WORKER_CONCURRENCY。")
    if any(r.pending.get(cfg.stream_in, 0) for r in results):
        hints.append("outside-in-stream 仍有 pending：reader 消费能力不足或 reader 数太少。")
    if any(r.pending.get(cfg.stream_out, 0) for r in results):
        hints.append("inside-out-stream 仍有 pending：dispatcher 回传消费滞后。")
    if results and max(r.rps for r in results) > 0:
        hints.append("req/s 随 readers 增长趋于平缓后，再加 reader 不再提升吞吐，说明瓶颈已转到服务端/Redis。")
    if hints:
        print("提示:")
        for h in hints:
            print(f"  - {h}")


def _print_mixed_row(readers: int, lr: LevelResult, gated: bool) -> None:
    fail = lr.requested - lr.ok
    waiters = f"{lr.waiters_ok}/{lr.waiters_total}" if gated else "-"
    print(
        f"{readers:>7} {lr.level:>4} {lr.requested:>4} {lr.ok:>4} {fail:>4} "
        f"{lr.success_rate * 100:>6.1f} {lr.throughput:>6.1f} {lr.submit_p95_ms:>6.1f} "
        f"{lr.wait_p99_s:>6.2f} {lr.wait_max_s:>6.2f} {waiters:>7} "
        f"{'YES' if lr.stable else 'NO':>3}",
        flush=True,
    )
    extra: list[str] = []
    if lr.failures:
        extra.append("失败: " + ", ".join(f"{k}={v}" for k, v in sorted(lr.failures.items())))
    if lr.pending:
        extra.append("pending峰值: " + " ".join(f"{k}={v}" for k, v in lr.pending.items() if v))
    if lr.reason:
        extra.append("原因: " + lr.reason)
    if extra:
        print("        ↳ " + " | ".join(extra), flush=True)


def _print_mixed_summary(rows: list[tuple[int, LevelResult]]) -> None:
    print("\n" + "=" * 78)
    print("联合结论（科研 agent 与 reader 同时工作）")
    print("=" * 78)
    if not rows:
        print("无样本")
        return

    per_r: dict[int, list[LevelResult]] = {}
    for readers, lr in rows:
        per_r.setdefault(readers, []).append(lr)
    for readers in sorted(per_r):
        stable = [lr for lr in per_r[readers] if lr.stable]
        if stable:
            best = max(stable, key=lambda x: x.level)
            print(
                f"readers={readers}: 最大稳定 N={best.level} "
                f"(p99 {best.wait_p99_s:.2f}s, {best.throughput:.1f} rps)"
            )
        else:
            print(f"readers={readers}: 无稳定档位")

    stable_rows = [(R, lr) for R, lr in rows if lr.stable]
    if stable_rows:
        best = max(stable_rows, key=lambda x: (x[1].level, -x[1].wait_p99_s))
        print(
            f"联合最大稳定: N={best[1].level} × readers={best[0]} "
            f"(p99 {best[1].wait_p99_s:.2f}s)"
        )
    else:
        print("联合最大稳定: 无")

    improves = False
    for n in sorted({lr.level for _, lr in rows}):
        items = sorted((R, lr) for R, lr in rows if lr.level == n and lr.ok)
        if len(items) >= 2:
            first, last = items[0], items[-1]
            print(
                f"  N={n}: R={first[0]} p99={first[1].wait_p99_s:.2f}s → "
                f"R={last[0]} p99={last[1].wait_p99_s:.2f}s"
            )
            if last[1].wait_p99_s < first[1].wait_p99_s * 0.9:
                improves = True

    hints: list[str] = []
    if improves:
        hints.append("增大 readers 能降低端到端 p99：reader 并发是有效杠杆。")
    if any(lr.throttle_retries for _, lr in rows):
        hints.append("出现 429：联合吞吐受 QUOTA_QPS 限制，可用 --submit-qps 节流或提高服务端配额。")
    if not stable_rows:
        hints.append("无稳定组合：先看失败类型与 pending，必要时降低 N 或提高 R。")
    if hints:
        print("提示:")
        for h in hints:
            print(f"  - {h}")


# --------------------------------------------------------------------------- #
# 服务端指标
# --------------------------------------------------------------------------- #
async def _fetch_json(client: httpx.AsyncClient, url: str) -> dict | None:
    try:
        resp = await client.get(url, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def _fetch_pending(cfg: Config, r: aioredis.Redis | None) -> dict[str, int]:
    """读取各消费组的 pending 数（真正的积压）。"""
    if r is None:
        return {}
    out: dict[str, int] = {}
    pairs = (
        (cfg.stream_in_raw, "guard-workers"),
        (cfg.stream_in, cfg.reader_group),
        (cfg.stream_out, "inside-out-dispatchers"),
    )
    for stream, group in pairs:
        try:
            res = await r.xpending(stream, group)
            count = res.get("pending", 0) if isinstance(res, dict) else int(res or 0)
            out[stream] = int(count)
        except Exception:
            continue
    return out


def _counter_delta(before: dict | None, after: dict | None) -> dict[str, int]:
    if not before or not after:
        return {}
    b = before.get("counters") or {}
    a = after.get("counters") or {}
    out: dict[str, int] = {}
    for key in sorted(set(a) | set(b)):
        diff = int(a.get(key, 0)) - int(b.get(key, 0))
        if diff:
            out[key] = diff
    return out


# --------------------------------------------------------------------------- #
# 预热 / 报告
# --------------------------------------------------------------------------- #
async def _warmup(cfg: Config, clients: list[AgentCommClient]) -> tuple[bool, str]:
    client = clients[0]
    intents = cfg.intents or [cfg.intent]
    try:
        for intent in intents:
            payload: dict[str, Any] = {
                "claim": "warmup",
                "paper_id": cfg.paper_id or "warmup",
            }
            payload.update(cfg.payload_extra)
            if cfg.mock_reader:
                result = await client.ask(
                    intent,
                    payload=payload,
                    transfer_key=cfg.transfer_key,
                    timeout_s=cfg.warmup_timeout,
                )
                if result.get("type") != "final":
                    return False, (
                        f"[{intent}] 未拿到 final："
                        f"{_short(result.get('type'))} {_short(result.get('data'))}"
                    )
                continue
            resp = await client.submit(
                intent,
                payload=payload,
                transfer_key=cfg.transfer_key,
                wait=False,
                timeout_s=cfg.warmup_timeout,
            )
            await client.cancel(resp["request_id"])
        return True, ""
    except AgentCommError as exc:
        hint = ""
        if exc.status == 401:
            hint = "；检查 --api-key / --secret（需与服务端 AGENT_SECRETS 一致）"
        elif exc.status == 403:
            hint = "；from_agent 与鉴权身份不一致，或 transfer_key 非法"
        elif exc.status == 422:
            hint = "；intent 不在白名单，或 payload 触发了内容检测"
        elif exc.status == 429:
            hint = "；QUOTA_QPS 太小，稍后重跑"
        return False, f"HTTP {exc.status}: {_short(exc.detail, 300)}{hint}"
    except (httpx.HTTPError, OSError) as exc:
        return False, f"无法连接服务：{exc!r}"


def _print_level(lr: LevelResult, cfg: Config, gated: bool) -> None:
    fail = lr.requested - lr.ok
    waiters = f"{lr.waiters_ok}/{lr.waiters_total}" if gated else "-"
    print(
        f"{lr.level:>4} {lr.requested:>4} {lr.ok:>4} {fail:>4} {lr.success_rate * 100:>6.1f} "
        f"{lr.throughput:>6.1f} {lr.submit_p95_ms:>6.1f} "
        f"{lr.wait_p50_s:>6.2f} {lr.wait_p99_s:>6.2f} {lr.wait_max_s:>6.2f} "
        f"{lr.throttle_retries:>4} {waiters:>7} {'YES' if lr.stable else 'NO':>3}",
        flush=True,
    )
    extra: list[str] = []
    if lr.failures:
        extra.append("失败: " + ", ".join(f"{k}={v}" for k, v in sorted(lr.failures.items())))
    if lr.pending:
        q = []
        for name in (cfg.stream_in_raw, cfg.stream_in, cfg.stream_out):
            if name in lr.pending:
                q.append(f"{name}={lr.pending[name]}")
        if q:
            extra.append("pending峰值: " + " ".join(q))
    if lr.reason:
        extra.append("原因: " + lr.reason)
    if extra:
        print("      ↳ " + " | ".join(extra), flush=True)


def _print_summary(
    cfg: Config,
    results: list[LevelResult],
    max_stable: int,
    first_unstable: LevelResult | None,
    metrics_delta: dict[str, int],
    final_queues: dict[str, int],
    final_pending: dict[str, int],
    reader_stats: Counter,
    gated: bool,
) -> None:
    if cfg.mode in ("reader", "mixed"):
        return
    print("\n" + "=" * 78)
    print("结论")
    print("=" * 78)
    print(f"最大稳定并发: {max_stable if max_stable else '无'}")
    if first_unstable is not None:
        print(f"首个不稳定档位: {first_unstable.level}（{first_unstable.reason}）")
    elif max_stable:
        print("所有测试档位均稳定，可继续提高 --levels 探测上限。")

    if metrics_delta:
        keys = [
            "ingest.accepted",
            "ingest.duplicate",
            "ingest.block",
            "ingest.quarantine",
            "guard.pass",
            "guard.block",
            "guard.quarantine",
            "dispatch.final",
            "dispatch.error",
            "dispatch.ack",
            "dispatch.progress",
            "ingest.cancelled",
        ]
        shown = {k: metrics_delta[k] for k in keys if k in metrics_delta}
        rest = {k: v for k, v in metrics_delta.items() if k not in shown and not k.startswith("uptime")}
        if shown or rest:
            print("服务端计数增量: " + ", ".join(f"{k}=+{v}" for k, v in (shown | rest).items()))
    if final_queues:
        print("stream 长度（累计，仅参考）: " + " ".join(f"{k}={v}" for k, v in final_queues.items()))
    if final_pending:
        print("结束时 pending: " + " ".join(f"{k}={v}" for k, v in final_pending.items()))
    if cfg.mock_reader:
        stats = " ".join(f"{k}={v}" for k, v in sorted(reader_stats.items()))
        print(f"mock reader: {stats or '无统计'}")

    hints: list[str] = []
    if sum(r.throttle_retries for r in results) > 0:
        hints.append("出现 429 重试：提交速率受 QUOTA_QPS 限制，可加 --submit-qps 15 隔离配额影响。")
    if gated and any(r.waiters_total and r.waiters_ok < r.waiters_total for r in results):
        hints.append("部分 waiter 未在超时内登记：检查服务端 /wait 是否被阻塞或连接数不足。")
    if final_pending.get(cfg.stream_in_raw, 0) > 0 or final_pending.get(cfg.stream_in, 0) > 0:
        hints.append("仍有 pending：guard/reader 消费能力可能是瓶颈，调 WORKER_CONCURRENCY 或增加 reader。")
    if any(r.failures.get("network") for r in results):
        hints.append("出现网络错误：检查客户端 ulimit -n、连接池上限与服务端 backlog。")
    if any(r.failures.get("http_500") for r in results):
        hints.append(
            "服务端 500：常见原因是 Redis 连接池耗尽（redis-py 8 默认 100 连接，每个长轮询占 1 个），"
            "需调大服务端 Redis max_connections 或降低并发。"
        )
    if max_stable and max_stable == max(cfg.levels):
        hints.append("最高档位仍稳定，建议用更高的 --levels 继续探测。")
    if hints:
        print("提示:")
        for h in hints:
            print(f"  - {h}")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def run(cfg: Config) -> int:
    print("=" * 78)
    print("agent_comm_service 并发压力测试")
    print("=" * 78)
    print(
        f"模式       : {cfg.mode}"
        "（scientific=科研侧；reader=reader 侧；mixed=两方同时；both=先后跑）"
    )
    print(f"服务       : {cfg.base_url}")
    print(f"Redis      : {cfg.redis_url if cfg.mock_reader else '(未使用)'}")
    print(f"agents     : {', '.join(cfg.agent_ids)}")
    device = _device_info(cfg)
    print(f"设备       : 压测机 {_format_device(device)}")
    if device["same_host_as_service"]:
        print("             （服务地址为本机；若服务在容器中且未设 cgroup 限额，规格与压测机一致）")
    if cfg.server_spec:
        print(f"服务端设备 : {cfg.server_spec}")
    if cfg.mode == "reader":
        print(
            f"reader 档位: {cfg.reader_levels}，每档 {cfg.reader_requests} 个请求，"
            f"每请求回传 {cfg.reader_messages_per_request} 条"
        )
    elif cfg.mode == "mixed":
        print(f"mixed 档位 : N={cfg.mixed_levels} × R={cfg.mixed_reader_levels}，rounds={cfg.rounds}")
    else:
        print(f"档位/轮次  : {cfg.levels} x {cfg.rounds}")
    print(f"intents    : {', '.join(cfg.intents)}  request_timeout={cfg.request_timeout}s")
    if cfg.transfer_key:
        print(f"transfer_key: {cfg.transfer_key[:20]}…（放进信封，由服务端校验）")
    if cfg.paper_id:
        print(f"paper_id   : {cfg.paper_id}")
    if cfg.payload_extra:
        print(f"payload 附加: {', '.join(sorted(cfg.payload_extra))}")
    if cfg.mock_reader:
        print(
            f"mock reader: pool={cfg.reader_workers}, delay={cfg.reader_delay_ms:.0f}ms "
            f"jitter={cfg.reader_jitter:.2f}, gate={'on' if cfg.gate else 'off'}"
        )
    else:
        print("mock reader: 未启用（等待应用侧真实 reader 回包）")
    print(
        f"SLO        : 成功率 >= {cfg.slo * 100:.1f}%，"
        f"p99 等待 <= {cfg.p99_budget:.1f}s（scientific/mixed）"
    )
    print(f"run_id     : {cfg.run_id}")
    print()

    # 健康检查
    http_client = httpx.AsyncClient(timeout=cfg.http_timeout)
    health = await _fetch_json(http_client, f"{cfg.base_url}/healthz")
    if not health or not health.get("ok"):
        print(f"[fatal] 服务不可用：{cfg.base_url}/healthz")
        await http_client.aclose()
        return 1

    r: aioredis.Redis | None = None
    if cfg.mock_reader:
        try:
            r = aioredis.from_url(
                cfg.redis_url,
                decode_responses=True,
                # mock reader 会长期占用连接（XREADGROUP BLOCK），reader 档位更高时也要够用
                max_connections=max(
                    32,
                    cfg.reader_workers,
                    max(cfg.reader_levels or [1]),
                ) + 32,
            )
            await r.ping()
        except Exception as exc:
            print(f"[fatal] Redis 不可用（mock reader 需要）：{exc!r}")
            await http_client.aclose()
            return 1
        await _ensure_reader_group(r, cfg)

    limits = httpx.Limits(
        max_connections=cfg.max_connections,
        max_keepalive_connections=cfg.max_connections,
    )
    clients = [
        AgentCommClient(
            cfg.base_url,
            agent_id,
            api_key=(cfg.api_key or _secret_for(agent_id)) or None,
            secret=cfg.secret or None,
            timeout=cfg.http_timeout,
            limits=limits,
        )
        for agent_id in cfg.agent_ids
    ]

    registry = GateRegistry()
    reader_stats: Counter = Counter()
    stop = asyncio.Event()
    reader_pool: list[asyncio.Task] = []
    all_rids: list[str] = []
    results: list[LevelResult] = []
    reader_results: list[ReaderLevelResult] = []
    mixed_rows: list[tuple[int, LevelResult]] = []
    max_stable = 0
    first_unstable: LevelResult | None = None
    reader_max_stable = 0
    reader_first_unstable: ReaderLevelResult | None = None
    run_scientific = cfg.mode in ("scientific", "both")
    run_reader = cfg.mode in ("reader", "both")
    run_mixed = cfg.mode == "mixed"
    gated = cfg.mock_reader and cfg.gate and (run_scientific or run_mixed)

    if cfg.record_jsonl:
        rec = ReplyRecorder(cfg.record_jsonl, cfg.record_max_chars)
        rec.open()
        _set_recorder(rec)

    try:
        metrics_before = await _fetch_json(http_client, f"{cfg.base_url}/metrics")

        if cfg.mock_reader:
            reader_pool = _spawn_readers(cfg, r, cfg.reader_workers, registry, reader_stats, stop)
            await asyncio.sleep(0.1)
            baseline = await _fetch_pending(cfg, r)
            if any(v > 0 for v in baseline.values()):
                residual = " ".join(f"{k}={v}" for k, v in baseline.items() if v)
                print(f"[warn] 检测到历史 pending（{residual}），可能是上次压测残留，"
                      "服务端会在 XAUTOCLAIM 周期后回收；本次 pending 峰值可能偏高。")

        print("[warmup] 单请求全链路检查 ...", end=" ", flush=True)
        ok, why = await _warmup(cfg, clients)
        if not ok:
            print("FAILED")
            print(f"[fatal] 预热失败：{why}")
            return 1
        print("ok")

        pacer = Pacer(cfg.submit_qps)

        if run_scientific:
            print()
            print("latency: sub95=提交p95(ms)，p50/p99/max=等待时延(s)")
            print("rps 含提交准入；429=配额重试；waiters=放行前已登记/应登记")
            print("pending=消费组未 ACK 数，可能含历史残留")
            print(TABLE_HEADER)
            print("-" * len(TABLE_HEADER))

        for level in (cfg.levels if run_scientific else []):
            passes: list[PassRaw] = []
            for rnd in range(cfg.rounds):
                pr = await run_pass(cfg, level, rnd, pacer, clients, registry, r)
                passes.append(pr)
                all_rids.extend(c.rid for c in pr.cycles if c.rid)
            lr = summarize_level(cfg, level, passes)
            results.append(lr)
            _print_level(lr, cfg, gated)
            if lr.stable:
                max_stable = level
            elif first_unstable is None:
                first_unstable = lr
            if cfg.stop_on_fail and not lr.stable:
                print(f"[stop] {level} 档不稳定，按 --stop-on-fail 结束。")
                break

        metrics_after = await _fetch_json(http_client, f"{cfg.base_url}/metrics")
        metrics_delta = _counter_delta(metrics_before, metrics_after)
        final_queues = _stream_lengths(metrics_after)
        final_pending = await _fetch_pending(cfg, r)

        _print_summary(
            cfg,
            results,
            max_stable,
            first_unstable,
            metrics_delta,
            final_queues,
            final_pending,
            reader_stats,
            gated,
        )

        if run_reader:
            assert r is not None
            if reader_pool:
                await _stop_readers(reader_pool)
                reader_pool = []
            print()
            print("=" * 78)
            print("reader 侧：R 个并发 reader 消费一批已排队的请求")
            print("=" * 78)
            print("drain=reader 启动到全部完成(s)；req/s=完成吞吐；msg/s=dispatcher 消费的消息吞吐")
            print("err=dispatch handler_error；pend=结束时未 ACK 总数；回传条数见每档明细")
            print(READER_TABLE_HEADER)
            print("-" * len(READER_TABLE_HEADER))
            for readers in cfg.reader_levels:
                rl = await run_reader_level(
                    cfg, readers, pacer, clients, registry, r, http_client, reader_stats
                )
                reader_results.append(rl)
                _print_reader_level(rl)
                if rl.stable:
                    reader_max_stable = readers
                elif reader_first_unstable is None:
                    reader_first_unstable = rl
                if cfg.stop_on_fail and not rl.stable:
                    print(f"[stop] readers={readers} 档不稳定，按 --stop-on-fail 结束。")
                    break
            _print_reader_summary(
                cfg, reader_results, reader_max_stable, reader_first_unstable
            )

        if run_mixed:
            assert r is not None
            if reader_pool:
                await _stop_readers(reader_pool)
                reader_pool = []
            print()
            print("=" * 78)
            print("mixed：科研 agent 与 reader 同时工作（N 个长轮询 × R 个 reader）")
            print("=" * 78)
            print("readers=R 个并发 reader；conc=N 个科研 agent 同时提交并等待")
            print(MIXED_TABLE_HEADER)
            print("-" * len(MIXED_TABLE_HEADER))
            stop_mixed = False
            for readers in cfg.mixed_reader_levels:
                phase_stop = asyncio.Event()
                pool = _spawn_readers(
                    cfg, r, readers, registry, reader_stats, phase_stop, batch=1
                )
                await asyncio.sleep(0.05)
                try:
                    for n in cfg.mixed_levels:
                        passes: list[PassRaw] = []
                        for rnd in range(cfg.rounds):
                            pr = await run_pass(cfg, n, rnd, pacer, clients, registry, r)
                            passes.append(pr)
                            all_rids.extend(c.rid for c in pr.cycles if c.rid)
                        lr = summarize_level(cfg, n, passes)
                        mixed_rows.append((readers, lr))
                        _print_mixed_row(readers, lr, gated)
                        if cfg.stop_on_fail and not lr.stable:
                            print(
                                f"[stop] readers={readers} conc={n} 不稳定，"
                                "按 --stop-on-fail 结束。"
                            )
                            stop_mixed = True
                            break
                finally:
                    await _stop_readers(pool)
                if stop_mixed:
                    break
            _print_mixed_summary(mixed_rows)

        # reader/mixed 阶段结束后重新取一次全局指标，供 JSON 与最终 pending 使用
        if run_reader or run_mixed:
            metrics_after = await _fetch_json(http_client, f"{cfg.base_url}/metrics")
            metrics_delta = _counter_delta(metrics_before, metrics_after)
            final_queues = _stream_lengths(metrics_after)
            final_pending = await _fetch_pending(cfg, r)

        if cfg.json_out:
            payload = {
                "device": device,
                "config": {
                    "mode": cfg.mode,
                    "base_url": cfg.base_url,
                    "levels": cfg.levels,
                    "rounds": cfg.rounds,
                    "intent": cfg.intent,
                    "intents": cfg.intents,
                    "paper_id": cfg.paper_id,
                    "transfer_key_used": bool(cfg.transfer_key),
                    "payload_extra_keys": sorted(cfg.payload_extra),
                    "slo": cfg.slo,
                    "p99_budget_s": cfg.p99_budget,
                    "agent_ids": cfg.agent_ids,
                    "mock_reader": cfg.mock_reader,
                    "reader_workers": cfg.reader_workers,
                    "reader_delay_ms": cfg.reader_delay_ms,
                    "reader_levels": cfg.reader_levels,
                    "reader_requests": cfg.reader_requests,
                    "reader_messages_per_request": cfg.reader_messages_per_request,
                    "mixed_levels": cfg.mixed_levels,
                    "mixed_reader_levels": cfg.mixed_reader_levels,
                    "gate": cfg.gate,
                    "run_id": cfg.run_id,
                },
                "levels": [asdict(lr) for lr in results],
                "max_stable": max_stable,
                "first_unstable": asdict(first_unstable) if first_unstable else None,
                "reader_levels": [asdict(x) for x in reader_results],
                "reader_max_stable": reader_max_stable,
                "reader_first_unstable": asdict(reader_first_unstable) if reader_first_unstable else None,
                "mixed_levels": [{"readers": R, **asdict(lr)} for R, lr in mixed_rows],
                "metrics_delta": metrics_delta,
                "final_stream_lengths": final_queues,
                "final_pending": final_pending,
                "reader_stats": dict(reader_stats),
            }
            pathlib.Path(cfg.json_out).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\n明细已写入: {cfg.json_out}")

        if cfg.fail_under and run_scientific and max_stable < cfg.fail_under:
            print(f"\n[fail] 最大稳定并发 {max_stable or 0} < --fail-under {cfg.fail_under}")
            return 2
        if cfg.fail_under_readers and run_reader and reader_max_stable < cfg.fail_under_readers:
            print(
                f"\n[fail] 最大稳定 reader 并发 {reader_max_stable or 0} "
                f"< --fail-under-readers {cfg.fail_under_readers}"
            )
            return 2
        return 0
    finally:
        stop.set()
        await _stop_readers(reader_pool)
        _close_recorder()
        for client in clients:
            with contextlib.suppress(Exception):
                await client.aclose()
        if cfg.cleanup and r is not None and all_rids:
            await _cleanup(cfg, r, all_rids)
        if r is not None:
            with contextlib.suppress(Exception):
                await r.aclose()
        await http_client.aclose()


def _stream_lengths(metrics: dict | None) -> dict[str, int]:
    queues = (metrics or {}).get("queues")
    return {str(k): int(v) for k, v in (queues or {}).items()}


async def _cleanup(cfg: Config, r: aioredis.Redis, rids: list[str]) -> None:
    keys: list[str] = []
    for rid in dict.fromkeys(rids):
        keys.extend(
            [
                f"agent:req:{rid}",
                f"agent:reply:{rid}",
                f"agent:waiter:{rid}",
                f"agent:submitted:{rid}",
                f"agent:cancel:{rid}",
            ]
        )
    for i in range(0, len(keys), 500):
        with contextlib.suppress(Exception):
            await r.unlink(*keys[i : i + 500])
    print(f"[cleanup] 已删除 {len(keys)} 个请求相关 key")


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def build_config(ns: argparse.Namespace) -> Config:
    levels = sorted({int(x) for x in str(ns.levels).split(",") if x.strip()})
    if not levels or any(lv <= 0 for lv in levels):
        raise SystemExit("--levels 需要逗号分隔的正整数，例如 1,8,32,64")
    reader_levels = sorted({int(x) for x in str(ns.reader_levels).split(",") if x.strip()})
    if not reader_levels or any(lv <= 0 for lv in reader_levels):
        raise SystemExit("--reader-levels 需要逗号分隔的正整数，例如 8,16,32,64")
    mixed_levels = sorted({int(x) for x in str(ns.mixed_levels).split(",") if x.strip()})
    mixed_reader_levels = sorted(
        {int(x) for x in str(ns.mixed_reader_levels).split(",") if x.strip()}
    )
    if not mixed_levels or any(lv <= 0 for lv in mixed_levels):
        raise SystemExit("--mixed-levels 需要逗号分隔的正整数，例如 8,32,64")
    if not mixed_reader_levels or any(lv <= 0 for lv in mixed_reader_levels):
        raise SystemExit("--mixed-reader-levels 需要逗号分隔的正整数，例如 8,16,32")
    if ns.mode not in ("scientific", "reader", "mixed", "both"):
        raise SystemExit("--mode 只能是 scientific / reader / mixed / both")
    agent_ids = [s.strip() for s in str(ns.agent_ids).split(",") if s.strip()] or ["research-agent-v1"]
    if ns.rounds < 1:
        raise SystemExit("--rounds 至少为 1")
    if ns.mock_reader and ns.reader_workers < 1:
        raise SystemExit("--reader-workers 至少为 1（或用 --no-mock-reader）")
    if ns.mode in ("reader", "mixed", "both") and not ns.mock_reader:
        raise SystemExit("reader/mixed 模式需要 mock reader；去掉 --no-mock-reader，或改用 --mode scientific")
    if ns.reader_requests < 1:
        raise SystemExit("--reader-requests 至少为 1")
    if ns.reader_messages_per_request < 1:
        raise SystemExit("--reader-messages-per-request 至少为 1")
    if ns.submit_concurrency < 1:
        raise SystemExit("--submit-concurrency 至少为 1")

    intents = [s.strip() for s in str(ns.intents or "").split(",") if s.strip()] or [ns.intent]
    try:
        payload_extra = json.loads(ns.payload_json) if ns.payload_json else {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--payload-json 不是合法 JSON：{exc}")
    if not isinstance(payload_extra, dict):
        raise SystemExit("--payload-json 必须是 JSON 对象")

    max_conn = ns.max_connections or max(
        64,
        max(levels) * 2,
        max(reader_levels),
        max(mixed_levels),
        max(mixed_reader_levels),
        ns.submit_concurrency + 8,
    )
    return Config(
        base_url=ns.base_url.rstrip("/"),
        redis_url=ns.redis_url,
        agent_ids=agent_ids,
        api_key=ns.api_key or "",
        secret=ns.secret or None,
        levels=levels,
        rounds=ns.rounds,
        intent=ns.intent,
        intents=intents,
        transfer_key=ns.transfer_key or None,
        paper_id=ns.paper_id or None,
        payload_extra=payload_extra,
        request_timeout=ns.request_timeout,
        wait_timeout=ns.wait_timeout,
        poll_window=ns.poll_window,
        http_timeout=ns.http_timeout,
        warmup_timeout=ns.warmup_timeout,
        slo=ns.slo,
        p99_budget=ns.p99_budget,
        stop_on_fail=ns.stop_on_fail,
        submit_qps=ns.submit_qps,
        retry_throttled=ns.retry_throttled,
        level_timeout=ns.level_timeout,
        barrier_timeout=ns.barrier_timeout,
        mock_reader=ns.mock_reader,
        gate=ns.gate,
        gate_timeout=ns.gate_timeout,
        reader_workers=ns.reader_workers,
        reader_batch=ns.reader_batch,
        reader_delay_ms=ns.reader_delay_ms,
        reader_jitter=ns.reader_jitter,
        reader_error_rate=ns.reader_error_rate,
        reader_progress_ratio=ns.reader_progress_ratio,
        reader_require_waiter=ns.reader_require_waiter,
        reader_messages_per_request=ns.reader_messages_per_request,
        submit_concurrency=ns.submit_concurrency,
        mode=ns.mode,
        server_spec=ns.server_spec or "",
        reader_levels=reader_levels,
        reader_requests=ns.reader_requests,
        reader_timeout=ns.reader_timeout,
        fail_under_readers=ns.fail_under_readers,
        mixed_levels=mixed_levels,
        mixed_reader_levels=mixed_reader_levels,
        stream_in_raw=ns.stream_in_raw,
        stream_in=ns.stream_in,
        stream_out=ns.stream_out,
        reader_group=ns.reader_group,
        stream_maxlen=ns.stream_maxlen,
        max_connections=max_conn,
        cleanup=ns.cleanup,
        json_out=ns.json_out,
        record_jsonl=ns.record_jsonl or None,
        record_max_chars=ns.record_max_chars,
        fail_under=ns.fail_under,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="agent_comm_service 并发压力测试（模拟科研 agent + reader agent）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--base-url", default=os.getenv("STRESS_BASE_URL", "http://127.0.0.1:9500"))
    p.add_argument("--redis-url", default=os.getenv("STRESS_REDIS_URL", "redis://127.0.0.1:6379/0"))
    p.add_argument("--agent-ids", default="research-agent-v1", help="逗号分隔；多 agent 可分摊配额")
    p.add_argument("--api-key", default=os.getenv("STRESS_API_KEY", ""), help="为空时尝试从 AGENT_SECRETS 推断")
    p.add_argument("--secret", default="", help="REQUIRE_SIGNATURE=1 时使用的 HMAC 密钥")
    p.add_argument("--mode", choices=("scientific", "reader", "mixed", "both"), default="scientific",
                   help="scientific=科研侧长轮询；reader=reader 侧积压消费；"
                        "mixed=N 个科研 agent × R 个 reader 同时工作；both=两段先后跑")
    p.add_argument("--levels", default="1,4,8,16,32,64,128", help="科研侧并发档位（同时挂起的长轮询数）")
    p.add_argument("--rounds", type=int, default=2, help="每个档位重复轮数，用于累积样本")
    p.add_argument("--reader-levels", default="8,16,32,64", help="reader 侧档位（并发 reader 数）")
    p.add_argument("--reader-requests", type=int, default=256, help="reader 侧每个档位排队的请求数")
    p.add_argument("--reader-messages-per-request", type=int, default=1,
                   help="每个请求回传几条消息：1=final，2=ack+final，>=3 再加 progress")
    p.add_argument("--reader-timeout", type=float, default=120.0, help="reader 侧等待全部完成的超时（秒）")
    p.add_argument("--submit-concurrency", type=int, default=64, help="reader 模式批量提交的并发上限")
    p.add_argument("--mixed-levels", default="8,32,64", help="mixed 模式的科研侧并发 N")
    p.add_argument("--mixed-reader-levels", default="8,16,32", help="mixed 模式的 reader 并发 R")
    p.add_argument("--server-spec", default="",
                   help="服务端机器规格备注（服务与压测机不同机时填写，如 '16C/64G'）")
    p.add_argument("--intent", default="paper.insight.validate")
    p.add_argument("--intents", default="", help="逗号分隔多个 intent，按 worker 轮询混用；缺省用 --intent")
    p.add_argument("--transfer-key", default="", help="通信服务 transfer_key（tk:v2...），放进请求信封")
    p.add_argument("--paper-id", default="", help="payload.paper_id（真实 reader 用来定位论文）")
    p.add_argument(
        "--payload-json",
        default="",
        help='额外 payload 字段（JSON），如 \'{"transfer_key":"ptk_..."}\'',
    )
    p.add_argument("--request-timeout", type=int, default=180, help="请求信封里的 timeout_s")
    p.add_argument("--wait-timeout", type=float, default=60.0, help="客户端等待上限")
    p.add_argument("--poll-window", type=int, default=25, help="单次长轮询挂起秒数")
    p.add_argument("--http-timeout", type=float, default=60.0)
    p.add_argument("--warmup-timeout", type=float, default=30.0)
    p.add_argument("--slo", type=float, default=0.99, help="成功率阈值")
    p.add_argument("--p99-budget", type=float, default=10.0, help="等待 p99 预算（秒），<=0 表示不检查")
    p.add_argument("--stop-on-fail", action="store_true", help="遇到首个不稳定档位即停止")
    p.add_argument("--submit-qps", type=float, default=0.0, help="全局提交限速；0 表示不限速")
    p.add_argument("--retry-throttled", type=int, default=20, help="429 最大重试次数")
    p.add_argument("--level-timeout", type=float, default=240.0, help="单阶段安全超时（秒）")
    p.add_argument("--barrier-timeout", type=float, default=15.0, help="等待 waiter 全部登记的超时（秒）")
    p.add_argument("--no-mock-reader", dest="mock_reader", action="store_false", help="不启动 mock reader")
    p.add_argument("--no-gate", dest="gate", action="store_false", help="不放闸门，直接流水线处理")
    p.add_argument("--gate-timeout", type=float, default=120.0, help="reader 端闸门最长等待（秒）")
    p.add_argument("--reader-workers", type=int, default=32)
    p.add_argument("--reader-batch", type=int, default=16)
    p.add_argument("--reader-delay-ms", type=float, default=200.0)
    p.add_argument("--reader-jitter", type=float, default=0.3)
    p.add_argument("--reader-error-rate", type=float, default=0.0)
    p.add_argument("--reader-progress-ratio", type=float, default=0.0)
    p.add_argument("--reader-require-waiter", action="store_true", help="没有 waiter 就跳过处理")
    p.add_argument("--stream-in-raw", default=DEFAULT_STREAM_IN_RAW)
    p.add_argument("--stream-in", default=DEFAULT_STREAM_IN)
    p.add_argument("--stream-out", default=DEFAULT_STREAM_OUT)
    p.add_argument("--reader-group", default=DEFAULT_READER_GROUP)
    p.add_argument("--stream-maxlen", type=int, default=10000)
    p.add_argument("--max-connections", type=int, default=0, help="客户端连接池上限，0=按最大档位自动")
    p.add_argument("--cleanup", action="store_true", help="结束后删除本次请求的 Redis key")
    p.add_argument("--json-out", default="", help="把明细写入 JSON 文件")
    p.add_argument(
        "--record-jsonl",
        default="",
        help="把每条请求的 reader 回复写成 JSONL（逐条落盘，便于事后排查）",
    )
    p.add_argument(
        "--record-max-chars",
        type=int,
        default=8000,
        help="单条回复保留的最大字符数，0=不截断",
    )
    p.add_argument("--fail-under", type=int, default=0, help="科研侧最大稳定并发低于该值时退出码为 2")
    p.add_argument("--fail-under-readers", type=int, default=0, help="reader 侧最大稳定并发低于该值时退出码为 2")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    cfg = build_config(parse_args(argv))
    try:
        return asyncio.run(run(cfg))
    except KeyboardInterrupt:
        print("\n[abort] 收到 Ctrl-C，已停止")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
