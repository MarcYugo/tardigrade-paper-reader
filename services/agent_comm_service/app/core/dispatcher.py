"""两个消费者：
  * guard  : outside-in-raw -> 深检 -> outside-in-stream（或 DLQ）
  * dispatch: inside-out-stream -> agent:reply:{rid}（或 DLQ）

另有 XAUTOCLAIM 巡检，认领消费者崩溃后残留的 pending（at-least-once）。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any, Awaitable, Callable

import redis.asyncio as aioredis
from redis.exceptions import TimeoutError as RedisTimeoutError

from env_load import (
    BLOCK_MS,
    MAX_INLINE_KB,
    READ_BATCH,
    RECLAIM_IDLE_MS,
    RECLAIM_INTERVAL_S,
    REQUEST_TTL,
    REQUIRE_READER_SIG,
    RESULT_MAX_MB,
    STREAM_DLQ_IN,
    STREAM_DLQ_OUT,
    STREAM_IN,
    STREAM_IN_RAW,
    STREAM_MAXLEN,
    WORKER_CONCURRENCY,
)
from . import guard, ingest, stamp
from .metrics import METRICS
from .models import KIND_TO_STATUS, RequestEnvelope
from .streams import append_reply, put_result, request_key, to_dlq

Handler = Callable[[aioredis.Redis, str, dict], Awaitable[None]]


# --------------------------------------------------------------------------- #
# guard：深检后转发到干净流
# --------------------------------------------------------------------------- #
async def handle_raw(r: aioredis.Redis, msg_id: str, fields: dict) -> None:
    raw = fields.get("envelope") or "{}"
    rid = (fields.get("request_id") or "").strip()
    try:
        env = RequestEnvelope.model_validate_json(raw)
    except Exception as exc:
        METRICS.inc("guard.schema_block")
        await to_dlq(r, STREAM_DLQ_IN, source=STREAM_IN_RAW, entry_id=msg_id, reason=f"schema:{exc}", raw=raw)
        if rid:
            await _mark(r, rid, "rejected", reason="schema_invalid")
        return

    verdict = await guard.deep_check(env)
    if verdict.ok:
        await r.xadd(STREAM_IN, fields, maxlen=STREAM_MAXLEN, approximate=True)
        METRICS.inc("guard.pass")
        return

    METRICS.inc(f"guard.{verdict.level.value}")
    await to_dlq(
        r,
        STREAM_DLQ_IN,
        source=STREAM_IN_RAW,
        entry_id=msg_id,
        reason=f"{verdict.reason}:{verdict.detail}",
        raw=raw,
    )
    status = "quarantined" if verdict.level is guard.Level.QUARANTINE else "rejected"
    await _mark(r, rid or env.request_id or "", status, reason=verdict.reason)


# --------------------------------------------------------------------------- #
# dispatch：把 reader agent 的结果落到请求专属回传流
# --------------------------------------------------------------------------- #
# 回包里的「正文型」大字段：超限时只留摘要，完整内容进 agent:result:{rid}
_HEAVY_KEYS = {"notes", "sections", "ai_summary", "blocks", "entries", "text"}


def _parse_json(raw: str) -> Any | None:
    try:
        return json.loads(raw)
    except Exception:
        return None


def _section_index(sec: dict) -> dict:
    """分节索引：标题 / 页码 / 字数 / 块数，丢掉正文。"""
    out: dict[str, Any] = {
        "title": sec.get("title"),
        "pages": sec.get("pages") or [],
    }
    text = sec.get("text")
    if isinstance(text, str):
        out["chars"] = len(text)
    elif isinstance(sec.get("chars"), int):
        out["chars"] = sec["chars"]
    blocks = sec.get("blocks")
    out["blocks"] = len(blocks) if isinstance(blocks, list) else int(blocks or 0)
    return out


def _small(value: Any, limit: int = 4096) -> bool:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode()) <= limit
    except Exception:
        return False


def slim_payload(data: Any, rid: str, meta: dict) -> dict:
    """把超大回包压成「摘要 + 引用」，完整内容由 `GET /v1/requests/{rid}/result` 提供。

    reader agent（如 `paper-trans-to-html/backend/agents/skills/agent_comm.py`）会自己
    先 spill 一次，这里兜底：任何 reader 直接往 `inside-out-stream` 写超限 data 时，
    也不再丢数据，而是同样落 `agent:result:{rid}`。
    """
    slim: dict[str, Any] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            if key in _HEAVY_KEYS:
                continue
            if _small(value):
                slim[key] = value
        notes = data.get("notes")
        if isinstance(notes, dict):
            slim["notes"] = {
                "stats": notes.get("stats") or {},
                "preview": {
                    key: list(notes.get(key) or [])[:3]
                    for key in ("sentence_notes", "free_notes", "highlights")
                },
            }
        elif isinstance(notes, list):
            slim["notes"] = notes[:3]
        sections = data.get("sections")
        if isinstance(sections, dict):
            slim["sections"] = {
                name: _section_index(sec)
                for name, sec in sections.items() if isinstance(sec, dict)
            }
    else:
        slim["content"] = str(data)[:2000]

    slim.update(
        {
            "truncated": True,
            "trimmed": True,
            "result_ref": meta["key"],
            "result_url": meta["url"],
            "result_bytes": meta["bytes"],
            "result_sha256": meta["sha256"],
            "result_encoding": meta["encoding"],
            "hint": (f"完整结果已存入通信服务 Redis（{meta['key']}，TTL {meta['ttl']}s）；"
                     f"请 GET {meta['url']}（SDK: fetch_result(request_id)）取回完整内容。"),
        }
    )
    return slim


def check_reply(fields: dict, ctx_owner: str | None) -> tuple[bool, str]:
    """校验 reader 回包：签名 + 归属（`REQUIRE_READER_SIG=0` 时跳过）。

    Redis 本身没有访问控制，能写 `inside-out-stream` 就能把任意 `final` 塞给调用方。
    所以回包必须带 `sig = HMAC(stamp_secret, reply|rid|kind|owner|sha256(data))`，
    且 `owner`（如带了）要与请求盖章的归属一致。
    """
    if not REQUIRE_READER_SIG:
        return True, ""
    rid = str(fields.get("request_id") or "").strip()
    kind = str(fields.get("kind") or "final").strip()
    data = fields.get("data") or ""
    owner = str(fields.get("owner") or "")
    mac = str(fields.get("sig") or "")
    if not mac:
        return False, "missing_reply_sig"
    if not stamp.configured():
        return False, "no_stamp_secret"
    ok, why = stamp.verify_reply(request_id=rid, kind=kind, owner=owner,
                                 data=data, mac=mac)
    if not ok:
        return False, why
    if owner and ctx_owner and owner != ctx_owner:
        return False, "reply_owner_mismatch"
    return True, ""


async def handle_out(r: aioredis.Redis, msg_id: str, fields: dict) -> None:
    rid = (fields.get("request_id") or "").strip()
    kind = (fields.get("kind") or "final").strip()
    data = fields.get("data") or ""

    if not rid or kind not in KIND_TO_STATUS:
        METRICS.inc("dispatch.poison")
        await to_dlq(
            r,
            STREAM_DLQ_OUT,
            source="inside-out-stream",
            entry_id=msg_id,
            reason=f"bad_message(rid={rid!r},kind={kind!r})",
            raw=str(fields)[:4000],
        )
        return

    # 回包必须由持有盖章密钥的 reader 签名（否则任何人写流都能投毒）
    ctx = await ingest.context(r, rid) or {}
    ok, why = check_reply(fields, ctx.get("owner"))
    if not ok:
        METRICS.inc(f"dispatch.reply_rejected")
        await to_dlq(
            r,
            STREAM_DLQ_OUT,
            source="inside-out-stream",
            entry_id=msg_id,
            reason=why,
            raw=str(fields)[:4000],
        )
        return

    if len(data.encode()) > MAX_INLINE_KB * 1024:
        await _spill_oversized(r, rid, kind, data)
        return

    await append_reply(r, rid, kind, data)
    await _mark(r, rid, KIND_TO_STATUS[kind].value)
    METRICS.inc(f"dispatch.{kind}")


async def _spill_oversized(r: aioredis.Redis, rid: str, kind: str, data: str) -> None:
    """超限回包：完整 JSON 存 `agent:result:{rid}`，回传流里只放摘要 + 引用。"""
    parsed = _parse_json(data)
    if parsed is None:
        METRICS.inc("dispatch.oversized")
        await append_reply(
            r, rid, "error",
            {"reason": "payload_too_large",
             "hint": "data 必须是 JSON；大结果请先存 agent:result:{rid} 再回引用"},
        )
        await _mark(r, rid, "error", reason="payload_too_large")
        return

    if len(data.encode()) > RESULT_MAX_MB * 1024 * 1024:
        METRICS.inc("dispatch.oversized")
        await append_reply(
            r, rid, "error",
            {"reason": "result_too_large",
             "hint": f"完整结果超过 {RESULT_MAX_MB}MB，请分段或用 transfer_key 回传"},
        )
        await _mark(r, rid, "error", reason="result_too_large")
        return

    try:
        meta = await put_result(r, rid, parsed)
    except Exception as exc:
        METRICS.inc("dispatch.result_store_failed")
        print(f"[dispatch] result store failed rid={rid}: {exc!r}", flush=True)
        await append_reply(
            r, rid, "error",
            {"reason": "result_store_failed", "detail": str(exc)[:300],
             "hint": "通信服务 Redis 写入失败，请检查 RESULT_MAX_MB / 可用内存"},
        )
        await _mark(r, rid, "error", reason="result_store_failed")
        return

    slim = slim_payload(parsed, rid, meta)
    body = json.dumps(slim, ensure_ascii=False)
    if len(body.encode()) > MAX_INLINE_KB * 1024:      # 极端情况：再收缩一次
        slim = {
            "truncated": True, "trimmed": True, "ok": slim.get("ok", True),
            "action": slim.get("action"), "result_ref": meta["key"],
            "result_url": meta["url"], "result_bytes": meta["bytes"],
            "result_sha256": meta["sha256"],
            "hint": slim.get("hint", ""),
        }

    await append_reply(r, rid, kind, slim)
    await _mark(r, rid, KIND_TO_STATUS[kind].value)
    METRICS.inc("dispatch.spilled")
    METRICS.inc(f"dispatch.{kind}")


# 终态只能向前：running 不能覆盖任何终态，避免并发 dispatch 把 done 回滚成 running
_MARK_SCRIPT = """
local cur = redis.call('HGET', KEYS[1], 'status') or ''
if ARGV[1] == 'running' and (cur == 'done' or cur == 'error' or cur == 'cancelled'
    or cur == 'quarantined' or cur == 'rejected') then
  return 0
