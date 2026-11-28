"""完整结果 blob：超大回包 -> agent:result:{rid} -> 摘要 + 引用 -> 读回完整内容。

    python tests/test_result_blob.py

覆盖三件事：
  1. reader 直接往 `inside-out-stream` 写超过 64KB 的 `final`，dispatcher 不再报错，
     而是把完整 JSON 存进 `agent:result:{rid}`；
  2. 回传流里只有「摘要 + result_ref」，体积 < 64KB；
  3. `get_result()` / `result_meta()` / `ingest.context()` 都能看到完整结果。

需要可用的 Redis（与 `env_load.REDIS_URL` 一致）；连不上时打印 skip 并返回 0。
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import redis.asyncio as aioredis  # noqa: E402

from app.core import accounts, dispatcher, ingest, stamp  # noqa: E402
from app.core.models import RequestEnvelope  # noqa: E402
from app.core.streams import (  # noqa: E402
    drop_result,
    get_result,
    read_reply,
    reply_stream,
    request_key,
    result_key,
    result_meta,
)
from env_load import REDIS_URL, STREAM_OUT  # noqa: E402


def big_export(doc_id: str = "d1") -> dict:
    """造一个明显超过 64KB 的导出（300 条笔记 + 4 万字正文）。"""
    return {
        "ok": True,
        "action": "export_notes",
        "owner": "alice",
        "doc_id": doc_id,
        "paper": {"id": doc_id, "title": "Big Paper", "num_pages": 14},
        "format": "tardigrade-note-export",
        "version": 1,
        "notes": {
            "sentence_notes": [
                {"id": f"n{i}", "page": i % 14 + 1, "sentence": "原文" * 20,
                 "note": "笔记" * 100}
                for i in range(300)
            ],
            "free_notes": [],
            "highlights": [],
            "stats": {"sentence_notes": 300, "notes_total": 300},
        },
        "sections": {
            "abstract": {"title": "abstract", "pages": [1], "chars": 1200,
                         "blocks": 2, "text": "摘要" * 600},
            "main_text": {"title": "main_text", "pages": [2, 3], "chars": 40000,
                          "blocks": 124, "text": "正文" * 20000},
        },
        "report": {"mode": "mechanical", "summary": "机械导出"},
        "ai_summary": None,
    }


async def main() -> int:
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        await r.ping()
    except Exception as exc:
        print(f"skip: Redis 不可用（{exc!r}）")
        return 0

    env = RequestEnvelope(
        from_agent="research-agent-v1",
        intent="paper.note.export",
        payload={"doc_id": "d1"},
        timeout_s=60,
    )
    rid, created, _status = await ingest.submit_request(r, env)
    assert created, "request_id 冲突"
    print(f"[1] submitted {rid}")

    full = big_export()
    data = json.dumps(full, ensure_ascii=False)
    size = len(data.encode())
    assert size > 64 * 1024, f"样本不够大：{size} 字节"
    print(f"[2] 模拟 reader 直接回传超限 final（{size} 字节）")

    fields = {"request_id": rid, "kind": "final", "data": data}
    # 回包必须签名（dispatcher 会验），密钥与通信服务同一份
    acct = None
    if os.environ.get("AUTH_STAMP_SECRET"):
        stamp.set_secret(os.environ["AUTH_STAMP_SECRET"])
    else:
        acct = accounts.build_store()
        await acct.connect()
        await stamp.ensure_secret(acct)
    fields["sig"] = stamp.sign_reply(request_id=rid, kind="final", owner="", data=data)
    out_id = await r.xadd(STREAM_OUT, fields)
    await dispatcher.handle_out(r, out_id, fields)
    if acct is not None:
        await acct.close()

    items = await read_reply(r, rid, "0-0")
    assert items, "回传流里没有消息"
    final = items[-1]
    assert final["type"] == "final", final
    slim = final["data"]
    slim_size = len(json.dumps(slim, ensure_ascii=False).encode())
    assert slim["truncated"] is True, slim
    assert slim["result_ref"] == result_key(rid), slim
    assert slim["result_bytes"] == size, slim
    assert slim_size <= 64 * 1024, f"摘要仍然超限：{slim_size}"
    print(f"[3] 回传流里只有摘要（{slim_size} 字节），引用 {slim['result_ref']}")

    stored, meta = await get_result(r, rid)
    assert stored is not None, meta
    assert stored["notes"]["sentence_notes"] == full["notes"]["sentence_notes"]
    assert stored["sections"]["main_text"]["text"] == full["sections"]["main_text"]["text"]
    print(f"[4] 取回完整结果：{meta['bytes']} 字节，encoding={meta['encoding']}")

    info = await result_meta(r, rid)
    assert info["available"] and info["bytes"] == meta["bytes"], info

    ctx = await ingest.context(r, rid)
    assert ctx and ctx["status"] == "done", ctx
    assert ctx["result_available"] and ctx["result_bytes"] == size, ctx
    assert ctx["result_url"] == f"/v1/requests/{rid}/result", ctx
    print("[5] 状态 = done，context 里能看到 result_available/result_bytes")

    await drop_result(r, rid)
    await r.delete(request_key(rid), reply_stream(rid))
    await r.xdel(STREAM_OUT, out_id)
    await r.aclose()
    print("result blob ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
