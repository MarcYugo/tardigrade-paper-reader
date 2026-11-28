"""通信桥命令行：消费 `agent_comm_service` 的 Redis Streams，按内容处理并回包。

**应用已自动托管**：`backend/server/lifecycle.py` 在 FastAPI `startup` 时会起一个后台线程，
循环探测通信服务 `/healthz` —— **服务在才轮询**，不在就等着（`AGENT_COMM_AUTO=0` 关闭）。
所以正常情况下不需要跑本命令行；这里的入口更多用于体检 / 手工处理 / 排查：

    python tools/comm_worker.py --status      # 体检：服务 /healthz + Redis 流与积压
    python tools/comm_worker.py --once        # 取一批消息处理完就退出
    python tools/comm_worker.py               # 前台常驻（Ctrl+C 停止）
    python tools/comm_worker.py --inspect '{"intent":"paper.note.list","payload":{}}'

连接参数（环境变量或 `.env`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AGENT_COMM_AUTO` | `1` | 应用启动时是否自动托管通信桥（`0` = 不轮询） |
| `AGENT_COMM_PROBE_S` | `15` | 多久重新探测一次通信服务 |
| `AGENT_COMM_URL` | 依次探测 `127.0.0.1:8090` / `:9500` | 通信服务地址（仅健康检查用） |
| `AGENT_COMM_REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis 连接串（compose 开了 `requirepass` 后要带口令：`redis://:密码@127.0.0.1:6379/0`） |
| `AGENT_COMM_STREAM_IN` | `outside-in-stream` | 待处理消息流（消费组 `outside-in-workers`） |
| `AGENT_COMM_STREAM_OUT` | `inside-out-stream` | 结果回写流（dispatcher 落到 `agent:reply:{rid}`） |
| `AGENT_COMM_STAMP_SECRET` | 空 | 网关盖章密钥；留空则自动读 MongoDB `agent_gateway_meta` |
| `AGENT_COMM_STAMP_WINDOW` | `3600` | 盖章有效期（秒），与通信服务 `STAMP_WINDOW` 对齐 |
| `AGENT_COMM_ALLOW_UNSTAMPED` | `0` | **仅本地调试**：允许未盖章消息，退回旧账号推断 |
| `AGENT_COMM_USER` | 空 | 旧模型的兜底账号（仅 `AGENT_COMM_ALLOW_UNSTAMPED=1` 时生效） |

> 新信任模型：**归属账号只认通信服务的盖章**（信封 `owner` + `auth.mac`）。本进程不再解析
> `ptk_`、不看 `payload.user`、也不做“单账号自动”；未盖章 / 验章失败一律报错回包。
> 盖章密钥从 `agent_gateway_meta` 自动读取，所以无需手工同步（除非显式设置了
> `AGENT_COMM_STAMP_SECRET`，那必须与通信服务一致）。
>
> 回包方向：`send_reply()` 会给每条消息带上 `owner` 与 `sig`（HMAC，同一把盖章密钥），
> 通信服务 dispatcher 验不过就丢进 DLQ。所以本进程必须能读到密钥，否则只能回未签名消息。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from backend.agents.skills.agent_comm import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
