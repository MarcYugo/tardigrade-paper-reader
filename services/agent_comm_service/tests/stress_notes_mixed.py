"""科研 agent 侧并发压测：论文笔记导出 + 笔记列表导出（两种 intent 混合，真实 reader agent）。

与 `stress_test.py --mode scientific` 的区别：
  * 默认两种 intent 交替混用：`paper.note.export`（单篇笔记导出）/
    `paper.note.list`（笔记列表导出）；
  * **不启动 mock reader**（`--no-mock-reader`），等待应用侧真实 reader 回包；
  * 自动用 `.env` 里的配置：AGENT_SECRETS → X-Api-Key，MONGO_URL → 自动挑一篇真实
    论文做 `paper_id`；可选带上应用侧的 `ptk_` 转交 Key（`payload.transfer_key`）；
  * 自动向通信服务签发一把信封用的 `tk:v2...` transfer_key（走 guard 校验），
    结束后吊销，不在库里留垃圾。

用法：
    python tests/stress_notes_mixed.py                       # 8,16,32,64 档，每档 2 轮
    python tests/stress_notes_mixed.py --levels 16,64 --rounds 3
    python tests/stress_notes_mixed.py --paper-key ptk_xxx   # 携带应用侧转交 Key
    python tests/stress_notes_mixed.py --no-transfer-key     # 不带信封 tk:v2
    python tests/stress_notes_mixed.py --no-record           # 不写回复记录
    python tests/stress_notes_mixed.py -- --slo 0.95 --p99-budget 30   # 透传给 stress_test

**agent 模式导出**（`mode=agent`：reader 用该账号的 LLM 做联网核验 + 分析报告，
比机械导出慢很多、且会消耗该账号的 LLM 配额与学术库额度），建议单独跑、低并发：

    python tests/stress_notes_mixed.py --intents paper.note.export \
        --export-mode agent --levels 1,2,4 --rounds 1

脚本会自动把 `mode=agent` 放进 `payload`，并在你没显式传时放宽
`--wait-timeout/--request-timeout/--level-timeout/--reader-timeout`；
reader 侧如果该账号没配 LLM，会按 `payload.allow_fallback`（默认 true）降级成
mechanical，回包里会带 `fallback: llm_unavailable`（想严格失败就传
`--payload-json '{"mode":"agent","allow_fallback":false}'`）。

输出：默认把**每条请求的 reader 回复**逐条写进
`reports/stress_notes_<时间>.jsonl`（一行一条，含 intent/request_id/状态/时延/reply），
同时生成 `*.summary.json` 统计各 intent 与回包类型；用 `--record-dir` 换目录，
`--record-max-chars 0` 保留完整回复体。

前置：通信服务已启动；应用侧 reader agent 已在跑；intent 必须在服务端 ALLOWED_INTENTS 里。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import httpx  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

import stress_test  # noqa: E402

DEFAULT_INTENTS = "paper.note.export,paper.note.list"

# agent 模式要跑 LLM 工具循环 + 联网核验，单次可能几十秒；未显式指定时用这组宽松超时
AGENT_TIMEOUTS = {
    "--request-timeout": "300",   # 信封里的 timeout_s（int）
    "--wait-timeout": "300",      # 客户端等待上限
    "--level-timeout": "900",     # 单阶段安全超时
    "--reader-timeout": "900",    # reader 侧等待全部完成
}


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_cfg(path: pathlib.Path) -> dict[str, str]:
    """以 .env.example 为底，再用指定文件覆盖（.env 只写真实值）。"""
    base = ROOT / ".env.example"
    cfg = {k: v for k, v in dotenv_values(base).items() if v is not None}
    if path.resolve() != base.resolve():
        cfg.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    return cfg


def pick_paper_id(cfg: dict[str, str], override: str) -> str | None:
    """从 paper 库 `records` 里挑一条真实论文，让 export_notes 有东西可导。"""
    if override:
        return override
    url = cfg.get("MONGO_URL") or ""
    if not url or "mongo:" in url or "paper-mongo:" in url:
        return None                      # 容器内地址，宿主侧不可达
    try:
        from pymongo import MongoClient
    except Exception:
        return None
    try:
        client = MongoClient(url, serverSelectionTimeoutMS=3000)
        db = client[cfg.get("MONGO_DB") or "paper"]
        doc = db["records"].find_one({}, {"_id": 1, "title": 1})
        client.close()
    except Exception:
        return None
    if not doc:
        return None
    print(f"[info] 自动选用论文：{doc.get('title') or doc['_id']}（{doc['_id']}）")
    return str(doc["_id"])


def issue_comm_key(base: str, agent: str, api_key: str | None, ttl: int) -> dict | None:
    """向通信服务签发信封用的 tk:v2 transfer_key。"""
    headers = {"X-Agent-Id": agent}
    if api_key:
        headers["X-Api-Key"] = api_key
    try:
        with httpx.Client(timeout=10.0) as c:
            resp = c.post(f"{base}/v1/transfer-keys", json={"ttl": ttl, "label": "stress-notes"}, headers=headers)
    except Exception as exc:
        print(f"[warn] 签发 transfer_key 失败（{exc!r}），将不带信封 key 压测")
        return None
    if resp.status_code != 200:
        print(f"[warn] 签发 transfer_key 失败 HTTP {resp.status_code}：{resp.text[:200]}")
        return None
    return resp.json()


def revoke_comm_key(base: str, agent: str, api_key: str | None, key_id: str) -> None:
    headers = {"X-Agent-Id": agent}
    if api_key:
        headers["X-Api-Key"] = api_key
    try:
        with httpx.Client(timeout=10.0) as c:
            c.delete(f"{base}/v1/transfer-keys/{key_id}", params={"purge": "true"}, headers=headers)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args() -> tuple[argparse.Namespace, list[str]]:
    p = argparse.ArgumentParser(description="科研 agent 侧压测：笔记导出 + 笔记列表导出（混合）")
    p.add_argument("--env-file", default=None, help="缺省用 .env，没有则 .env.example")
    p.add_argument("--base-url", default=None, help="缺省 http://127.0.0.1:9500")
    p.add_argument("--agent", default="research-agent-v1")
    p.add_argument("--api-key", default=None, help="缺省取 AGENT_SECRETS[agent]")
    p.add_argument("--intents", default=DEFAULT_INTENTS, help=f"默认 {DEFAULT_INTENTS}")
    p.add_argument("--levels", default="8,16,32,64", help="科研侧并发档位")
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--paper-id", default="", help="缺省从 Mongo records 自动挑一条")
    p.add_argument("--paper-key", default="", help="应用侧 ptk_ 转交 Key，放进 payload.transfer_key")
    p.add_argument("--transfer-key", default="", help="直接指定信封 tk:v2（跳过自动签发）")
    p.add_argument("--no-transfer-key", action="store_true", help="不带信封 transfer_key")
    p.add_argument("--key-ttl", type=int, default=3600, help="自动签发的 tk:v2 有效期（秒）")
    p.add_argument("--keep-key", action="store_true", help="结束后不吊销自动签发的 key")
    p.add_argument("--dry-run", action="store_true", help="只打印将要执行的参数")
    p.add_argument("--json-out", default="", help="透传给 stress_test 的 JSON 明细")
    p.add_argument("--record-dir", default=str(ROOT / "reports"), help="reader 回复 JSONL 输出目录")
    p.add_argument("--no-record", action="store_true", help="不记录 reader 回复")
    p.add_argument("--record-max-chars", type=int, default=8000, help="单条回复最大字符数，0=不截断")
    p.add_argument(
        "--export-mode",
        default="",
        choices=("", "mechanical", "agent"),
        help=("放进 payload.mode：mechanical（默认、reader 自己就是）/ "
              "agent（reader 用账号的 LLM 做核验与分析报告，慢且耗配额）"),
    )
    return p.parse_known_args()


def main() -> int:
    args, extra = parse_args()
    if extra and extra[0] == "--":          # 允许 `-- ` 之后透传参数
        extra = extra[1:]

    env_path = pathlib.Path(args.env_file) if args.env_file else (ROOT / ".env")
    if not env_path.is_file():
        env_path = ROOT / ".env.example"
    cfg = load_cfg(env_path)
    print(f"[info] 配置来源：{env_path}")

    base = (args.base_url or cfg.get("AGENT_COMM_BASE_URL") or "http://127.0.0.1:9500").rstrip("/")
    api_key = args.api_key
    if api_key is None:
        try:
            secrets = json.loads(cfg.get("AGENT_SECRETS") or "{}")
        except json.JSONDecodeError:
            secrets = {}
        api_key = secrets.get(args.agent)

    paper_id = pick_paper_id(cfg, args.paper_id)
    paper_key = args.paper_key or cfg.get("PAPER_TRANSFER_KEY") or ""

    issued: dict | None = None
    comm_key = args.transfer_key
    if not comm_key and not args.no_transfer_key:
        issued = issue_comm_key(base, args.agent, api_key, args.key_ttl)
        if issued:
            comm_key = issued["transfer_key"]
            print(
                f"[info] 已签发信封 transfer_key：key_id={issued['key_id']} "
                f"expires_in={args.key_ttl}s"
            )

    argv: list[str] = [
        "--mode", "scientific",
        "--no-mock-reader",
        "--intents", args.intents,
        "--levels", args.levels,
        "--rounds", str(args.rounds),
        "--base-url", base,
        "--agent-ids", args.agent,
    ]
    if api_key:
        argv += ["--api-key", api_key]
    if comm_key:
        argv += ["--transfer-key", comm_key]
    if paper_id:
        argv += ["--paper-id", paper_id]

    # payload 附加项：应用侧转交 Key + 导出模式（agent 模式由 reader 侧跑 LLM）
    payload_extra: dict[str, object] = {}
    if paper_key:
        payload_extra["transfer_key"] = paper_key
    if args.export_mode:
        payload_extra["mode"] = args.export_mode
    if payload_extra:
        argv += ["--payload-json", json.dumps(payload_extra)]

    if args.json_out:
        argv += ["--json-out", args.json_out]

    if args.export_mode == "agent":
        # agent 导出走 LLM 工具循环 + 联网核验，比机械导出慢一个量级：
        # 1) 未显式指定就放宽各阶段超时；2) 提醒别混压 / 别高并发。
        relaxed = {k: v for k, v in AGENT_TIMEOUTS.items() if k not in extra}
        for key, value in relaxed.items():
            argv += [key, value]
        print(f"[info] agent 模式导出：已{'放宽' if relaxed else '沿用'}超时 "
              f"{', '.join(f'{k}={v}' for k, v in relaxed.items()) or '（用户已指定）'}")
        if "paper.note.list" in args.intents:
            print("[warn] agent 模式只影响 paper.note.export；混入 paper.note.list 仍走机械列表")
        print("[warn] agent 模式会消耗该账号的 LLM 配额与学术库额度，"
              "建议 --intents paper.note.export --levels 1,2 --rounds 1；"
              "想跳过全量参考文献核验可在 reader 进程设 NOTE_OUT_VERIFY_ALL=0")
    record_path = ""
    if not args.no_record:
        record_path = str(
            pathlib.Path(args.record_dir) / f"stress_notes_{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        )
        argv += ["--record-jsonl", record_path, "--record-max-chars", str(args.record_max_chars)]
    argv += extra

    shown = list(argv)
    if "--api-key" in shown:                 # 不打印密钥
        shown[shown.index("--api-key") + 1] = "***"
    if "--transfer-key" in shown:
        idx = shown.index("--transfer-key") + 1
        shown[idx] = shown[idx][:20] + "…"
    print("[info] 压测参数：" + " ".join(shown))
    if args.dry_run:
        print("[dry-run] 未执行")
        return 0

    started = time.time()
    try:
        code = stress_test.main(argv)
    finally:
        if issued and not args.keep_key:
            revoke_comm_key(base, args.agent, api_key, issued["key_id"])
            print(f"[info] 已吊销测试 transfer_key：key_id={issued['key_id']}")
        elif issued:
            print(f"[info] --keep-key：保留 transfer_key key_id={issued['key_id']}")
        if record_path:
            print(f"[info] reader 回复记录：{record_path}")
            print(f"[info] 回复汇总：{pathlib.Path(record_path).with_suffix('.summary.json')}")
    print(f"[info] 总耗时 {time.time() - started:.1f}s")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
