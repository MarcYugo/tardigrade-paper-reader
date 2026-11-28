---
name: tardigrade-paper-reader-comm
description: 通过 agent_comm_service 与本地论文阅读器通信：提问/核验论断、按章节检索、导出整篇论文全文与配图/笔记/参考文献检查、列笔记。**准入只认账号 transfer key（ptk_…）**，需用户先在阅读器网页「导出 → 🔑 转交 Key」生成并交给你。当任务需要论文原文、已有笔记、配图、参考文献核对，或需要阅读器用其账号的 LLM 解读论文时使用。
---

# 与论文阅读器通信

阅读器（`paper-trans-to-html`）是数据持有方；你通过通信服务 `agent_comm_service`
提交请求、长轮询取回结果。**所有操作都用本技能自带脚本完成，不要自己手写轮询。**

```bash
python scripts/paper_comm.py <子命令> [参数]
```

## 0. 先拿账号 transfer key（唯一的数据授权凭据）

信任模型只有一条：**通信服务校验账号 transfer key（`ptk_…`）→ 盖章归属账号 →
阅读器只按盖章结果取数据**。所以没有 `ptk_` 就做不了任何数据操作（会被 403 拒绝）。

`ptk_…` 由用户在阅读器网页的「导出 → 🔑 转交 Key」生成（可备注、可删除，删了立刻失效），
这是**用户主动授权你读取其账号数据**的动作。你需要：

1. 向用户要一把 `ptk_…`（或让用户把它写到 `scripts/.env` 的 `PAPER_COMM_TRANSFER_KEY=`）；
2. 不要自己生成（旧的 `keys --action issue` 已被服务端关闭）；
3. 不要把它当成 `X-Api-Key` 用（那样必然 401）。

两层凭据别搞混：

| 凭据 | 去哪 | 作用 | 缺失后果 |
|---|---|---|---|
| `X-Api-Key`（`PAPER_COMM_API_KEY`） | HTTP header | 只证明“我是哪个 agent”（服务端 `AGENT_SECRETS`） | 401 |
| 账号 transfer key `ptk_…`（`--transfer-key`） | 请求体顶层 `transfer_key` | **准入 + 数据归属**，网关盖章 `owner` | 403 `account_key_required` |

## 1. 再确认链路与凭据

```bash
python scripts/paper_comm.py status
```

服务地址自动探测 `http://127.0.0.1:8090`（本地直跑）和 `http://127.0.0.1:9500`
（docker compose 映射）。凭据优先从脚本目录的 `.env` 自动加载，也可用参数或环境变量覆盖，
优先级：**命令行参数 > 进程环境变量 > `.env`**。

| 参数 | 环境变量 / .env 键 | 说明 |
|---|---|---|
| `--transfer-key` | `PAPER_COMM_TRANSFER_KEY` | **账号转交 Key（`ptk_…`）：数据授权，必填** |
| `--base-url` | `PAPER_COMM_URL` | 通信服务地址 |
| `--agent-id` | `PAPER_COMM_AGENT_ID` | 你的 agent 身份，默认 `research-agent-v1` |
| `--api-key` | `PAPER_COMM_API_KEY` | 传输层 agent 密钥（服务端 `AGENT_SECRETS`） |
| `--secret` | `PAPER_COMM_SECRET` | 开启签名校验时必填（HMAC-SHA256） |
| `--env-file` | `PAPER_COMM_ENV_FILE` | 指定 `.env` 路径 |

`.env` 查找顺序：`--env-file` → `$PAPER_COMM_ENV_FILE` → **脚本目录/.env** → 当前目录/.env。
`status` 会回显实际用的文件、传输层鉴权方式、账号 key 是否已配置（只显示前 8 位），
并用一个不存在的 rid 真实探一次 `X-Api-Key`：`transport_ok.status=404` 说明凭据对，
`=401` 说明 **`PAPER_COMM_API_KEY` 与服务端 `AGENT_SECRETS` 不一致**（此时 `status` 退出码 5）。

可用的 `.env` 写法：

```dotenv
PAPER_COMM_AGENT_ID=research-agent-v1
PAPER_COMM_API_KEY=change-me                  # 传输层身份（不是账号 key！）
PAPER_COMM_TRANSFER_KEY=ptk_xxxxxxxx          # 数据授权：用户生成后给你
# 也兼容直接沿用服务端的 AGENT_SECRETS（自动取当前 agent 的密钥）
```