end
redis.call('HSET', KEYS[1], 'status', ARGV[1])
if ARGV[2] ~= '' then
  redis.call('HSET', KEYS[1], 'reason', ARGV[2])
end
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""


async def _mark(r: aioredis.Redis, rid: str, status: str, *, reason: str = "") -> None:
    """单调更新请求状态：ack/progress 不得把已终态改回 running。

    同一请求的 ack/progress/final 会被并发处理，晚到的 running 不能覆盖 done，
    否则客户端按状态轮询时会永远等不到终态。
    """
    if not rid:
        return
    await r.eval(
        _MARK_SCRIPT,
        1,
        request_key(rid),
        status,
        reason or "",
        REQUEST_TTL,
    )


# --------------------------------------------------------------------------- #
# 通用消费循环（信号量背压，与 agents_redis_streams/apps/worker.py 同思路）
# --------------------------------------------------------------------------- #
async def run_consumer(
    r: aioredis.Redis,
    *,
    stream: str,
    group: str,
    consumer: str,
    handler: Handler,
    block_ms: int = BLOCK_MS,
    concurrency: int = WORKER_CONCURRENCY,
) -> None:
    sem = asyncio.Semaphore(max(1, concurrency))
    inflight: set[asyncio.Task[None]] = set()

    async def run_one(msg_id: str, fields: dict) -> None:
        try:
            await handler(r, msg_id, fields)
            with suppress(Exception):
                await r.xack(stream, group, msg_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 坏消息也 ACK，避免无限重投；内容已记日志/DLQ
            print(f"[{consumer}] handler error: {exc!r}", flush=True)
            METRICS.inc(f"{consumer}.handler_error")
            with suppress(Exception):
                await r.xack(stream, group, msg_id)
        finally:
            sem.release()

    print(f"[{consumer}] started stream={stream} group={group} conc={concurrency}", flush=True)
    try:
        while True:
            await sem.acquire()
            try:
                resp = await r.xreadgroup(
                    group, consumer, {stream: ">"}, count=READ_BATCH, block=block_ms
                )
            except RedisTimeoutError:
                sem.release()
                continue
            except asyncio.CancelledError:
                sem.release()
                raise
            except Exception as exc:
                sem.release()
                print(f"[{consumer}] xreadgroup error: {exc!r}", flush=True)
                await asyncio.sleep(1)
                continue

            entries = [(mid, f) for _s, items in (resp or []) for mid, f in items]
            if not entries:
                sem.release()
                continue

            for idx, (msg_id, fields) in enumerate(entries):
                if idx:                       # 第一条已在上面的 acquire 里占了槽
                    await sem.acquire()
                job = asyncio.create_task(run_one(msg_id, fields))
                inflight.add(job)
                job.add_done_callback(inflight.discard)
    finally:
        for job in list(inflight):
            job.cancel()
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)


# --------------------------------------------------------------------------- #
# 巡检：认领超时 pending
# --------------------------------------------------------------------------- #
async def run_reclaimer(
    r: aioredis.Redis,
    *,
    stream: str,
    group: str,
    consumer: str,
    handler: Handler,
    idle_ms: int = RECLAIM_IDLE_MS,
    interval_s: int = RECLAIM_INTERVAL_S,
) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            start = "0-0"
            while True:
                res = await r.xautoclaim(stream, group, consumer, idle_ms, start_id=start, count=16)
                if isinstance(res, (tuple, list)) and len(res) == 3:
                    start, entries, _deleted = res
                else:
                    start, entries = res  # type: ignore[misc]

                if not entries:
                    break
                for msg_id, fields in entries:
                    try:
                        await handler(r, msg_id, fields)
                        await r.xack(stream, group, msg_id)
                        METRICS.inc("reclaim.handled")
                    except Exception as exc:
                        print(f"[{consumer}] reclaim error: {exc!r}", flush=True)
                if start == "0-0":
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[{consumer}] reclaim loop error: {exc!r}", flush=True)
