## Agent 通信模块

本应用 reader agent 与外部 agent（科研 agent 等）之间的双向消息网关：Redis Streams 作持久化总线，HTTP + 长轮询作对外接口。异步实现，可多副本水平扩容。

### 1. 职责

- 外部 agent **只经本服务**进入，不直连 Redis。
- 服务在投递前完成鉴权、配额、恶意载荷检测；不通过的消息永不进入 reader agent 视野。
- **所有校验都在本服务完成**：账号归属只由账号 transfer key（`ptk_…`）决定，校验结果以
  「网关盖章」（信封 `owner` + `auth.mac`）随消息下发，reader 不再自己推断身份。
- reader agent 消费 `outside-in-stream`，把**完整结果字典**写入 `inside-out-stream`。
- 服务消费 `inside-out-stream`，按 `request_id` 落到该请求专属回传流，等待者取走。
- 记录"谁在等"（waiter），reader agent 开工前可预检，没人等就跳过，省算力。

### 2. 拓扑

```mermaid
flowchart LR
    A[科研 agent + sdk] -- POST /v1/requests --> B[Ingress API]
    B --> C[快检: 鉴权/配额/Schema/恶意模式]
    C --> D[(outside-in-raw)]
    D --> E[深检消费者]
    E -- pass --> F[(outside-in-stream)]
    E -. block/quarantine .-> G[(agent:dlq:in)]
    F --> H[reader agent]
    H --> I[(inside-out-stream)]
    I --> J[Dispatcher]
    J --> K[(agent:reply:id)]
    J -. 毒消息 .-> L[(agent:dlq:out)]
    A -- 长轮询 --> K
```

外部 agent、tardigrade reader agent 与通信模块的数据请求示意图（信任模型）：

```mermaid
flowchart TB
    subgraph EXT["① 外部 agent（不可信）"]
        A["科研 agent + SDK<br/>持 X-Api-Key + 账号 ptk_…"]
    end

    subgraph GW["② 通信模块 agent_comm_service —— 唯一信任边界 / 唯一准入决策点"]
        B["authenticate()<br/>X-Agent-Id + X-Api-Key"]
        C["fast_check()<br/>意图白名单 / 体积 / 恶意模式"]
        D["authorize()<br/>校验 ptk_ → 归属账号"]
        E["stamped_envelope()<br/>抹掉明文 key · 盖 owner + auth.mac"]
        F["ingest()<br/>幂等登记 + 投递 raw 流"]
        B --> C --> D --> E --> F
    end

    subgraph DATA["③ 数据面（持久化）"]
        M1[("paper.transfer_keys<br/>账号 → keys 数组（明文）")]
        M2[("paper.users<br/>账号是否仍存在")]
        M3[("agent_gateway_meta<br/>盖章密钥")]
        R1[("outside-in-raw / outside-in-stream<br/>只含 owner + auth，无明文 key")]
        R2[("inside-out-stream / agent:reply:rid")]
    end

    subgraph TARD["④ tardigrade reader（无准入权，只消费结果）"]
        H["agent_comm.py<br/>只读盖章 owner，不再自己推断身份"]
    end

    A -- "POST /v1/requests<br/>transfer_key = ptk_…" --> B
    D -- "按 key 反查账号" --> M1
    D -- "确认账号未注销" --> M2
    E -- "读取盖章密钥" --> M3
    F --> R1
    R1 --> H
    H -- "ack / final" --> R2
    R2 -- "GET /v1/requests/id/wait 长轮询" --> A

    B -. "401 传输层身份错<br/>422 客户端自带 owner / auth" .-> X["拒绝：消息不进流、不落 Redis"]
    D -. "403 无 key / key 无效 / 归属冲突" .-> X
```

三条不变量：

1. **准入只发生在 ②**：账号归属完全由 `ptk_…` 决定，`authorize()` 是唯一判断点；`payload` 里的
   `transfer_key` / `user` 不再具备任何“自证身份”的能力（冲突直接 403）。