> `KEY_HASH_PEPPER` 是服务端算摘要用的，**不是凭据**，新模型下也不再参与鉴权；
> `tk:v2…` 旧令牌已停用（`ALLOW_ENV_TRANSFER_KEYS=0`），脚本会直接拦下。

`status` 不通就别继续：先让用户启动服务（`python run_service.py` 或
`docker compose up -d`），不要反复重试同一请求。

## 2. 四种意图（`intent` 白名单）

| intent | 用途 | payload 关键字段 |
|---|---|---|
| `paper.section.query` | 用自然语言问论文某个部分 | `question`（必填）、`doc_id` / `title`（定位论文，账号只有一篇时可省） |
| `paper.insight.validate` | 核验一句论断是否被论文/笔记支持 | `claim`（必填，等价于 `question`）、`doc_id` / `title` |
| `paper.note.export` | 导出整篇：全文 + 配图 + 笔记 + 参考文献检查 | `doc_id` / `title`；开关见下 |
| `paper.note.list` | 列出账号下的笔记/论文清单 | `doc_id` / `title`（可选，缩小范围） |

白名单外的 `intent` 会被 `422` 拒绝，不要猜名字。

定位论文的字段：`doc_id`（推荐）> `paper_id` > `title` > `paper`。

**不要**在 `payload` 里传 `user` / `username` / `transfer_key` 来指定或冒名账号：归属账号完全由账号
`transfer key` 决定（网关会把它归一化成 `payload.user`）；自报值与 key 归属不一致会直接
`403 owner_mismatch`。想确认读到的是哪个账号，看脚本打印的 `owner=` 或回包里的 `data.owner`。

回答类意图的终态 `data`：`{ok, action, owner, question, answer, mode, sources[]}`；
`mode=llm` 表示阅读器账号的 LLM 作答，`mechanical` 是关键词检索的降级回答。

## 3. 常用操作

> 下面假设 `PAPER_COMM_TRANSFER_KEY` 已写进 `.env`；若没有，每条命令都加上
> `--transfer-key ptk_…`（或先 `export PAPER_COMM_TRANSFER_KEY=ptk_…`）。

```bash
# 提问（答案在 data.answer；data.mode=llm 表示阅读器账号的 LLM 作答）
python scripts/paper_comm.py ask --intent paper.section.query \
    --payload '{"doc_id":"08fc1159...","question":"实验用的什么数据集？"}'

# 核验论断
python scripts/paper_comm.py ask --intent paper.insight.validate \
    --payload '{"doc_id":"08fc1159...","claim":"多头注意力等价于加权特征融合"}'

# 列笔记
python scripts/paper_comm.py list --doc-id 08fc1159...

# 导出整篇（大结果自动落盘，终端只留摘要）
python scripts/paper_comm.py export --doc-id 08fc1159... --out ./paper.json

# 只导出、不取回配图（省流量和上下文）
python scripts/paper_comm.py export --doc-id 08fc1159... --no-images

# 用阅读器账号的 LLM 做参考文献联网核验 + 分析报告（慢、耗配额）
python scripts/paper_comm.py export --doc-id 08fc1159... --mode agent

# 事后补取 / 清理某次请求的完整结果
python scripts/paper_comm.py result --request-id req_xxx --purge
```

`export` 默认 `--mode mechanical`（纯本地、秒级）。`mode=agent` 需要该账号配了 LLM，
否则服务端会降级并在 `data.fallback` 写 `llm_unavailable`；想严格失败加
`--no-fallback`（对应 `payload.allow_fallback=false`）。

其它导出开关：`--no-fulltext`（`include_fulltext=false`）、
`--image-budget-mb 8`（配图 base64 总预算）。

## 4. 大结果规则（必读）

终态消息里只有摘要时，`data` 会带 `"truncated": true` 和 `result_url` / `result_ref`：

- `ask` / `export` 子命令**已经自动取回完整结果**，你拿到的就是全文；
- 自己发 HTTP 时，取到终态后再 `GET /v1/requests/{rid}/result`（脚本：`result` 子命令）；
- 完整结果在服务端 Redis 只保留 `RESULT_TTL`（默认 1 小时），过期变 `404`；
- 不再需要时用 `--purge` **取完即删**，别让几 MB 的 JSON 白占内存。

