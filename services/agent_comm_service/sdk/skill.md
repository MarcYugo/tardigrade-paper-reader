# Skill: 与论文阅读器 agent 通信

你（科研 agent）需要向论文阅读器提出请求时，使用 `sdk/client.py` 的 `AgentCommClient`。

## 工具

| 工具 | 作用 |
|---|---|
| `submit(intent, payload, transfer_key=None, timeout_s=180)` | 提交请求，返回 `request_id` |
| `wait(request_id, timeout_s=180)` | 长轮询直到终态，返回 `{status, items}` |
| `ask(intent, payload, transfer_key=None, hydrate=True)` | 提交 + 等待，直接返回最终结果；**大结果自动取回完整内容** |
| `fetch_result(request_id, purge=False)` | 取完整结果（回传消息只是摘要时用）；`purge=True` 取完即删 |
| `fetch_result_meta(request_id)` | 只看完整结果的大小 / sha256 / 编码 / TTL |
| `cancel(request_id)` | 取消 |
| `issue_transfer_key(blob_id=None, ttl=None, label="")` | 申请**旧的** `tk:v2` 令牌（默认已被服务端关闭，会 403） |
| `list_transfer_keys()` / `revoke_transfer_key(key_id)` | 查自己名下的旧令牌 / 吊销 |

## 凭据：只能用账号 transfer key

`transfer_key` 必须是用户在 tardigrade 应用里签发的账号转交 Key（`ptk_…`）。
网关会：

1. 在入口查库校验（存在 / 未删除 / 账号未注销）；
2. 把归属账号以「盖章」形式写进信封（`owner` + `auth.mac`），并抹掉明文 key；
3. `payload.user` 由服务端固定成该账号——**不要自己传** `user` / `transfer_key` 来冒名，
   与 key 归属不一致会直接 `403 owner_mismatch`。

> `owner` / `auth` 是服务端字段，客户端填任何值都会 `422`。
> 旧的 `tk:v2`（按服务启动环境变量里的 pepper 派生）默认拒收。

## 约定

1. **一次 `ask` 就够**，不要自己写高频轮询；需要手工等时用 `wait`，它内部就是长轮询。
2. `payload` 只放小体积结构化参数（≤ 64 KB）；论文全文、长表、图片一律用 `transfer_key` 传引用。
3. `intent` 必须在白名单内，未知意图会被 `422` 拒绝。
4. 结果是一个**完整字典**，`wait` 返回的 `items` 里 `type == "final"` 那条即答案。
5. **大结果（笔记全量导出等）不会塞进消息**：终态 `data` 会带 `"truncated": true`
   和 `result_url` / `result_ref`，此时：
   - `ask()` 默认自动 `fetch_result()`，返回项的 `data` 已是完整内容，`slim` 是摘要；
   - 自己写 `wait()` 的，取到终态后再 `await c.fetch_result(request_id)`；
   - 完整结果在服务端 Redis 保留 `RESULT_TTL`（默认 1 小时），过期后 `/result` 返回 404；
   - 拿到就不再需要时，用 `fetch_result(rid, purge=True)`（或 URL 加 `?purge=true`）
     **取完即删**，别让几 MB 的结果白占一小时内存。
6. 错误处理：
   - `401 / 403` 不要重试（身份 / 账号 key / 归属问题）；
   - `422` 不要重试（参数或内容不合规，按 `detail` 改）；
   - `429 / 5xx` 可退避重试；
   - `status == "quarantined"` 表示内容被安全隔离，不要原样重发。

## 示例

```python
from sdk.client import AgentCommClient

async with AgentCommClient("http://127.0.0.1:9500", "research-agent-v1", api_key="...") as c:
    # ptk_… 由用户在 tardigrade 的「🔑 转交 Key」里签发并交给本 agent
    account_key = "ptk_xxxxxxxx"
    result = await c.ask(
        "paper.insight.validate",
        payload={"claim": "多头注意力等价于加权特征融合"},
        transfer_key=account_key,
        timeout_s=300,
    )
    print(result["type"], result["data"])

    # 笔记全量导出：data 太大时 ask() 已自动取回，等价于下面这步
    note = await c.submit("paper.note.export", payload={"doc_id": "…"},
                          transfer_key=account_key, timeout_s=120)
    waited = await c.wait(note["request_id"], timeout_s=120)
    final = next(i for i in waited["items"] if i["type"] == "final")
    if final["data"].get("truncated"):
        full = await c.fetch_result(note["request_id"])   # 完整笔记 JSON
    else:
        full = final["data"]
```

## `paper.note.export` 返回的完整内容

消息（`final.data`）只是摘要，`fetch_result()` / `ask()` 取回的完整 JSON 里：

| 字段 | 内容 |
|---|---|
| `fulltext` | **整篇论文纯文本**（标题→首页信息→摘要→正文→参考文献→附录）；`paragraphs[]` 逐段 `{section,id,page,kind,label,text}`，**不含 bbox** |
| `figures` | **配图**：`page` / `file` / `kind` / `caption` / `bytes` / `data_url`（`data:image/png;base64,…`）；超预算的图不带 base64，见 `skipped` |
| `code_links` | 正文与笔记里提到的代码仓地址（GitHub / GitLab / Hugging Face …） |
| `reference_check` | 参考文献结构检查：`count` / `cited` / `missing` / `uncited` / `duplicates` / `links` |
| `notes` | 全部句子笔记 / 独立笔记 / 高亮（含页码与原句） |
| `sections` | 原格式块（bbox / 公式区间 / 表格 HTML），只在需要还原版式时用 |
| `report` / `ai_summary` | 机械报告（Agent 模式下含联网核验的 `references_stats`）/ AI 整理笔记 |

开关：`payload.include_images=false` 关掉配图，`payload.include_fulltext=false` 关掉纯文本，
`payload.image_budget_mb=8` 调整配图 base64 的总预算。

导出模式：`payload.mode` 可选 `mechanical`（默认，纯本地、秒级）/ `agent`
（reader 用**该账号的 LLM** 做参考文献联网核验 + 分析报告，慢、且消耗配额）。
`mode=agent` 但账号没配 LLM 时，默认降级成 mechanical 并在 `data.fallback`
写 `llm_unavailable`；想严格失败就传 `payload.allow_fallback=false`。