2. **③ 里没有凭据**：明文 key 在 `stamped_envelope()` 就被抹掉，流里只有 `owner` + `auth.key_id`；
   盖章 `mac` 绑定 `request_id / from_agent / owner / key_id / intent / stamped_at`，改动即失效。
3. **④ 不再做判断**：reader 只读 `owner`（由网关归一化成 `payload.user`），不校验、不兜底、
   不猜账号；它看到的就是 ② 的结论。

> 已知边界：Redis 本身没有访问控制，能直连 Redis 的进程仍可伪造流消息——伪造的 `owner`
> 会被 `auth.mac` 判为 `bad_stamp_mac` / `stamp_owner_not_bound` 并进 DLQ，但要彻底堵住
> 需要在基础设施层给 Redis 加认证与网络隔离。

### 3. 消息契约

入站（外部 → 服务）：

```json
{"request_id": "可选幂等键", "from_agent": "research-agent-v1",
 "intent": "paper.insight.validate", "transfer_key": "ptk_...",
 "payload": {}, "wait": true, "timeout_s": 180}
```

`transfer_key` **必须是账号的 transfer key（`ptk_…`）**，即 tardigrade 应用「🔑 转交 Key」
里签发的那一把。网关校验通过后会：

* 抹掉明文 `transfer_key`（不再随信封进 Redis / 日志），只在 `auth.key_id` 留下审计 id；
* 盖上 `owner`（归属账号，权威值）与 `auth`（`via` / `key_id` / `from_agent` / `stamped_at` / `mac`）；
* 把 payload 归一化成 `payload.user = owner`，删掉 `payload.transfer_key` / `payload.username`。

> `owner` / `auth` 是**服务端字段**：客户端填任何值都会 422。旧的 `tk:v2` key 默认被拒
> （`ALLOW_ENV_TRANSFER_KEYS=0`），因为它由服务端的 pepper 派生、跟账号无关。

出站（reader agent → `inside-out-stream`）：

```json
{"request_id": "...", "kind": "ack|progress|final|error", "data": {...},
 "owner": "test123", "sig": "<HMAC>", "trace_id": "..."}
```

`owner` 是网关盖章的归属账号；`sig = HMAC(stamp_secret, reply|rid|kind|owner|sha256(data))`，
**dispatcher 会验签**（`REQUIRE_READER_SIG=1`，默认）：没签名 / 签名不对 / `owner` 与请求归属不符
的消息直接进 `agent:dlq:out`，不落到回传流、也不改请求状态。这样“能写 Redis 就能伪造 final”
的路被堵住。

`kind` 只有 4 种，`final`/`error` 即终态，**一请求一条终态消息**。

> **reader 侧实现**：本仓库同级的 `paper-trans-to-html/backend/agents/skills/agent_comm.py`
> 就是 `outside-in-stream` 的消费者（技能 `agent_comm`）。它用 `XREADGROUP`
> （组 `outside-in-workers`）取 `request_id` / `agent_id` / `envelope`，按
> `payload.action` 或 `intent` 执行「导出某篇论文的笔记 / 阅读笔记列表 / 回答」，
> 先后写 `ack` 与 `final`（或 `error`）到 `inside-out-stream`，再 `XACK`。
> 命令行：`python tools/comm_worker.py --status|--once`（环境变量 `AGENT_COMM_*`）。

#### 完整结果（超过 64KB 的导出）

单条 `data` 有 64KB 上限，但「完整笔记导出」经常更大。处理方式：

1. reader 侧发现回包超过上限（`AGENT_COMM_INLINE_KB`，默认 60KB）时，先把**完整 JSON**
   写进**同一个 Redis** 的 `agent:result:{rid}`（>32KB 用 `gzip+base64`），
   再把「摘要 + 引用」写 `inside-out-stream`；`paper.note.export` 的**完整导出一定落库**
   （正文全文 + 原格式块 + 全部笔记），消息永远只是摘要；
