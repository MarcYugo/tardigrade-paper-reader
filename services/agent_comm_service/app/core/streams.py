"""Redis 键命名与流操作封装。"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import time
from typing import Any

import redis.asyncio as aioredis

from env_load import (
    DLQ_MAXLEN,
    REPLY_MAXLEN,
    REPLY_TTL,
    REQUEST_TTL,
    RESULT_COMPRESS_KB,
    RESULT_TTL,
)


# --------------------------------------------------------------------------- #
# key 命名
# --------------------------------------------------------------------------- #
def request_key(rid: str) -> str:
    return f"agent:req:{rid}"


def reply_stream(rid: str) -> str:
    return f"agent:reply:{rid}"


def waiter_key(rid: str) -> str:
    return f"agent:waiter:{rid}"


def cancel_key(rid: str) -> str:
    return f"agent:cancel:{rid}"


def submitted_key(rid: str) -> str:
    return f"agent:submitted:{rid}"


def result_key(rid: str) -> str:
    """完整结果（超出单条回包上限的 JSON）存放处；reader agent 也写这个键。"""
    return f"agent:result:{rid}"


def nonce_key(agent_id: str, nonce: str) -> str:
    return f"agent:nonce:{agent_id}:{nonce}"


def quota_key(agent_id: str, bucket: str) -> str:
    return f"agent:quota:{agent_id}:{bucket}"


# --------------------------------------------------------------------------- #
# 消费组
# --------------------------------------------------------------------------- #
async def ensure_group(r: aioredis.Redis, stream: str, group: str) -> None:
    try:
        await r.xgroup_create(stream, group, id="0", mkstream=True)
    except aioredis.ResponseError as exc:  # 已存在
        if "BUSYGROUP" not in str(exc):
            raise


# --------------------------------------------------------------------------- #
# 死信 / 回传
# --------------------------------------------------------------------------- #
async def to_dlq(
    r: aioredis.Redis,
    stream: str,
    *,
    source: str,
    entry_id: str,
    reason: str,
    raw: str,
) -> None:
    await r.xadd(
        stream,
        {"source": source, "entry_id": entry_id, "reason": reason, "raw": (raw or "")[:8000]},
        maxlen=DLQ_MAXLEN,
        approximate=True,
    )


async def append_reply(r: aioredis.Redis, rid: str, kind: str, data: Any) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    msg_id = await r.xadd(
        reply_stream(rid),
        {"type": kind, "data": payload},
        maxlen=REPLY_MAXLEN,
        approximate=True,
    )
    await r.expire(reply_stream(rid), REPLY_TTL)
    return msg_id


def _maybe_json(raw: Any) -> Any:
    if raw is None or isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return raw


async def read_reply(
    r: aioredis.Redis,
    rid: str,
    from_id: str = "0-0",
    *,
    count: int = 200,
    block: int | None = None,
) -> list[dict[str, Any]]:
    resp = await r.xread({reply_stream(rid): from_id}, count=count, block=block)
    items: list[dict[str, Any]] = []
    for _stream, entries in resp or []:
        for msg_id, fields in entries:
            items.append(
                {
                    "id": msg_id,
                    "type": fields.get("type", "final"),
                    "data": _maybe_json(fields.get("data", "")),
                }
            )
    return items


# --------------------------------------------------------------------------- #
# 完整结果（agent:result:{rid}）
#
# Redis 流里的单条 data 限 MAX_INLINE_KB（默认 64KB），完整笔记导出很容易超；
# 于是「大结果进 key、消息里只放摘要 + 引用」，客户端再 GET /v1/requests/{rid}/result。
# 大结果用 gzip+base64 存（decode_responses=True 下必须是纯文本，不能塞二进制）。
# --------------------------------------------------------------------------- #
def _encode_result(data: Any) -> tuple[str, str, bytes]:
    """序列化 + 必要时压缩，返回 `(待存的字符串, 编码名, 原始字节)`。"""
    raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    if len(raw) >= max(1, RESULT_COMPRESS_KB) * 1024:
        blob = base64.b64encode(gzip.compress(raw, compresslevel=6)).decode("ascii")
        return blob, "gzip+base64", raw
    return raw.decode("utf-8"), "json", raw


async def put_result(
    r: aioredis.Redis, rid: str, data: Any, *, ttl: int | None = None
) -> dict[str, Any]:
    """把完整结果写进 `agent:result:{rid}`，并把元信息挂到请求 hash 上。

    返回 `{key, url, bytes, stored_bytes, encoding, sha256, ttl}`，可直接嵌进回包。
    """
    blob, encoding, raw = _encode_result(data)
    ttl = int(ttl or RESULT_TTL)
    meta = {
        "key": result_key(rid),
        "url": f"/v1/requests/{rid}/result",
        "bytes": len(raw),
        "stored_bytes": len(blob.encode("utf-8")),
        "encoding": encoding,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "ttl": ttl,
    }
    fields = {
        "result_bytes": str(meta["bytes"]),
        "result_stored_bytes": str(meta["stored_bytes"]),
        "result_encoding": encoding,
        "result_sha256": meta["sha256"],
        "result_at": str(int(time.time())),
        "result_ttl": str(ttl),
    }
    pipe = r.pipeline(transaction=False)
    pipe.set(result_key(rid), blob, ex=ttl)
    pipe.hset(request_key(rid), mapping=fields)
    pipe.expire(request_key(rid), max(ttl, REQUEST_TTL))
    await pipe.execute()
    return meta


async def result_meta(r: aioredis.Redis, rid: str) -> dict[str, Any]:
    """只读元信息（不取正文），供 `?meta=true` 与 `ingest.context()` 使用。"""
    data = await r.hgetall(request_key(rid))
    size = int(data.get("result_bytes") or 0)
    if not size:
        return {"available": False, "key": result_key(rid),
                "url": f"/v1/requests/{rid}/result"}
    return {
        "available": True,
        "key": result_key(rid),
        "url": f"/v1/requests/{rid}/result",
        "bytes": size,
        "stored_bytes": int(data.get("result_stored_bytes") or 0),
        "encoding": data.get("result_encoding") or "json",
        "sha256": data.get("result_sha256") or "",
        "at": int(data.get("result_at") or 0),
        "ttl": int(data.get("result_ttl") or RESULT_TTL),
    }


async def get_result(
    r: aioredis.Redis, rid: str, *, purge: bool = False
) -> tuple[Any | None, dict[str, Any]]:
    """取回完整结果；不存在/已过期返回 `(None, meta)`。

    `purge=True` 时**读完即删**（`agent:result:{rid}` 与请求 hash 上的结果元信息），
    用于「确认收到就不必再留」的调用方，避免大结果在 Redis 里占到 TTL 过期；
    默认 `purge=False`，仍由 `RESULT_TTL` 过期回收（可重复取回 / 多读者）。
    """
    meta = await result_meta(r, rid)
    blob = await r.get(result_key(rid))
    if blob is None:
        return None, {**meta, "available": False}
    if isinstance(blob, (bytes, bytearray)):
        blob = blob.decode("utf-8")
    encoding = str(meta.get("encoding") or "json")
    try:
        raw = (gzip.decompress(base64.b64decode(blob))
               if encoding.startswith("gzip") else blob.encode("utf-8"))
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:                     # 内容损坏：当作不可用，别抛栈
        if purge:
            await drop_result(r, rid)
        return None, {**meta, "available": False, "error": f"decode:{exc}"}
    if purge:
        await drop_result(r, rid)
    return data, {**meta, "purged": bool(purge)}


async def drop_result(r: aioredis.Redis, rid: str) -> None:
    pipe = r.pipeline(transaction=False)
    pipe.delete(result_key(rid))
    pipe.hdel(
        request_key(rid),
        "result_bytes", "result_stored_bytes", "result_encoding",
        "result_sha256", "result_at", "result_ttl",
    )
    await pipe.execute()
