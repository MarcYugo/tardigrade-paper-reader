#!/usr/bin/env python3
"""paper_comm.py —— 与论文阅读器通信服务（agent_comm_service）的命令行客户端。

只用标准库（urllib），不需要 pip 安装任何东西；等价于 sdk/client.py 的能力。

**凭据只有一种：账号 transfer key（`ptk_…`）**，在 tardigrade 网页「导出 → 🔑 转交 Key」
生成。它放在请求体顶层的 `transfer_key`（`--transfer-key`，或 `.env` 的
`PAPER_COMM_TRANSFER_KEY`），由通信服务在入口校验、盖章归属账号。

`X-Api-Key`（`PAPER_COMM_API_KEY`）只是**传输层 agent 身份**（服务端 `AGENT_SECRETS`），
不是数据授权；把 `ptk_` 填到那里会 401。旧的 `tk:v2`（`keys --action issue` 签发）与
`KEY_HASH_PEPPER` 都不再参与鉴权。
优先级：命令行参数 > 进程环境变量 > .env。


    status                      探活 + 凭据体检
    ask       --intent ...      提一个问题 / 核验一句论断
    export    --doc-id ...      导出整篇（全文 + 配图 + 笔记 + 参考文献检查）
    list      [--doc-id ...]    列出笔记
    result    --request-id ...  事后补取完整结果（可 --purge 取完即删）
    keys      --action ...      旧的 tk:v2 令牌（服务端默认已关闭）

示例：
    python paper_comm.py status
    python paper_comm.py ask --intent paper.section.query \
        --transfer-key ptk_xxx --payload '{"doc_id":"abc","question":"用了什么数据集？"}'
    python paper_comm.py export --doc-id abc --out ./paper.json
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from typing import Any
from urllib import error, request
from urllib.parse import urlencode

TERMINAL = {"done", "error", "cancelled", "quarantined", "rejected"}
TERMINAL_ITEM = {"final": "done", "error": "error", "cancelled": "cancelled"}
DEFAULT_URLS = ("http://127.0.0.1:8090", "http://127.0.0.1:9500")
# 账号 transfer key 前缀：通信服务只接受这种 key 作为准入凭据
ACCOUNT_KEY_PREFIX = "ptk_"
# 旧的 tk:v2（pepper 派生、与账号无关），服务端默认已停用
LEGACY_KEY_PREFIX = "tk:v2."
KNOWN_INTENTS = {
    "paper.insight.validate",
    "paper.section.query",
    "paper.note.export",
    "paper.note.list",
}

# 运行时填充（main 里写入，status 会回显）
ENV_FILE_PATH: str | None = None
AUTH_SOURCE = "none"
TRANSFER_KEY_SOURCE = "无"


# ------------------------------ .env 读取 ---------------------------------- #
def load_env_file(path: str) -> dict[str, str]:
    """解析 KEY=VALUE 形式的 .env（忽略注释/空行，去掉包裹引号，支持 export 前缀）。"""
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key:
                values[key] = value
    return values


def find_env_file(explicit: str | None) -> str | None:
    """按优先级找 .env：--env-file > $PAPER_COMM_ENV_FILE > 脚本目录 > 当前目录 > agent_comm_service。"""
    candidates = [
        explicit,
        os.environ.get("PAPER_COMM_ENV_FILE"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.join(os.getcwd(), ".env"),
        os.path.expanduser("~/workspace/agent_comm_service/.env"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def pick_env(env: dict[str, str], *names: str, default: str | None = None) -> str | None:
    """真实环境变量优先，其次 .env；支持多个别名。"""
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
    for name in names:
        if env.get(name):
            return env[name]
    return default


def secret_from_agent_secrets(raw: str, agent_id: str) -> str | None:
    """从 AGENT_SECRETS={"agent":"secret"} 里取当前 agent 的密钥。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        value = data.get(agent_id)
        return value if isinstance(value, str) and value else None
    return None