2. dispatcher 侧兜底：任何 reader 直接写超限 `data`，也会被存进
   `agent:result:{rid}`，不会像以前那样回 `payload_too_large` 丢内容；
3. 科研 agent 拿到 `final` 后，用 `GET /v1/requests/{rid}/result`
   （SDK：`fetch_result(request_id)`；`ask()` 默认自动取回完整结果）拿全量。

摘要消息形如：

```json
{"ok": true, "action": "export_notes", "doc_id": "...",
 "notes": {"stats": {"notes_total": 128}, "preview": {"sentence_notes": ["…前 3 条"]}},
 "sections": {"main_text": {"pages": [2, 3], "chars": 29845, "blocks": 124}},
 "report": {"summary": "…"},
 "truncated": true,
 "result_ref": "agent:result:req_x", "result_url": "/v1/requests/req_x/result",
 "result_bytes": 356681, "result_sha256": "…", "result_encoding": "gzip+base64"}
```

> 前提：reader 与通信服务必须连**同一个 Redis**（`AGENT_COMM_REDIS_URL` = `REDIS_URL`）。
> 完整结果默认保留 `RESULT_TTL`（3600s）；过期后 `/result` 返回 404。
> 想「取完就清」可以用 `GET /v1/requests/{id}/result?purge=true`（SDK
> `fetch_result(rid, purge=True)`），或全局设 `RESULT_DELETE_ON_FETCH=1`。

### 4. 不做流式的原因

reader agent 回传的是一个完整字典，没有"半个字典"的中间态可推送。Redis Stream 是**持久化队列**（消费组 / ACK / 离线重放），与 token 级流式推送无关，两者不要混为一谈。只有 LLM 正文逐字输出才需要 SSE。

大对象不进消息：入站的大文件用 `transfer_key` 指向 blob；出站 `data` 限 64 KB，
**超出部分不丢**，而是完整结果落 `agent:result:{rid}`，消息里只带摘要 + `result_ref`
（见 3.1）。客户端按需 `GET /v1/requests/{rid}/result` 取回。

### 5. 流与键

| Key / Stream | Group | 说明 |
|---|---|---|
| `outside-in-raw` | `guard-workers` | 隔离区，深检前暂存 |
| `outside-in-stream` | `outside-in-workers` | 干净流，reader agent 消费 |
| `inside-out-stream` | `inside-out-dispatchers` | reader 产出，服务消费 |
| `agent:reply:{rid}` | — | 每请求回传流，TTL |
| `agent:result:{rid}` | — | **完整结果**（超 64KB 的 JSON；>32KB 时 gzip+base64），TTL |
| `agent:req:{rid}` | — | 状态 / deadline / 取消 / waiter 摘要 / `result_*` 元信息 |
| `agent:waiter:{rid}` | — | 等待者存活标记（TTL 即超时） |
| `agent:dlq:in` / `agent:dlq:out` | — | 死信 |
| `paper.agent_transfer_keys`（MongoDB） | — | transfer_key 元数据：摘要 / 有效期 / 吊销 / 归属 |

