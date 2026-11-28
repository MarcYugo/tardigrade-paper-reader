# `paper.note.export` 完整导出结构（实测 2026-11）

消息里的 `final.data` 只是摘要；完整 JSON 由 `fetch_result()` / `ask()` 取回（脚本的
`export` 子命令已自动取回并存盘）。以下字段以真实回包为准。

## 顶层

| 字段 | 内容 |
|---|---|
| `ok` / `action` / `owner` / `version` / `format` / `exported_at` | 元信息（`owner` = 网关盖章的归属账号） |
| `doc_id` | 论文 id |
| `paper` | `{id, title, authors, source, num_pages, parser, owner, reading:{last_page, progress, status, last_pos}, created_at}` |
| `fulltext` | **对象**（不是字符串）：`chars` / `text` / `plain` / `paragraphs[]` / `sections` / `bbox` |
| `notes` | **对象**：`sentence_notes[]` / `free_notes[]` / `highlights[]` / `stats{}` |
| `sections` | 原格式块数组（bbox / 公式区间 / 表格 HTML），只在还原版式时用 |
| `reference_check` | `count` / `cited[]` / `missing[]` / `uncited[]` / `duplicates[]` / `links[]` |
| `report` | 机械报告：`mode` / `stats` / `summary` / `section_overview` / `notes_by_page` / `figures` / `references` / `reference_check` / `code_links` |
| `code_links[]` | 正文与笔记里提到的代码仓地址（GitHub / GitLab / Hugging Face …） |
| `figures[]` | 配图：`page` / `file` / `kind` / `caption` / `bytes` / `data_url`（`data:image/png;base64,…`）；`include_images=false` 时不返回 |
| `skipped[]` | 因预算被跳过的配图 |
| `ai_summary` / `report.references_stats` | 仅 `mode=agent` 才有 |
| `mode` / `mode_requested` / `fallback` | 实际模式；`fallback=llm_unavailable` 表示降级为 mechanical |

## `fulltext` 对象

| 字段 | 内容 |
|---|---|
| `chars` | 总字符数（先看它判断体量） |
| `text` | 整篇纯文本：标题 → 首页信息 → 摘要 → 正文 → 参考文献 → 附录 |
| `plain` | 纯文本（无额外标注的版本） |
| `paragraphs[]` | 逐段 `{section, id, page, kind, label, text}`，**不含 bbox**；检索与引用定位首选 |
| `sections` | 全文内的分节索引 |
| `bbox` | 段级版面坐标（还原版式时才用） |

## payload 开关

| 字段 | 默认 | 说明 |
|---|---|---|
| `doc_id` / `title` | — | 定位论文；账号只有一篇时可省 |
| `user` | — | **不要传**：归属账号由网关根据账号 transfer key（`ptk_…`）盖章决定；自报且不一致会 403 |
| `mode` | `mechanical` | `agent` 会用该账号的 LLM 做参考文献联网核验 + 分析报告（慢、耗配额） |
| `allow_fallback` | `true` | `mode=agent` 且账号没配 LLM 时是否降级为 mechanical；`false` 则严格失败 |
| `include_images` | `true` | `false` 不返回配图（省流量） |
| `include_fulltext` | `true` | `false` 不返回纯文本全文 |
| `image_budget_mb` | 服务端默认 | 配图 base64 的总预算（MB） |
| `instructions` | — | 给 AI 整理笔记的附加指令 |

## 读法建议

- **通读论文**：按 `fulltext.paragraphs[].section` 分组读，比整段 `fulltext.text` 更好定位页码。
- **引用原文**：`notes.sentence_notes` / `notes.highlights` 带页码与原句，回答时保留原文措辞。
- **核对参考文献**：先看 `reference_check.missing` / `uncited`，必要时再 `mode=agent` 拿
  `report.references_stats`（联网核验）。
- **看图**：`figures[].data_url` 是 base64，体量大；只取需要的几张，别整批塞进上下文。
- **还原版式**：才用 `sections[]`（含 bbox），开销最大。
- **别整份读进上下文**：先看脚本输出的摘要，需要正文再分段读落盘文件。