导出结果字段含义见 `references/export-schema.md`。**注意结构**：
`fulltext` 是对象，正文在 `fulltext.text`，逐段内容在 `fulltext.paragraphs[]`
（含 `section` / `page`）；`notes` 是对象（`sentence_notes` / `free_notes` / `highlights`）；
`data.slim` 是原始摘要，`data` 才是完整内容。
**不要把几 MB 的 JSON 直接读进上下文**：先看脚本打印的摘要，需要正文再分段读落盘文件。

## 5. payload 与凭据

- `payload` 只放小体积结构化参数，**≤ 64 KB**；论文全文、长表、图片一律用引用（`doc_id`）。
- **只有一种 key：账号 transfer key（`ptk_…`）**，放请求体顶层的 `transfer_key`
  （`--transfer-key` / `PAPER_COMM_TRANSFER_KEY`）。流程：

  ```text
  用户网页生成 ptk_  →  你带着它提交  →  网关查库校验并盖章 owner
                   →  reader 只按 owner 取数据  →  明文 key 不进入 Redis
  ```

- 网关的行为（不需要你做任何事，但决定了错误码）：
  - 缺 key → `403 account_key_required`；
  - key 不存在 / 在网页里被删除 / 账号已注销 → `403 transfer_key_unknown`；
  - `payload.user` 与 key 归属不一致 → `403 owner_mismatch`；
  - 客户端填 `owner` / `auth`（服务端盖章字段）→ `422`。
- 旧的 `tk:v2`（`keys --action issue` 签发）与 `KEY_HASH_PEPPER` 已停用：
  默认 `403 transfer_key_env_key_rejected` / `env_transfer_keys_disabled`，不要再用。

```bash
# 只检查旧令牌列表（一般用不到；签发已被服务端关闭）
python scripts/paper_comm.py keys --action list
```

## 6. 错误处理

| 现象 | 处理 |
|---|---|
| `401 bad api key` | `PAPER_COMM_API_KEY` 与服务端 `AGENT_SECRETS` 里该 agent 的密钥不一致：**服务端改完要 `docker compose up -d` 重启**；`status` 的 `transport_ok` 会直接告诉你对不对 |
| `401 unknown agent` | `PAPER_COMM_AGENT_ID` 不在服务端 `AGENT_SECRETS` 的键里 |
| `403 account_key_required` | 没给账号 key：向用户要 `ptk_…`，传 `--transfer-key` 或写进 `.env` |
| `403 transfer_key_unknown` | key 无效 / 已被删除 / 账号已注销：向用户要一把新的，**不要重试同一把** |
| `403 owner_mismatch` | `payload` 里自报了 `user`：删掉，账号由 key 决定 |
| `403 transfer_key_env_key_rejected` | 用了已停用的 `tk:v2`：改用账号 `ptk_` |
| `422` | 参数或内容不合规：按 `detail` 改 payload / intent；`owner`/`auth` 不可自填 |
| `429 / 5xx` | 可退避重试（脚本已内置 3 次指数退避） |
| `status == quarantined` | 内容被安全隔离，**不要原样重发**，换措辞或告知用户 |
| `status == timeout` | 加大 `--timeout`（默认 180s，`mode=agent` 建议 300–900s）；若一直是 `queued`，说明阅读器进程没在消费队列，让用户确认阅读器已启动且连的是同一个 Redis |
| `result` 返回 404 | 结果已过期，重新发一次请求 |

## 7. 直接写代码时

用 `agent_comm_service/sdk/client.py` 的 `AgentCommClient`（仅依赖 `httpx`）：

```python
from sdk.client import AgentCommClient

async with AgentCommClient("http://127.0.0.1:9500", "research-agent-v1", api_key="...") as c:
    res = await c.ask("paper.section.query",
                      payload={"doc_id": "…", "question": "…"},
                      transfer_key="ptk_xxx",   # 账号转交 Key，必填
                      timeout_s=300)
    print(res["type"], res["data"])
```

`sdk/skill.md` 是它的完整说明。一次 `ask` 就够，别再手写高频轮询。