### 6. API

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/v1/requests` | 提交请求，返回 `request_id`（幂等） |
| GET | `/v1/requests/{id}?from=` | 查状态 + 增量结果 |
| GET | `/v1/requests/{id}/result?meta=` | **取完整结果**（`meta=true` 只看大小/摘要/编码） |
| GET | `/v1/requests/{id}/wait?from=&timeout=` | 长轮询，返回新消息或 204 |
| GET | `/v1/requests/{id}/waiter` | 是否还有人在等（reader 预检） |
| DELETE | `/v1/requests/{id}` | 取消（写取消旗标，立即回一条 cancelled） |
| POST | `/v1/transfer-keys` | **默认关闭**（`ALLOW_ENV_TRANSFER_KEYS=1` 才可签发旧的 `tk:v2`） |
| GET | `/v1/transfer-keys` | 列出自己名下（agent 维度）的旧 key 元数据 |
| DELETE | `/v1/transfer-keys/{key_id}?purge=` | 吊销（默认软删）/ 物理删除旧 key |
| GET | `/healthz` `/metrics` `/v1/dlq` | 运维 |

### 7. 安全检测

| 层 | 内容 | 处置 |
|---|---|---|
| Redis 传输 | `--requirepass` + 端口只绑 `127.0.0.1`；容器间用 `redis://:密码@redis:6379` | NOAUTH / 连不上 |
| 鉴权（agent） | `X-Agent-Id` + `X-Api-Key`；开 `REQUIRE_SIGNATURE` 时加 HMAC+时间窗+nonce 防重放 | 401 |
| **准入（账号）** | **唯一决策点**：`transfer_key` 必须是账号 key（`ptk_…`），查库确认存在 + 账号未注销 + 未被删；`payload.user` 自报不一致也拒 | 403 |
| 盖章 | 校验结果写成信封 `owner` + `auth.mac`（HMAC 绑 `request_id|from_agent|owner|key_id|intent|ts`），reader 只读这个结果 | 403 |
| 请求归属 | `GET/DELETE /v1/requests/{rid}`、`/wait`、`/result` 只允许提交方读取；`/v1/dlq` 也要鉴权 | 403 / 401 |
| 回包签名 | reader 回包带 `owner` + `sig`（HMAC，密钥=盖章密钥）；验不过进 `agent:dlq:out` | DLQ |
| 配额 | 每 agent 每秒限额 | 429 |
| Schema | `extra=forbid`、长度上限、`intent` 白名单；`owner`/`auth` 属服务端字段，客户端填写即拒 | 422 |
| 快检（HTTP 内） | 命令拼接、路径穿越、绝对路径、脚本标签、`data:` URI、SQL/NoSQL 注入 | 422 |
| 深检（raw 流上） | 盖章完整性（MAC / 归属绑定）+ 嵌套深度、双向控制符、提示注入标记、超长 base64 块 | DLQ + `quarantined` |

旧模型遗留：`tk:v2` + `KEY_HASH_PEPPER` 只是兜底兼容（`ALLOW_ENV_TRANSFER_KEYS=1` 才启用），
默认关闭——访问权只来自账号 transfer key，不来自容器启动时的环境变量。

reader agent 侧约束：payload 只当数据，禁止 `eval/exec/pickle.loads`；落盘前归一化并校验路径。

### 8. 并发与可靠性

- `redis.asyncio` 单连接池；消费者用信号量做背压，槽位满即停止拉取。
- 消费组水平扩容；`XADD` 全部带 `maxlen`，所有 key 带 TTL。
- `XAUTOCLAIM` 定期认领超时 pending（at-least-once，终态消息重复可被客户端按 `type` 去重）。
- 毒消息进 DLQ 并 ACK，不堵队列。

### 9. 运行

MongoDB 复用同级的 `mongo_configuration/`（单机版：容器 `paper-mongo`、网络 `paper-mongo-net`、
业务账号 `paper` 对 `paper` 库有 readWrite）：

```bash
# 1) 先起库（首次：cp .env.example .env 并按需改密码）
cd ../mongo_configuration && docker compose up -d

# 2) 再起本服务：自动加入 paper-mongo-net，用 mongo:27017 连库
cd ../agent_comm_service && docker compose up -d --build

# 本地跑（需先有 Redis + MongoDB）
pip install -r requirements.txt
python run_service.py     # 默认 mongodb://paper:paper123@127.0.0.1:27017/paper?authSource=paper
```