def resolve_auth(
    env: dict[str, str], agent_id: str, api_key: str | None, secret: str | None
) -> tuple[str | None, str | None, str]:
    """确定鉴权凭据，返回 (api_key, secret, 来源说明)。"""
    if not api_key:
        api_key = pick_env(env, "PAPER_COMM_API_KEY", "AGENT_API_KEY", "API_KEY")
    if not secret:
        secret = pick_env(env, "PAPER_COMM_SECRET", "AGENT_SECRET")

    source = "参数" if (api_key or secret) else "无"
    if api_key:
        source = "api_key"
    if secret:
        source = "api_key + HMAC 签名" if api_key else "HMAC 签名"
    if api_key or secret:
        return api_key, secret, source

    derived = secret_from_agent_secrets(pick_env(env, "AGENT_SECRETS") or "", agent_id)
    require_sig = (pick_env(env, "REQUIRE_SIGNATURE") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if derived:
        # 两个头都带：服务端开 REQUIRE_SIGNATURE 时走签名，未开时走 api key
        source = ".env: AGENT_SECRETS" + ("（签名+api key）" if require_sig else "（api key）")
        return derived, derived, source

    pepper = pick_env(env, "KEY_HASH_PEPPER")
    if pepper:
        print(
            "[warn] .env 里有 KEY_HASH_PEPPER：它是服务端算 transfer_key 摘要用的 pepper，"
            "不是凭据；新信任模型下它也不再参与鉴权。请改用 AGENT_SECRETS 或 PAPER_COMM_API_KEY",
            file=sys.stderr,
        )
    return None, None, "无（服务端开了 AGENT_SECRETS 时会 401）"


def resolve_transfer_key(env: dict[str, str], explicit: str | None) -> tuple[str | None, str]:
    """账号 transfer key（ptk_…）：命令行 > 环境变量/.env。"""
    if explicit and explicit.strip():
        return explicit.strip(), "参数 --transfer-key"
    for name in ("PAPER_COMM_TRANSFER_KEY", "PAPER_COMM_ACCOUNT_KEY", "AGENT_TRANSFER_KEY"):
        value = pick_env(env, name)
        if value and value.strip():
            return value.strip(), f".env/环境变量 {name}"
    return None, "无"


def require_transfer_key(key: str | None, source: str) -> str:
    """准入凭据必须有；格式不对就地报清楚，不发一个必然 403 的请求。"""
    key = (key or "").strip()
    if not key:
        raise SystemExit(
            "缺少账号 transfer key（ptk_…）：通信服务只接受账号 key。\n"
            "  1) 在 tardigrade 网页「导出 → 🔑 转交 Key」生成一把（可带备注）；\n"
            "  2) 传 --transfer-key ptk_xxx，或在 .env 里写 PAPER_COMM_TRANSFER_KEY=ptk_xxx。\n"
            "  注意：DATA 授权用 ptk_，X-Api-Key 只代表 agent 身份，两者不能混。"
        )
    if key.startswith(LEGACY_KEY_PREFIX):
        raise SystemExit(
            "tk:v2 是已停用的旧凭据（服务端 pepper 派生、与账号无关），"
            "请改用账号 transfer key（ptk_…）。"
        )
    if not key.startswith(ACCOUNT_KEY_PREFIX):
        print(
            f"[warn] transfer key 前缀不是 {ACCOUNT_KEY_PREFIX}（当前 {key[:6]}…），"
            "服务端很可能拒收",
            file=sys.stderr,
        )
    return key


def key_hint(key: str | None) -> str:
    key = (key or "").strip()
    return f"{key[:8]}…（{len(key)} 字符）" if key else "未配置"


class CommError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def _sign(secret: str, ts: str, method: str, path: str, nonce: str, body: bytes) -> str:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    slot = "\n".join([ts, method.upper(), path, nonce, body_hash])
    return hmac.new(secret.encode(), slot.encode(), hashlib.sha256).hexdigest()


class Client:
    def __init__(
        self,
        base_url: str,
        agent_id: str,
        *,
        api_key: str | None = None,
        secret: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.api_key = api_key
        self.secret = secret
        self.timeout = timeout

    # ----------------------------- 底层 ----------------------------- #
    def _headers(self, method: str, path: str, body: bytes = b"") -> dict[str, str]:
        headers = {"X-Agent-Id": self.agent_id, "Accept": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        if self.secret:
            ts = str(int(time.time()))
            nonce = uuid.uuid4().hex
            headers.update(
                {
                    "X-Timestamp": ts,
                    "X-Nonce": nonce,
                    "X-Signature": _sign(self.secret, ts, method, path, nonce, body),
                }
            )
        return headers

    def call(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        retries: int = 3,
    ) -> tuple[bytes, int]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urlencode(params)
        headers = self._headers(method, path, data or b"")
        if data is not None:
            headers["Content-Type"] = "application/json"

        last: CommError | None = None
        for attempt in range(retries):
            req = request.Request(url, data=data, headers=headers, method=method)
            try:
                with request.urlopen(req, timeout=timeout or self.timeout) as resp:
                    return resp.read(), resp.status
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    last = CommError(exc.code, detail)
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise CommError(exc.code, detail) from exc
            except error.URLError as exc:
                last = CommError(0, f"无法连接 {self.base_url}：{exc.reason}")
                if attempt < retries - 1:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise last from exc
        raise last or CommError(0, "请求失败")

    def json(self, *args: Any, **kwargs: Any) -> Any:
        raw, _ = self.call(*args, **kwargs)
        return json.loads(raw) if raw else None

    # ----------------------------- 业务 ----------------------------- #
    def submit(
        self,
        intent: str,
        payload: dict[str, Any] | None = None,
        *,
        transfer_key: str | None = None,
        timeout_s: int = 180,
        wait: bool = True,
    ) -> dict[str, Any]:
        body = {
            "from_agent": self.agent_id,
            "intent": intent,
            "payload": payload or {},
            "transfer_key": transfer_key,
            "wait": wait,
            "timeout_s": timeout_s,
        }
        return self.json("POST", "/v1/requests", body=body)

    def wait(self, request_id: str, *, timeout_s: float = 180, window: int = 25) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        cursor = "0-0"
        items: list[dict[str, Any]] = []
        status = "queued"
        while time.time() < deadline:
            remain = max(1, min(window, int(deadline - time.time())))
            body = self.json(
                "GET",
                f"/v1/requests/{request_id}/wait",
                params={"from": cursor, "timeout": remain},
                timeout=remain + 10,
            )
            if not body:  # 204：这一轮没新消息
                continue
            status = body.get("status", status)
            new_items = body.get("items") or []
            if new_items:
                cursor = body.get("next", cursor)
                items.extend(new_items)
                for item in new_items:
                    mapped = TERMINAL_ITEM.get(str(item.get("type")))
                    if mapped:
                        status = mapped
                        break
            if status in TERMINAL:
                break
        return {"request_id": request_id, "status": status, "items": items}

    def fetch_result(self, request_id: str, *, purge: bool = False) -> Any:
        params = {"purge": "true"} if purge else None
        body = self.json("GET", f"/v1/requests/{request_id}/result", params=params)
        return (body or {}).get("result")

    def fetch_result_meta(self, request_id: str) -> dict[str, Any]:
        return self.json("GET", f"/v1/requests/{request_id}/result", params={"meta": "true"}) or {}

    def issue_key(self, blob_id: str | None, ttl: int | None, label: str) -> str:
        body = {"blob_id": blob_id, "ttl": ttl, "label": label}
        return self.json("POST", "/v1/transfer-keys", body=body)["transfer_key"]

    def list_keys(self) -> list[dict[str, Any]]:
        return (self.json("GET", "/v1/transfer-keys") or {}).get("items", [])

    def revoke_key(self, key_id: str, *, purge: bool = False) -> dict[str, Any]:
        return self.json(
            "DELETE", f"/v1/transfer-keys/{key_id}", params={"purge": str(purge).lower()}
        )


# ------------------------------- 输出工具 ---------------------------------- #
def emit(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def save_json(obj: Any, path: str | None, default_name: str) -> str:
    target = path or default_name
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    return target


def size_of(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode())


def count_items(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return sum(count_items(v) for v in value.values())
    return 0


def export_summary(rid: str, status: str, data: dict[str, Any], saved: str | None) -> dict[str, Any]:
    paper = data.get("paper") if isinstance(data.get("paper"), dict) else {}
    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    fulltext = data.get("fulltext")

    fulltext_chars = 0
    paragraphs = 0
    sections = data.get("sections")
    if isinstance(fulltext, dict):
        fulltext_chars = fulltext.get("chars") or len(str(fulltext.get("text") or ""))
        paragraphs = count_items(fulltext.get("paragraphs"))
        if sections is None:
            sections = fulltext.get("sections")
    elif isinstance(fulltext, str):
        fulltext_chars = len(fulltext)

    notes = data.get("notes") if isinstance(data.get("notes"), dict) else {}
    note_counts = {k: len(v) for k, v in notes.items() if isinstance(v, list)}

    refs = data.get("reference_check") or report.get("reference_check") or {}
    figures = data.get("figures")
    if figures is None:
        figures = report.get("figures")

    summary: dict[str, Any] = {
        "request_id": rid,
        "status": status,
        "saved": saved,
        "bytes": size_of(data),
        "doc_id": data.get("doc_id") or paper.get("id"),
        "title": paper.get("title") or data.get("title"),
        "mode": data.get("mode") or report.get("mode"),
        "fallback": data.get("fallback"),
        "fulltext_chars": fulltext_chars,
        "paragraphs": paragraphs,
        "figures": count_items(figures),
        "figures_skipped": count_items(data.get("skipped")),
        "notes": sum(note_counts.values()),
        "note_counts": note_counts,
        "code_links": count_items(data.get("code_links") or report.get("code_links")),
        "sections": count_items(sections),
    }
    if isinstance(refs, dict) and refs:
        summary["reference_check"] = {
            key: (len(value) if isinstance(value, list) else value)
            for key, value in refs.items()
            if key in ("count", "cited", "missing", "uncited", "duplicates")
        }
    return summary


# ------------------------------- 子命令 ------------------------------------ #
def final_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in reversed(items):
        if item.get("type") in ("final", "error", "cancelled"):
            return item
    return None


def _report_owner(client: Client, rid: str, expect: str | None) -> None:
    """打印通信服务盖章的归属账号（owner），可选与期望值断言。

    新信任模型下 owner 只由网关根据账号 transfer key 得出，reader 与调用方都改不了；
    这里只做“确认是你想的那个账号”，不参与授权。
    """
    try:
        ctx = client.json("GET", f"/v1/requests/{rid}", retries=1) or {}
    except CommError:
        return
    owner = str(ctx.get("owner") or "")
    via = str(ctx.get("auth_via") or "")
    key_id = str(ctx.get("account_key_id") or "")
    if owner:
        print(
            f"[info] 网关盖章 owner={owner} via={via or '-'}"
            + (f" key_id={key_id}" if key_id else ""),
            file=sys.stderr,
        )
    if expect and owner and owner.lower() != expect.strip().lower():
        raise SystemExit(f"账号不符：盖章 owner={owner}，而 --user 指定 {expect}")


def run_request(
    client: Client,
    intent: str,
    payload: dict[str, Any],
    *,
    transfer_key: str,
    timeout: float,
    purge: bool,
    expect_owner: str | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any] | None]:
    """提交 + 等待 + 自动取回完整结果。返回 (rid, status, data, item)。

    `transfer_key` 必须是账号 key（`ptk_…`）：归属账号由通信服务校验并盖章，
    脚本不再向 payload 里自报 `user`/`transfer_key`。
    """
    resp = client.submit(intent, payload, transfer_key=transfer_key, timeout_s=int(timeout))
    rid = resp["request_id"]
    print(f"[info] 已提交 {intent}  request_id={rid}  status={resp.get('status')}", file=sys.stderr)

    _report_owner(client, rid, expect_owner)
    waited = client.wait(rid, timeout_s=timeout)
    item = final_item(waited["items"])
    if item is None:
        return rid, "timeout", {"items": waited["items"]}, None

    data = item.get("data") or {}
    if isinstance(data, dict) and (data.get("result_url") or data.get("result_ref")):
        try:
            full = client.fetch_result(rid, purge=purge)
        except CommError as exc:
            print(f"[warn] 完整结果取回失败（{exc}），退回摘要", file=sys.stderr)
            return rid, waited["status"], data, item
        if full is not None:
            return rid, waited["status"], full, {**item, "slim": data}
    return rid, waited["status"], data, item


def parse_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_file:
        with open(args.payload_file, encoding="utf-8") as fh:
            return json.load(fh)
    if args.payload:
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--payload 不是合法 JSON：{exc}")
        if not isinstance(payload, dict):
            raise SystemExit("--payload 必须是 JSON 对象")
        return payload
    return {}


def cmd_status(client: Client, _args: argparse.Namespace) -> int:
    body = client.json("GET", "/healthz", retries=1)
    key = getattr(_args, "transfer_key", None)
    # `/healthz` 不鉴权，看不出 X-Api-Key 对不对；用一个不存在的 rid 探一下：
    # 401 = 传输层凭据错，404 = 凭据 OK（只是请求不存在）。
    probe: dict[str, Any]
    try:
        client.json("GET", "/v1/requests/req_comm_probe", retries=1)
        probe = {"ok": True, "note": "已通过鉴权"}
    except CommError as exc:
        if exc.status == 401:
            probe = {"ok": False, "status": 401,
                     "error": exc.detail[:160],
                     "hint": "X-Api-Key 与服务端 AGENT_SECRETS 不匹配，或 agent-id 不存在"}
        else:
            probe = {"ok": True, "status": exc.status}   # 403/404 都说明凭据过了
    emit(
        {
            "base_url": client.base_url,
            "healthz": body,
            "agent_id": client.agent_id,
            "env_file": ENV_FILE_PATH,
            "transport_auth": AUTH_SOURCE,
            "transport_ok": probe,
            "account_key": {
                "configured": bool(key),
                "hint": key_hint(key),
                "source": TRANSFER_KEY_SOURCE,
            },
            "note": "数据授权只认账号 transfer key（ptk_…）；X-Api-Key 只代表 agent 身份",
        }
    )
    if not key:
        print(
            "[warn] 还没有账号 transfer key：ask/export/list 会被本地拦下。\n"
            "       在 tardigrade 网页「导出 → 🔑 转交 Key」生成后，写进 .env 的 "
            "PAPER_COMM_TRANSFER_KEY=ptk_xxx",
            file=sys.stderr,
        )
    if not probe.get("ok"):
        print(
            "[warn] 传输层鉴权失败（401）：skill 的 PAPER_COMM_API_KEY 必须等于服务端 "
            "AGENT_SECRETS 里该 agent 的密钥，改完服务端要 `docker compose up -d` 重启生效。",
            file=sys.stderr,
        )
    return 0 if probe.get("ok") else 5


def cmd_ask(client: Client, args: argparse.Namespace) -> int:
    payload = parse_payload(args)
    if args.intent not in KNOWN_INTENTS:
        print(f"[warn] {args.intent} 不在已知白名单 {sorted(KNOWN_INTENTS)} 内", file=sys.stderr)
    rid, status, data, _ = run_request(
        client, args.intent, payload,
        transfer_key=args.transfer_key, timeout=args.timeout, purge=args.purge,
    )
    result = {"request_id": rid, "status": status, "data": data}
    if args.out:
        saved = save_json(result, args.out, f"ask-{rid}.json")
        print(f"[info] 结果已保存：{saved}", file=sys.stderr)
    emit(result)
    return 0 if status == "done" else 3


def cmd_export(client: Client, args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {}
    if args.doc_id:
        payload["doc_id"] = args.doc_id
    if args.title:
        payload["title"] = args.title
    if args.mode:
        payload["mode"] = args.mode
        if args.mode == "agent" and args.no_fallback:
            payload["allow_fallback"] = False
    if args.no_images:
        payload["include_images"] = False
    if args.no_fulltext:
        payload["include_fulltext"] = False
    if args.image_budget_mb is not None:
        payload["image_budget_mb"] = args.image_budget_mb

    rid, status, data, _ = run_request(
        client, "paper.note.export", payload,
        transfer_key=args.transfer_key, timeout=args.timeout, purge=args.purge,
        expect_owner=args.user,
    )
    if not isinstance(data, dict):
        data = {"result": data}
    default_name = f"paper-export-{args.doc_id or rid}.json"
    saved = None
    if not args.no_save:
        saved = save_json(data, args.out, default_name)
    summary = export_summary(rid, status, data, saved)
    if args.json:
        emit(summary)
    else:
        emit(summary)
        if saved:
            print(
                f"\n[提示] 完整导出在 {saved}（{summary['bytes']:,} 字节）。"
                "别整份读进上下文：先用摘要判断，需要正文再分段读该文件。",
                file=sys.stderr,
            )
    return 0 if status == "done" else 3


def cmd_list(client: Client, args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {}
    if args.doc_id:
        payload["doc_id"] = args.doc_id
    if args.title:
        payload["title"] = args.title
    rid, status, data, _ = run_request(
        client, "paper.note.list", payload,
        transfer_key=args.transfer_key, timeout=args.timeout, purge=args.purge,
    )
    result = {"request_id": rid, "status": status, "data": data}
    if args.out:
        print(f"[info] 结果已保存：{save_json(result, args.out, f'notes-{rid}.json')}", file=sys.stderr)
    emit(result)
    return 0 if status == "done" else 3


def cmd_result(client: Client, args: argparse.Namespace) -> int:
    if args.meta:
        emit(client.fetch_result_meta(args.request_id))
        return 0
    data = client.fetch_result(args.request_id, purge=args.purge)
    saved = None
    if data is not None and not args.stdout:
        saved = save_json(data, args.out, f"result-{args.request_id}.json")
        emit({"request_id": args.request_id, "saved": saved, "bytes": size_of(data)})
    else:
        emit(data)
    return 0 if data is not None else 4


def cmd_keys(client: Client, args: argparse.Namespace) -> int:
    action = args.action
    if action == "issue":
        try:
            key = client.issue_key(args.blob_id, args.ttl, args.label)
        except CommError as exc:
            if exc.status == 403 and "env_transfer_keys_disabled" in exc.detail:
                raise SystemExit(
                    "服务端已关闭旧的 tk:v2 签发（env_transfer_keys_disabled）：\n"
                    "  访问权只来自账号 transfer key（ptk_…），请到 tardigrade 网页"
                    "「导出 → 🔑 转交 Key」生成。"
                )
            raise
        emit({"transfer_key": key, "blob_id": args.blob_id, "ttl": args.ttl})
        print("[提示] 这是旧的 tk:v2（服务端已默认禁收），请改用账号 key", file=sys.stderr)
        return 0
    if action == "list":
        emit(client.list_keys())
        return 0
    if action == "revoke":
        if not args.key_id:
            raise SystemExit("revoke 需要 --key-id")
        emit(client.revoke_key(args.key_id, purge=args.purge))
        return 0
    raise SystemExit(f"未知 --action：{action}")


# ------------------------------- 入口 -------------------------------------- #
def detect_base_url(explicit: str | None) -> str:
    if explicit:
        return explicit.rstrip("/")
    env = os.environ.get("PAPER_COMM_URL")
    if env:
        return env.rstrip("/")
    for candidate in DEFAULT_URLS:
        try:
            with request.urlopen(f"{candidate}/healthz", timeout=2):
                return candidate
        except Exception:
            continue
    return DEFAULT_URLS[0]


def build_parser(env: dict[str, str] | None = None) -> argparse.ArgumentParser:
    env = env or {}
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", default=pick_env(env, "PAPER_COMM_URL", "AGENT_COMM_URL"),
                        help="通信服务地址（默认自动探测）")
    common.add_argument("--agent-id",
                        default=pick_env(env, "PAPER_COMM_AGENT_ID", "AGENT_ID",
                                         default="research-agent-v1"))
    common.add_argument("--api-key", default=None,
                        help="传输层 agent 身份（服务端 AGENT_SECRETS 的密钥），不是账号 key")
    common.add_argument("--secret", default=None, help="签名密钥（REQUIRE_SIGNATURE=1 时用）")
    common.add_argument("--env-file", default=None, help="凭据 .env 路径（默认自动查找）")
    common.add_argument(
        "--transfer-key", default=None,
        help="账号 transfer key（ptk_…，tardigrade 网页「🔑 转交 Key」生成）；"
             "也可在 .env 里写 PAPER_COMM_TRANSFER_KEY",
    )
    common.add_argument("--timeout", type=float, default=180.0, help="等待终态的超时秒数")
    common.add_argument("--purge", action="store_true", help="取回完整结果后立即删除")

    parser = argparse.ArgumentParser(
        prog="paper_comm.py", description="与论文阅读器通信服务的命令行客户端"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="探活", parents=[common])

    p_ask = sub.add_parser("ask", help="提问 / 核验论断", parents=[common])
    p_ask.add_argument("--intent", required=True, choices=sorted(KNOWN_INTENTS))
    p_ask.add_argument("--payload", default=None, help="JSON 字符串，例如 '{\"question\":\"…\"}'")
    p_ask.add_argument("--payload-file", default=None, help="从文件读 payload JSON")
    p_ask.add_argument("--out", default=None, help="把结果另存为 JSON")

    p_exp = sub.add_parser("export", help="导出整篇（全文/配图/笔记/参考文献检查）", parents=[common])
    p_exp.add_argument("--doc-id", default=None)
    p_exp.add_argument("--title", default=None)
    p_exp.add_argument("--user", default=None,
                       help="断言账号名：与网关盖章的 owner 不一致就报错（不再写进 payload）")
    p_exp.add_argument("--mode", choices=("mechanical", "agent"), default=None)
    p_exp.add_argument("--no-fallback", action="store_true", help="mode=agent 但无 LLM 时直接失败")
    p_exp.add_argument("--no-images", action="store_true")
    p_exp.add_argument("--no-fulltext", action="store_true")
    p_exp.add_argument("--image-budget-mb", type=float, default=None)
    p_exp.add_argument("--out", default=None)
    p_exp.add_argument("--no-save", action="store_true", help="不落盘，只在终端打印摘要")
    p_exp.add_argument("--json", action="store_true", help="只打印摘要 JSON")

    p_list = sub.add_parser("list", help="列出笔记", parents=[common])
    p_list.add_argument("--doc-id", default=None)
    p_list.add_argument("--title", default=None)
    p_list.add_argument("--out", default=None)

    p_res = sub.add_parser("result", help="补取完整结果", parents=[common])
    p_res.add_argument("--request-id", required=True)
    p_res.add_argument("--meta", action="store_true", help="只看元信息，不下载正文")
    p_res.add_argument("--out", default=None)
    p_res.add_argument("--stdout", action="store_true", help="直接打印到终端")

    p_key = sub.add_parser("keys", help="旧的 tk:v2 令牌（服务端默认已关闭，请用账号 key）",
                           parents=[common])
    p_key.add_argument("--action", required=True, choices=("issue", "list", "revoke"))
    p_key.add_argument("--blob-id", default=None)
    p_key.add_argument("--ttl", type=int, default=None)
    p_key.add_argument("--label", default="")
    p_key.add_argument("--key-id", default=None)

    return parser


HANDLERS = {
    "status": cmd_status,
    "ask": cmd_ask,
    "export": cmd_export,
    "list": cmd_list,
    "result": cmd_result,
    "keys": cmd_keys,
}


def preload_env(argv: list[str]) -> tuple[str | None, dict[str, str]]:
    """先扫一遍 argv 找 --env-file，再加载 .env（参数解析前就需要默认值）。"""
    explicit = None
    for i, arg in enumerate(argv):
        if arg == "--env-file" and i + 1 < len(argv):
            explicit = argv[i + 1]
        elif arg.startswith("--env-file="):
            explicit = arg.split("=", 1)[1]
    path = find_env_file(explicit)
    if not path:
        return None, {}
    try:
        return path, load_env_file(path)
    except OSError as exc:
        print(f"[warn] 读取 .env 失败（{path}）：{exc}", file=sys.stderr)
        return None, {}


def main(argv: list[str] | None = None) -> int:
    global ENV_FILE_PATH, AUTH_SOURCE, TRANSFER_KEY_SOURCE
    raw_argv = list(sys.argv[1:] if argv is None else argv)

    ENV_FILE_PATH, env = preload_env(raw_argv)
    args = build_parser(env).parse_args(raw_argv)

    api_key, secret, AUTH_SOURCE = resolve_auth(env, args.agent_id, args.api_key, args.secret)
    # 常见误配：把账号 key 填到了 X-Api-Key 里（那会 401）。自动挪到信封 transfer_key。
    if api_key and api_key.strip().startswith(ACCOUNT_KEY_PREFIX):
        print(
            "[warn] PAPER_COMM_API_KEY 里填的是账号 transfer key（ptk_…）："
            "它是信封的 transfer_key，不是 X-Api-Key；已自动改用它。",
            file=sys.stderr,
        )
        if not args.transfer_key:
            args.transfer_key = api_key.strip()
        api_key = secret_from_agent_secrets(pick_env(env, "AGENT_SECRETS") or "", args.agent_id)
        secret = secret or api_key
        AUTH_SOURCE = (".env: AGENT_SECRETS（api key）" if api_key
                       else "无（需补 X-Api-Key，但准入仍看 ptk_）")

    args.transfer_key, TRANSFER_KEY_SOURCE = resolve_transfer_key(env, args.transfer_key)

    if ENV_FILE_PATH:
        print(
            f"[info] 凭据来自 {ENV_FILE_PATH} → agent={args.agent_id} "
            f"传输鉴权：{AUTH_SOURCE}",
            file=sys.stderr,
        )
    print(
        f"[info] 账号 key（准入）：{key_hint(args.transfer_key)} ← {TRANSFER_KEY_SOURCE}",
        file=sys.stderr,
    )

    # 需要数据授权的子命令：没有账号 key 就别发（肯定 403），先说清楚怎么拿
    if args.cmd in ("ask", "export", "list"):
        args.transfer_key = require_transfer_key(args.transfer_key, TRANSFER_KEY_SOURCE)

    client = Client(
        detect_base_url(args.base_url),
        args.agent_id,
        api_key=api_key,
        secret=secret,
    )
    try:
        return HANDLERS[args.cmd](client, args)
    except CommError as exc:
        hint = comm_hint(exc)
        print(json.dumps({"error": str(exc), "hint": hint}, ensure_ascii=False), file=sys.stderr)
        return 1


def comm_hint(exc: CommError) -> str:
    """按服务端的拒绝原因给具体下一步（新模型把准入错误都放在 detail 里）。"""
    detail = exc.detail or ""
    if exc.status == 401:
        return ("传输层身份问题，别重试：skill 的 PAPER_COMM_API_KEY 必须等于服务端 "
                "AGENT_SECRETS 里该 agent 的密钥（改完服务端要 docker compose up -d 重启）；"
                "注意 X-Api-Key 不是账号 ptk_，先跑 status 看 transport_ok")
    if exc.status == 403:
        if "account_key_required" in detail:
            return ("缺账号 transfer key：在 tardigrade 网页「导出 → 🔑 转交 Key」生成，"
                    "传 --transfer-key ptk_xxx 或写进 .env 的 PAPER_COMM_TRANSFER_KEY")
        if "transfer_key_unknown" in detail:
            return "key 不存在 / 已被删除 / 账号已注销：重新生成一把，别重试同一把"
        if "owner_mismatch" in detail:
            return "payload 里的 user 与 key 归属账号不一致：删掉自报的 user，账号由 key 决定"
        if "transfer_key_env_key_rejected" in detail:
            return "用了已停用的 tk:v2：改用账号 key（ptk_…）"
        if "env_transfer_keys_disabled" in detail:
            return "服务端已关闭 tk:v2 签发：请到 tardigrade 网页生成账号 key"
        if "stamp_" in detail or "account_key_id" in detail:
            return "请求在网关盖章/校验阶段被拒：重新提交；持续出现请查服务端日志"
        return "准入被拒（403）：先跑 status 看账号 key 是否配置正确"
    if exc.status == 422:
        return "参数或内容不合规，别重试：按 detail 改 payload / intent；owner/auth 属服务端字段不可自填"
    if exc.status == 404:
        return "结果不存在或已过 RESULT_TTL（默认 1 小时）"
    return "可稍后重试；429/5xx 已自动退避 3 次"


if __name__ == "__main__":
    sys.exit(main())
