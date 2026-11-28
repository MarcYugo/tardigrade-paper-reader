"""端到端冒烟（需要可用的 Redis）：

    python tests/test_flow.py

流程：提交 -> 深检 -> 干净流 -> 模拟 reader agent 回传完整字典 -> 读回结果。
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
from app.core.streams import read_reply, reply_stream, request_key  # noqa: E402
from env_load import REDIS_URL, STREAM_IN, STREAM_IN_RAW, STREAM_OUT  # noqa: E402


async def main() -> int:
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        await r.ping()
    except Exception as exc:
        print(f"skip: Redis 不可用（{exc!r}）")
        return 0

    env = RequestEnvelope(
        from_agent="research-agent-v1",
        intent="paper.insight.validate",
        payload={"claim": "多头注意力等价于加权特征融合"},
        timeout_s=60,
    )
    rid, created, status = await ingest.submit_request(r, env)
    assert created and status.value == "queued", (created, status)
    print(f"[1] submitted {rid}")

    raw_fields = None
    # 从最新往前找：流里可能残留着压测数据（XRANGE 只扫最早的 N 条会漏）
    for _entry_id, fields in await r.xrevrange(STREAM_IN_RAW, count=2000):
        if fields.get("request_id") == rid:
            raw_fields = fields
            break
    assert raw_fields, "raw 流里找不到该请求"
    await dispatcher.handle_raw(r, "0-0", raw_fields)
    print("[2] guard 深检完成")

    hit = any(
        fields.get("request_id") == rid
        for _e, fields in await r.xrevrange(STREAM_IN, count=2000)
    )
    assert hit, "未进入 outside-in-stream"
    print("[3] 已进入 outside-in-stream")

    payload = json.dumps({"answer": 42, "note": "完整字典"}, ensure_ascii=False)
    acct = None
    if os.environ.get("AUTH_STAMP_SECRET"):
        stamp.set_secret(os.environ["AUTH_STAMP_SECRET"])
    else:
        acct = accounts.build_store()
        await acct.connect()
        await stamp.ensure_secret(acct)
    fields = {"request_id": rid, "kind": "final", "data": payload,
              "sig": stamp.sign_reply(request_id=rid, kind="final", owner="",
                                      data=payload)}
    out_id = await r.xadd(STREAM_OUT, fields)
    await dispatcher.handle_out(r, out_id, fields)
    if acct is not None:
        await acct.close()
    print("[4] 模拟 reader agent 回传 final")

    items = await read_reply(r, rid, "0-0")
    assert items and items[-1]["type"] == "final", items
    assert items[-1]["data"]["answer"] == 42, items
    print(f"[5] 长轮询可读到结果：{items[-1]['data']}")

    ctx = await ingest.context(r, rid)
    assert ctx and ctx["status"] == "done", ctx
    print("[6] 状态 = done")

    await r.delete(request_key(rid), reply_stream(rid))
    await r.xdel(STREAM_OUT, out_id)
    await r.aclose()
    print("flow ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