真实部署配置写 `.env`（compose 会读取）。准入相关的配置项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_SECRETS` | 无（**必填**） | 传输层 agent 身份：`{"agent-id":"密钥"}`；compose 用 `${AGENT_SECRETS:?}`，不设直接拒绝启动 |
| `REDIS_PASSWORD` | `paper-redis-dev` | Redis 口令（compose 注入 `--requirepass`；reader 侧也要用同一口令拼 `REDIS_URL`） |
| `ACCOUNT_KEY_REQUIRED` | `1` | 每个请求都必须带账号 transfer key（`ptk_…`）；置 0 只给本地调试 |
| `ALLOW_ENV_TRANSFER_KEYS` | `0` | 是否还接受/签发旧的 `tk:v2`（pepper 摘要、与账号无关）；默认关闭 |
| `ACCOUNT_KEY_PREFIX` | `ptk_` | 账号 key 前缀 |
| `ACCOUNT_KEY_COLLECTION` | `transfer_keys` | 应用那套「转交 Key」集合（`_id` = 用户名小写，`keys[]` 里是明文 key） |
| `USER_COLLECTION` | `users` | 用来确认账号没被注销 |
| `GATEWAY_META_COLLECTION` | `agent_gateway_meta` | 盖章密钥的持久化位置（自动生成，勿删） |
| `AUTH_STAMP_SECRET` | 空 | 显式指定盖章密钥；为空则用 Mongo 里那份（跨重启稳定，不依赖启动环境变量） |
| `STAMP_WINDOW` | `REQUEST_TTL` | 盖章有效期（秒），超时视为过期 |
| `REQUIRE_READER_SIG` | `1` | reader 回包必须签名（HMAC，密钥=盖章密钥）；置 0 只给兼容旧 reader 用 |

自检：

```bash
python tests/test_account_key.py                       # 账号 key + 盖章（不依赖 Mongo/Redis）
python tests/test_mongo_e2e.py --strict-pepper         # 旧的 tk:v2 链路（需 ALLOW_ENV_TRANSFER_KEYS=1）
```

账号 key 由应用写在 `paper.transfer_keys`（一个账号一行，`keys[]` 里是明文，所有者可复制）；
旧的 `tk:v2` 在 `paper.agent_transfer_keys`（只存 HMAC 摘要），默认不再使用。
不依赖 Mongo 时可设 `MONGO_URL=memory://`（进程内存，仅调试）。

客户端见 `sdk/client.py`，给科研 agent 的工具说明见 `sdk/skill.md`。

---

## 10. 并发压测

`tests/stress_test.py` 同时模拟多个科研 agent（提交 + 长轮询）和多个 reader agent（消费 `outside-in-stream`、回写 `inside-out-stream`），分两种压力方向：

| 模式 | 测什么 | 关键输出 |
|---|---|---|
| `--mode scientific`（默认） | 科研 agent → 通信模块：N 个长轮询同时挂起 | 成功率、等待 p99、最大稳定并发 |
| `--mode reader` | reader 侧：先排 K 个请求，再用 R 个 reader 并发消费 | drain 时间、req/s、msg/s、pending、最大稳定 reader 并发 |
| `--mode mixed` | 两方同时工作：N 个科研 agent × R 个 reader 的联合矩阵 | 每个 (N,R) 的成功率、p99、联合最大稳定组合 |
| `--mode both` | 科研侧、reader 侧两段依次都跑（不重叠） | 两份结论 |

```bash
# 科研侧：自包含，脚本会启动 mock reader；默认用闸门让 N 个长轮询真正同时挂起
python tests/stress_test.py --api-key change-me

# 科研侧逐档加压；--fail-under 可用于 CI（最大稳定并发不足时退出码 2）
python tests/stress_test.py --levels 1,8,32,64,128 --rounds 3 \
    --api-key change-me --json-out stress.json --fail-under 64

# reader 侧：K=256 个请求排队，再用 8/16/32/64 个 reader 并发消费
python tests/stress_test.py --mode reader --reader-levels 8,16,32,64 \
    --reader-requests 256 --api-key change-me

# 两方同时工作：N=8/32/64 个科研 agent × R=8/16/32 个 reader 的联合矩阵
python tests/stress_test.py --mode mixed --mixed-levels 8,32,64,96 \
    --mixed-reader-levels 8,16,32,64 --api-key change-me --json-out stress.json

# 想顺带压 dispatcher 回传链路：每个请求回 3 条消息（ack + progress + final）
python tests/stress_test.py --mode reader --reader-messages-per-request 3 \
    --api-key change-me

# 已有真实 reader agent 时，不再启动 mock reader（仅适用于 scientific 模式）
python tests/stress_test.py --no-mock-reader --api-key change-me

# 应用侧 reader 已在跑、只压科研侧：笔记导出 + 笔记列表导出混合
# （自动读 .env 的账号/Mongo、自动签发并吊销信封 transfer_key）
python tests/stress_notes_mixed.py --levels 8,16,32,64
# 每条 reader 回复默认落到 reports/stress_notes_<时间>.jsonl（+ .summary.json）
```

