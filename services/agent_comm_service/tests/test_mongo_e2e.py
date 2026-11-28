"""端到端检查：科研 agent 用 transfer_key 请求通信模块，并确认整条链路真正落到 MongoDB。

覆盖：
  1. 服务存活（GET /healthz）
  2. MongoDB 可连（用 .env.example 里的 MONGO_URL，宿主侧 127.0.0.1）
  3. 签发 transfer_key → 文档写入 paper.agent_transfer_keys
  4. 库里只有不可逆摘要：没有明文 secret，hash 是 64 位 hex
  5. 科研 agent 带 key 提交请求 → 202（说明 guard 读库校验通过）
  6. 篡改 secret / 未知 key / 已吊销 key → 403，理由分别是 bad_secret / unknown_key / revoked
  7. 吊销后库里 revoked_at 不为空；列表接口不泄露 hash

用法（需服务已启动、Mongo 在跑）：
    python tests/test_mongo_e2e.py                     # 优先读 .env，没有则 .env.example
    python tests/test_mongo_e2e.py --strict-pepper     # 摘要必须与 KEY_HASH_PEPPER 一致
    python tests/test_mongo_e2e.py --env-file .env.example --base-url http://127.0.0.1:9500
    python tests/test_mongo_e2e.py --keep              # 不清理测试数据

退出码：0 = 全部通过（或服务不可达时 skip），1 = 有失败项。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import pathlib
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values  # noqa: E402
from pymongo import MongoClient  # noqa: E402
from pymongo.errors import PyMongoError  # noqa: E402

from sdk.client import AgentCommClient, AgentCommError  # noqa: E402

INTENT = "paper.insight.validate"
PAYLOAD = {"claim": "多头注意力等价于加权特征融合"}


# --------------------------------------------------------------------------- #
# 断言小工具
# --------------------------------------------------------------------------- #
class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.warned = 0

    def check(self, cond: bool, title: str, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            print(f"[PASS] {title}" + (f"  ({detail})" if detail else ""))
        else:
            self.failed += 1
            print(f"[FAIL] {title}" + (f"  ({detail})" if detail else ""))
        return cond

    def info(self, title: str, detail: str = "") -> None:
        print(f"[INFO] {title}" + (f"  ({detail})" if detail else ""))

    def warn(self, title: str, detail: str = "") -> None:
        self.warned += 1
        print(f"[WARN] {title}" + (f"  ({detail})" if detail else ""))

    def summary(self) -> int:
        print(f"\n结果：{self.passed} passed, {self.failed} failed, {self.warned} warnings")
        return 0 if self.failed == 0 else 1


def hash_secret(pepper: str, secret: str) -> str:
    return hmac.new(f"tk-store-v1|{pepper}".encode(), secret.encode(), hashlib.sha256).hexdigest()


# `.env` 是给容器写的（mongo:27017），宿主侧要换成 127.0.0.1
CONTAINER_HOSTS = {"mongo", "paper-mongo"}


def host_side_mongo_url(url: str, port_hint: str | None) -> tuple[str, bool]:
    parts = urlsplit(url)
    if (parts.hostname or "") not in CONTAINER_HOSTS:
        return url, False
    auth, _, _hostport = parts.netloc.rpartition("@")
    port = port_hint or "27017"
    netloc = f"{auth}@127.0.0.1:{port}" if auth else f"127.0.0.1:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), True


def split_key(key: str) -> tuple[str, str]:
    rest = key[len("tk:v2.") :]
    key_id, _, secret = rest.partition(".")
    return key_id, secret


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def run(args: argparse.Namespace, cfg: dict[str, str]) -> int:
    rep = Report()
    base = args.base_url.rstrip("/")
    agent = args.agent
    api_key = args.api_key

    mongo_url = args.mongo_url or cfg.get("MONGO_URL") or ""
    db_name = cfg.get("MONGO_DB") or "paper"
    col_name = cfg.get("KEY_COLLECTION") or "agent_transfer_keys"
    pepper = cfg.get("KEY_HASH_PEPPER") or ""

    rep.info("配置来源", args.env_file)
    rep.info("服务地址", base)
    mongo_url, rewritten = host_side_mongo_url(mongo_url, cfg.get("MONGO_PORT"))
    if rewritten:
        rep.info("MongoDB 地址已从容器名改写为宿主侧", mongo_url.split("@")[-1])
    rep.info("MongoDB", f"{db_name}.{col_name}")

    # ---------------- 1. MongoDB 可连 ----------------
    client = MongoClient(mongo_url, serverSelectionTimeoutMS=4000, tz_aware=True)
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        rep.check(False, "MongoDB 可连接", str(exc)[:160])
        return rep.summary()
    coll = client[db_name][col_name]
    rep.check(True, "MongoDB 可连接（宿主侧）", mongo_url.split("@")[-1])

    # ---------------- 2. 服务存活 ----------------
    async with AgentCommClient(base, agent, api_key=api_key) as c:
        import httpx

        async with httpx.AsyncClient(timeout=args.timeout) as raw:
            headers = {"X-Agent-Id": agent}
            if api_key:
                headers["X-Api-Key"] = api_key

            try:
                health = await raw.get(f"{base}/healthz")
            except Exception as exc:
                print(f"skip: 服务不可达 {base}（{exc!r}）")
                return 0
            rep.check(health.status_code == 200, "服务存活 /healthz", f"HTTP {health.status_code}")

            # ---------------- 3. 签发 key ----------------
            resp = await raw.post(
                f"{base}/v1/transfer-keys",
                json={"ttl": 300, "label": "e2e-mongo"},
                headers=headers,
            )
            rep.check(resp.status_code == 200, "签发 transfer_key", f"HTTP {resp.status_code}")
            if resp.status_code != 200:
                print(resp.text[:300])
                return rep.summary()

            issued = resp.json()
            key = issued["transfer_key"]
            key_id = issued["key_id"]
            key_id_from_key, secret = split_key(key)
            rep.check(key.startswith("tk:v2."), "key 格式为 tk:v2.<key_id>.<secret>", key[:20] + "...")
            rep.check(key_id_from_key == key_id, "响应 key_id 与 key 内嵌一致")
            rep.check(issued.get("expires_at", 0) > time.time(), "有效期在未来")

            # ---------------- 4. 落库检查 ----------------
            doc = coll.find_one({"_id": key_id})
            rep.check(doc is not None, "文档已写入 MongoDB", f"_id={key_id}")
            if doc is None:
                return rep.summary()

            rep.check(doc.get("owner") == agent, "文档 owner 绑定科研 agent", str(doc.get("owner")))
            exp_at = doc.get("exp_at")
            exp_ok = isinstance(exp_at, datetime) and (
                exp_at if exp_at.tzinfo else exp_at.replace(tzinfo=timezone.utc)
            ) > datetime.now(timezone.utc)
            rep.check(exp_ok, "文档 exp_at 未过期", str(exp_at))
            stored = str(doc.get("hash") or "")
            rep.check(len(stored) == 64 and all(ch in "0123456789abcdef" for ch in stored),
                      "库中只有 64 位 hex 摘要", stored[:16] + "...")
            rep.check(secret not in json.dumps(doc, default=str), "库中没有任何明文 secret")
            rep.check(doc.get("revoked_at") is None, "初始状态未吊销")

            # pepper 一致性：真实部署下应当一致；--strict-pepper 时不考虑容错，直接判失败
            env_name = pathlib.Path(args.env_file).name
            matched = bool(pepper) and hash_secret(pepper, secret) == stored
            if matched:
                rep.info(f"库中摘要与 {env_name} 的 KEY_HASH_PEPPER 一致")
            elif args.strict_pepper:
                rep.check(
                    False,
                    f"库中摘要与 {env_name} 的 KEY_HASH_PEPPER 一致",
                    "服务端用的是别的 pepper（检查 .env 是否写入、容器是否重建）",
                )
            else:
                rep.warn(
                    f"库中摘要与 {env_name} 的 KEY_HASH_PEPPER 不一致",
                    "运行中的服务用了别的 pepper；加 --strict-pepper 可强制校验",
                )

            # 列表接口不泄露摘要
            listing = await raw.get(f"{base}/v1/transfer-keys", headers=headers)
            items = listing.json().get("items", [])
            mine = next((i for i in items if i.get("key_id") == key_id), None)
            rep.check(mine is not None, "列表接口能查到该 key")
            rep.check(mine is not None and "hash" not in mine, "列表接口不返回摘要")

            # ---------------- 5. 科研 agent 带 key 提交请求 ----------------
            rid = ""
            try:
                submitted = await c.submit(INTENT, payload=PAYLOAD, transfer_key=key, wait=False)
                rid = submitted["request_id"]
                rep.check(True, "科研 agent 带 transfer_key 提交成功", f"request_id={rid}")
                polled = await c.poll(rid)
                rep.check(
                    polled.get("status") in {"queued", "running", "done", "error", "cancelled"},
                    "请求可查询（说明已进通信模块）",
                    f"status={polled.get('status')}",
                )
            except AgentCommError as exc:
                rep.check(False, "科研 agent 带 transfer_key 提交成功", f"{exc.status}: {exc.detail[:120]}")

            # ---------------- 6. 反例：篡改 / 未知 ----------------
            tampered = key[:-1] + ("A" if key[-1] != "A" else "B")
            await _expect_reject(rep, c, tampered, "transfer_key_bad_secret", "篡改 secret 被拒")
            await _expect_reject(rep, c, "tk:v2.deadbeefdeadbeef.nope", "transfer_key_unknown_key", "未知 key 被拒")

            # ---------------- 7. 吊销 ----------------
            deleted = await raw.delete(f"{base}/v1/transfer-keys/{key_id}", headers=headers)
            rep.check(deleted.status_code == 200, "吊销 key", f"HTTP {deleted.status_code}")
            after = coll.find_one({"_id": key_id})
            rep.check(after is not None and after.get("revoked_at") is not None, "库中 revoked_at 已写入")
            await _expect_reject(rep, c, key, "transfer_key_revoked", "吊销后 key 立即失效")

            # ---------------- 清理 ----------------
            if not args.keep:
                if rid:
                    try:
                        await c.cancel(rid)
                    except AgentCommError as exc:
                        rep.info("取消测试请求失败（可忽略）", f"{exc.status}: {exc.detail[:80]}")
                purged = await raw.delete(
                    f"{base}/v1/transfer-keys/{key_id}", params={"purge": "true"}, headers=headers
                )
                rep.check(purged.status_code == 200, "清理：物理删除测试 key")
                rep.check(coll.find_one({"_id": key_id}) is None, "清理后库里已无该文档")
            else:
                rep.info("--keep：保留测试数据", key_id)

    client.close()
    return rep.summary()


async def _expect_reject(rep: Report, c: AgentCommClient, key: str, reason: str, title: str) -> None:
    try:
        await c.submit(INTENT, payload=PAYLOAD, transfer_key=key, wait=False)
    except AgentCommError as exc:
        rep.check(
            exc.status == 403 and reason in exc.detail,
            title,
            f"{exc.status} {reason}",
        )
        return
    rep.check(False, title, "竟然被接受了")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="transfer_key → MongoDB 链路端到端检查")
    p.add_argument(
        "--env-file",
        default=None,
        help="读取 MONGO_*/KEY_HASH_PEPPER；缺省用 .env，没有则用 .env.example",
    )
    p.add_argument(
        "--base-url",
        default="http://127.0.0.1:9500",
        help="通信模块地址（默认 compose 对外端口 9500）",
    )
    p.add_argument("--agent", default="research-agent-v1")
    p.add_argument("--api-key", default=None, help="缺省取 env 文件里 AGENT_SECRETS 中该 agent 的密钥")
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--mongo-url", default=None, help="直接指定宿主侧连接串（覆盖 env 文件）")
    p.add_argument(
        "--strict-pepper",
        action="store_true",
        help="库中摘要与 KEY_HASH_PEPPER 不一致时直接判失败（校验真实部署配置）",
    )
    p.add_argument("--keep", action="store_true", help="不清理测试数据")
    return p.parse_args()


def load_cfg(path: pathlib.Path) -> dict[str, str]:
    """以 .env.example 为底，再用指定文件覆盖（.env 通常只写真实 secret）。"""
    base = ROOT / ".env.example"
    cfg = {k: v for k, v in dotenv_values(base).items() if v is not None}
    if path.resolve() != base.resolve():
        cfg.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    return cfg


def main() -> int:
    args = parse_args()
    if args.env_file is None:
        real = ROOT / ".env"
        args.env_file = str(real if real.is_file() else ROOT / ".env.example")
    path = pathlib.Path(args.env_file)
    if not path.is_file():
        print(f"找不到环境文件：{path}")
        return 1
    cfg = load_cfg(path)

    # 新模型：准入只认账号 transfer key（ptk_…），旧的 tk:v2 签发默认关闭。
    # 本脚本测的是旧链路，所以要求显式打开；否则直接跳过（不是失败）。
    if (cfg.get("ALLOW_ENV_TRANSFER_KEYS") or "0").strip().lower() not in (
            "1", "true", "yes", "on"):
        print("skip: ALLOW_ENV_TRANSFER_KEYS=0（新模型已关闭旧的 tk:v2 链路）；"
              "要测旧的 pepper 路径请显式设 ALLOW_ENV_TRANSFER_KEYS=1")
        return 0

    if not args.api_key:
        try:
            secrets: dict[str, str] = json.loads(cfg.get("AGENT_SECRETS") or "{}")
        except json.JSONDecodeError:
            secrets = {}
        args.api_key = secrets.get(args.agent)
    if not cfg.get("MONGO_URL"):
        print("环境文件里缺少 MONGO_URL")
        return 1

    return asyncio.run(run(args, cfg))


if __name__ == "__main__":
    raise SystemExit(main())