判定标准：科研侧某档位「成功率 ≥ `--slo` 且 p99 等待 ≤ `--p99-budget`」即为稳定；reader 侧则看成功率、残余 pending 与 `dispatch handler_error`，并观察 req/s 是否随 reader 数继续增长。`--reader-levels` 按 8→16→32→64 递增，若吞吐不再上涨说明瓶颈已转到 guard/dispatcher/Redis。

报告头部和 `--json-out` 明细都会带上压测机规格（CPU 型号、物理/逻辑核数、内存总量与可用量）。若服务与压测机不是同一台机器，用 `--server-spec "16C/64G"` 备注服务端规格，避免结果脱离硬件上下文。

两个与实际容量相关的常见瓶颈：

- `QUOTA_QPS` 限制提交速率，脚本会把 429 计为重试；想单独测长轮询并发可加 `--submit-qps 15`。
- redis-py 8.x 连接池默认只有 100 个连接，而每个长轮询会占住 1 个；并发超过约 100 时服务端会出现 `MaxConnectionsError` / 500。需调大服务端 Redis 连接池（`max_connections`）或降低并发。

### 压测结果（2026-11-27，笔记导出 `paper.note.export`，真实 reader）

两次压测均使用 `tests/stress_notes_mixed.py`，同一篇论文（Unlimited OCR Works），压测机 4C/8T Intel Xeon Gold 6133 / 15.6 GiB 内存，服务为本机 `127.0.0.1:9500`。原始日志见 `stress_export_mechanical.log`、`stress_export_agent.log`。

<table>
  <thead>
    <tr><th>方式</th><th>并发</th><th>请求</th><th>成功</th><th>成功率</th><th>rps</th><th>提交p95(ms)</th><th>等待 p50/p99/max (s)</th><th>429</th><th>判定</th></tr>
  </thead>
  <tbody>
    <tr><td rowspan="4">机械导出<br>（SLO 99% / p99≤10s）</td><td>8</td><td>8</td><td>8</td><td>100%</td><td>8.4</td><td>36.7</td><td>0.69 / 0.79 / 0.79</td><td>0</td><td>✅</td></tr>
    <tr><td>16</td><td>16</td><td>16</td><td>100%</td><td>13.9</td><td>67.5</td><td>0.51 / 0.92 / 0.93</td><td>0</td><td>✅</td></tr>
    <tr><td>32</td><td>32</td><td>32</td><td>100%</td><td>14.2</td><td>111.6</td><td>0.45 / 0.91 / 0.92</td><td>12</td><td>✅</td></tr>
    <tr><td>64</td><td>64</td><td>64</td><td>100%</td><td>11.6</td><td>175.3</td><td>0.77 / 2.34 / 2.38</td><td>72</td><td>✅</td></tr>
    <tr><td rowspan="3">Agent 导出<br>（SLO 95% / p99≤200s）</td><td>8</td><td>8</td><td>8</td><td>100%</td><td>0.1</td><td>36.6</td><td>145.19 / 157.88 / 158.30</td><td>0</td><td>✅</td></tr>
    <tr><td>16</td><td>16</td><td>16</td><td>100%</td><td>0.1</td><td>60.2</td><td>123.12 / 190.17 / 190.27</td><td>0</td><td>✅</td></tr>
    <tr><td>32</td><td>32</td><td>32</td><td>100%</td><td>0.1</td><td>94.4</td><td>160.78 / 293.66 / 295.52</td><td>12</td><td>❌ p99 293.66s &gt; 200s</td></tr>
  </tbody>
</table>

- 机械导出（reader 不做 LLM 处理）：**最大稳定并发 ≥ 64**（最高档仍稳定，可继续用更高 `--levels` 探测上限）；总耗时仅 10.1s。
- 通信链路本身不是瓶颈：机械导出等待时延全程秒级以内，服务端计数守恒（accepted = guard.pass = dispatch.final = dispatch.ack = +121）。
- 32/64 档出现 429 重试，系 `QUOTA_QPS` 提交限流所致，与容量无关（可用 `--submit-qps 15` 隔离验证）。
- Agent 导出（payload `mode=agent`，reader 走完整 LLM 笔记生成）：**最大稳定并发 = 16**，首个不稳定档位 32；总耗时 645.6s。
- Agent 导出全部请求最终成功（成功率 100%），时延完全由 reader 侧 LLM 生成耗时主导（单次约 2~5 分钟），通信模块的提交/回传开销可忽略；32 并发下 p99 突破 200s 预算，瓶颈是 reader 的 LLM 配额与处理速度，而非 agent_comm_service——若需更高并发，应扩容 reader 实例或放宽等待预算。

**结论**：通信模块在机械导出下可稳定支撑 ≥ 64 并发（p99 < 2.4s）；agent 导出下端到端能力受 reader 侧 LLM 处理时间限制，当前单 reader 配置建议将并发控制在 16 以内。

---

## 11. 源码目录结构

```text
agent_comm_service/
├── README.md                    设计与说明（本文）
├── requirements.txt             运行依赖
├── .env.example                 配置模板
├── env_load.py                  配置加载（流名/组名/TTL/并发/鉴权/意图白名单）
├── run_service.py               启动入口：uvicorn 起 app.main:app
├── Dockerfile                   服务镜像
├── docker-compose.yml           redis + agent-comm 编排
├── app/
│   ├── __init__.py
│   ├── main.py                  FastAPI 生命周期：建消费组、起 guard/dispatch/巡检任务
│   ├── core/
│   │   ├── __init__.py
│   │   ├── models.py            请求信封、状态机、kind→status 映射
│   │   ├── guard.py             快检/深检规则、transfer_key 签发与校验
│   │   ├── streams.py           key 命名、消费组、DLQ、回传流读写
│   │   ├── waiters.py           等待者登记（TTL 心跳）
│   │   ├── ingest.py            幂等提交、raw 流投递、上下文查询
│   │   ├── dispatcher.py        深检消费者/回传分发/信号量背压/XAUTOCLAIM 巡检
│   │   ├── auth.py              鉴权（ApiKey / HMAC+nonce）与每秒配额
│   │   └── metrics.py           进程内计数器
│   └── api/
│       ├── __init__.py
│       ├── routes_requests.py   提交/查询/长轮询/waiter/取消/transfer-keys
│       └── routes_admin.py      /healthz /metrics /v1/dlq
├── sdk/
│   ├── __init__.py
│   ├── client.py                科研 agent 客户端（submit/poll/wait/ask/cancel）
│   └── skill.md                 给科研 agent 的工具说明
└── tests/
    ├── test_guard.py            载荷检测规则测试
    ├── test_flow.py             端到端冒烟（需 Redis，否则 skip）
    ├── test_mongo_e2e.py        transfer_key→MongoDB 链路检查（需服务 + Mongo）
    ├── stress_notes_mixed.py    科研侧压测：笔记导出 + 笔记列表导出（真实 reader）
    └── stress_test.py           并发压测（科研 agent + reader agent）
```
