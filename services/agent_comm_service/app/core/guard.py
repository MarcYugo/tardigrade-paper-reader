"""载荷检测 + transfer_key 签发/校验。

设计原则：
  * 快检（HTTP 入口）只判 BLOCK 级硬命中，拒绝得早、代价低；
  * 深检（raw 流消费者）再补 QUARANTINE 级可疑项，可疑即隔离，绝不进 reader agent。
  * payload 永远只被当作数据，任何位置都不执行 / 反序列化。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterator

from env_load import (
    ACCOUNT_KEY_PREFIX,
    ACCOUNT_KEY_REQUIRED,
    ALLOW_ENV_TRANSFER_KEYS,
    ALLOWED_INTENTS,
    KEY_DEFAULT_TTL,
    KEY_HASH_PEPPER,
    KEY_MAX_TTL,
    MAX_INLINE_KB,
    MAX_PAYLOAD_DEPTH,
)
from . import accounts, stamp
from .keystore import KeyStore, get_store
from .models import AuthStamp, RequestEnvelope


class Level(str, Enum):
    PASS = "pass"
    BLOCK = "block"
    QUARANTINE = "quarantine"


@dataclass(frozen=True)
class Verdict:
    level: Level
    reason: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.level is Level.PASS


PASS = Verdict(Level.PASS)
_BLOCK = Level.BLOCK
_QUAR = Level.QUARANTINE

# name, pattern, level, 是否在 HTTP 快检阶段就判定
_RULES: tuple[tuple[str, re.Pattern[str], Level, bool], ...] = (
    (
        "shell_chain",
        re.compile(
            r"(\$\(|`[^`\n]{1,80}`|\brm\s+-rf\b|;\s*(rm|sh|bash|curl|wget|nc|python|powershell)\b"
            r"|&&\s*(rm|sh|bash|curl|wget)\b|\|\s*(sh|bash)\b)",
            re.I,
        ),
        _BLOCK,
        True,
    ),
    ("path_traversal", re.compile(r"(\.\.[/\\]|%2e%2e%2f)", re.I), _BLOCK, True),
    (
        "abs_path",
        re.compile(r"(/etc/|/proc/|/root/|/var/run/|[A-Za-z]:\\\\?(Windows|Users|Program))", re.I),
        _BLOCK,
        True,
    ),
    ("script_tag", re.compile(r"<\s*/?\s*(script|iframe|object|embed|link|meta)\b", re.I), _BLOCK, True),
    ("data_uri", re.compile(r"\bdata:[a-z]+/[a-z0-9.+-]+;base64,", re.I), _BLOCK, True),
    (
        "sql_inject",
        re.compile(r"(\bunion\s+select\b|\bdrop\s+table\b|\bor\s+1\s*=\s*1\b|'\s*or\s*'1'\s*=\s*'1)", re.I),
        _BLOCK,
        True,
    ),
    ("nosql_inject", re.compile(r"(\$where\b|\$ne\s*:|\$gt\s*:|\$function\b)"), _BLOCK, True),
    (
        "bidi_ctrl",
        re.compile(r"[\u202a-\u202e\u2066-\u2069\u200b-\u200f]|[\u0000-\u0008\u000b\u000c\u000e-\u001f]"),
        _QUAR,
        False,
    ),
    (
        "prompt_inject",
        re.compile(
            r"(ignore\s+(all\s+)?previous\s+instructions|disregard\s+(the\s+)?previous"
            r"|<\|\s*im_start\s*\|>|\[\[\s*system\s*\]\]|you\s+are\s+now\s+a)",
            re.I,
        ),
        _QUAR,
        False,
    ),
    ("long_b64", re.compile(r"[A-Za-z0-9+/]{512,}={0,2}"), _QUAR, False),
)


# --------------------------------------------------------------------------- #
# 递归遍历 / 度量
# --------------------------------------------------------------------------- #
def _iter_strings(obj: Any, depth: int = 0) -> Iterator[str]:
    if depth > MAX_PAYLOAD_DEPTH + 4:      # 兜底：不让异常结构把检测拖死
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key)
            yield from _iter_strings(value, depth + 1)
    elif isinstance(obj, (list, tuple, set)):
        for value in obj:
            yield from _iter_strings(value, depth + 1)


def payload_depth(obj: Any) -> int:
    if isinstance(obj, dict) and obj:
        return 1 + max(payload_depth(v) for v in obj.values())
    if isinstance(obj, (list, tuple)) and obj:
        return 1 + max(payload_depth(v) for v in obj)
    return 0


def _size_kb(payload_json: str) -> float:
    return len(payload_json.encode()) / 1024


# --------------------------------------------------------------------------- #
# transfer_key：tk:v2.<key_id>.<secret>
#   * key_id 可安全写日志；secret 只以不可逆摘要入库（与 LLM API key 同款做法）；
#   * 明文只在签发响应里返回一次，服务端此后不持有 → 无需可逆加密，
#     因此**不存在"第二个解密密钥"**；
#   * 校验完全基于库里那一条记录：存在性 + 摘要 + 未吊销 + 未过期 + 归属，
#     "可生成 / 限时可用 / 可删除"因此都是文档字段的自然结果。
# --------------------------------------------------------------------------- #
KEY_PREFIX = "tk:v2."


def _pepper() -> bytes:
    # 域分离前缀 + 专用 pepper；pepper 只作用于不可逆摘要，不存在解密环节
    return f"tk-store-v1|{KEY_HASH_PEPPER}".encode()


def hash_secret(secret: str) -> str:
    """不可逆摘要：即使拖库也无法还原出可用 key。"""
    return hmac.new(_pepper(), secret.encode(), hashlib.sha256).hexdigest()


def _new_key_pair() -> tuple[str, str]:
    return secrets.token_hex(8), secrets.token_urlsafe(32)


async def issue_transfer_key(
    owner: str,
    blob_id: str | None = None,
    ttl: int | None = None,
    label: str = "",
    store: KeyStore | None = None,
) -> tuple[str, dict[str, Any]]:
    """签发一把 key，返回 (明文 key, 入库文档)。明文只此一次，服务端不保留。"""
    ttl_s = max(60, min(int(ttl if ttl is not None else KEY_DEFAULT_TTL), KEY_MAX_TTL))
    now = datetime.now(timezone.utc)
    ks = store or get_store()
    for _ in range(3):
        key_id, secret = _new_key_pair()
        doc: dict[str, Any] = {
            "_id": key_id,
            "owner": owner,
            "blob_id": blob_id,
            "label": (label or "")[:64],
            "hash": hash_secret(secret),
            "created_at": now,
            "exp_at": now + timedelta(seconds=ttl_s),
            "revoked_at": None,
            "last_used_at": None,
        }
        try:
            await ks.add(doc)
        except Exception as exc:      # 仅 _id 撞车（几乎不可能）才换一对重试
            if type(exc).__name__ in ("DuplicateKeyError", "KeyError"):
                continue
            raise
        return f"{KEY_PREFIX}{key_id}.{secret}", doc
    raise RuntimeError("transfer_key 签发失败：id 重复")


async def verify_transfer_key(
    key: str | None,
    owner: str | None = None,
    blob_id: str | None = None,
    store: KeyStore | None = None,
) -> tuple[bool, str]:
    if not key:
        return False, "bad_format"
    if not key.startswith(KEY_PREFIX):
        return False, "bad_format"
    key_id, sep, secret = key[len(KEY_PREFIX) :].partition(".")
    if not sep or not key_id or not secret:
        return False, "bad_format"

    ks = store or get_store()
    try:
        doc = await ks.get(key_id)
    except Exception:
        return False, "store_unavailable"      # 失败即拒：宁可误杀，不可放行
    if doc is None:
        return False, "unknown_key"
    if not hmac.compare_digest(str(doc.get("hash") or ""), hash_secret(secret)):
        return False, "bad_secret"
    if doc.get("revoked_at") is not None:
        return False, "revoked"
    exp_at = doc.get("exp_at")
    if not isinstance(exp_at, datetime) or exp_at <= datetime.now(timezone.utc):
        return False, "expired"
    if owner is not None and doc.get("owner") != owner:
        return False, "owner_mismatch"
    scope = doc.get("blob_id")
    if scope and blob_id is not None and scope != blob_id:
        return False, "blob_mismatch"

    with suppress(Exception):       # 使用时间只是旁路审计信息，写失败不影响放行
        await ks.touch(key_id, datetime.now(timezone.utc))
    return True, ""


async def transfer_blob_id(key: str, store: KeyStore | None = None) -> str | None:
    if not key or not key.startswith(KEY_PREFIX):
        return None
    key_id, sep, _ = key[len(KEY_PREFIX) :].partition(".")
    if not sep or not key_id:
        return None
    try:
        doc = await (store or get_store()).get(key_id)
    except Exception:
        return None
    return str((doc or {}).get("blob_id") or "") or None


# --------------------------------------------------------------------------- #
# 检测入口
# --------------------------------------------------------------------------- #
def _scan_payload(payload: dict[str, Any], *, fast_only: bool) -> Verdict:
    for text in _iter_strings(payload):
        for name, pattern, level, fast in _RULES:
            if fast_only and not fast:
                continue
            if pattern.search(text):
                return Verdict(level, name, text[:120])
    return PASS


async def fast_check(env, payload_json: str | None = None) -> Verdict:
    """HTTP 入口快检：只拦硬命中，可疑项留给深检。

    注意：这里**只**处理旧的 `tk:v2` 前缀；账号 key（`ptk_`）的权威校验在
    `authorize()` 里做（那是唯一的准入决策点），这里不掺和。
    """
    if env.intent not in ALLOWED_INTENTS:
        return Verdict(Level.BLOCK, "intent_not_allowed", env.intent[:64])

    size = _size_kb(payload_json) if payload_json is not None else _size_kb(env.model_dump_json())
    if size > MAX_INLINE_KB:
        return Verdict(Level.BLOCK, "payload_too_large", f"{size:.1f}KB > {MAX_INLINE_KB}KB")

    if env.transfer_key and env.transfer_key.startswith(KEY_PREFIX):
        if not ALLOW_ENV_TRANSFER_KEYS:
            return Verdict(Level.BLOCK, "transfer_key_env_key_rejected",
                           "只接受账号 transfer key（ptk_…）")
        ok, why = await verify_transfer_key(env.transfer_key, owner=env.from_agent)
        if not ok:
            return Verdict(Level.BLOCK, f"transfer_key_{why}", "")

    return _scan_payload(env.payload, fast_only=True)


# --------------------------------------------------------------------------- #
# 准入（唯一决策点）：账号 transfer key → 归属账号
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Grant:
    """授权结果：`owner` 为空表示“本次没有账号归属”（仅调试模式会出现）。"""

    owner: str | None = None
    key_id: str = ""
    via: str = ""


async def authorize(env: RequestEnvelope) -> tuple[Grant, Verdict]:
    """校验账号 `ptk_` 并给出归属账号 —— **所有校验都在通信模块内完成**。

    规则：
      * 缺 key：`ACCOUNT_KEY_REQUIRED=1` 时直接拒（否则退回旧的“reader 自己推断”）；
      * `tk:v2.`：`ALLOW_ENV_TRANSFER_KEYS=0`（默认）时一律拒 —— 它是 pepper 派生、
        与账号无关的旧凭据；
      * `ptk_`：查库（存在 + 账号还在），并核对客户端自报的 `payload.user` 不得与
        归属冲突（不允许自证身份）；
      * 其它前缀：格式错。
    """
    key = (env.transfer_key or "").strip()
    payload = env.payload if isinstance(env.payload, dict) else {}
    if not key:                                   # 兼容旧客户端：允许写在 payload 里
        key = str(payload.get("transfer_key") or "").strip()

    if not key:
        if ACCOUNT_KEY_REQUIRED:
            return Grant(), Verdict(Level.BLOCK, "account_key_required",
                                    "缺少账号 transfer key（ptk_…）")
        return Grant(), PASS

    if key.startswith(KEY_PREFIX):
        if not ALLOW_ENV_TRANSFER_KEYS:
            return Grant(), Verdict(Level.BLOCK, "transfer_key_env_key_rejected",
                                    "只接受账号 transfer key（ptk_…）")
        ok, why = await verify_transfer_key(key, owner=env.from_agent)
        if not ok:
            return Grant(), Verdict(Level.BLOCK, f"transfer_key_{why}", "")
        return Grant(via="env_key"), PASS

    if not key.startswith(ACCOUNT_KEY_PREFIX):
        return Grant(), Verdict(Level.BLOCK, "transfer_key_bad_format", key[:16])

    try:
        acct = await accounts.get_store().resolve(key)
    except Exception as exc:                      # 失败即拒：宁可误杀，不可放行
        return Grant(), Verdict(Level.BLOCK, "transfer_key_store_unavailable",
                                type(exc).__name__)
    if not acct:
        return Grant(), Verdict(Level.BLOCK, "transfer_key_unknown",
                                "key 不存在 / 已删除 / 账号已注销")

    claimed = str(payload.get("user") or payload.get("username") or "").strip()
    if claimed and claimed.lower() != str(acct["username"]).lower():
        return Grant(), Verdict(Level.BLOCK, "owner_mismatch",
                                f"payload 自报 {claimed} ≠ key 归属")
    return Grant(owner=str(acct["username"]), key_id=str(acct.get("key_id") or ""),
                 via="account_key"), PASS


def stamped_envelope(env: RequestEnvelope, grant: Grant, rid: str) -> RequestEnvelope:
    """把授权结果“盖”到信封上，并归一化 payload（reader 只读这里的结果）。

    * 明文 key 一律抹掉（不再随信封进 Redis / 日志），只在 `auth.key_id` 留审计 id；
    * `payload.transfer_key` / `payload.username` 删除，`payload.user` 固定为归属账号；
    * `auth.mac` 绑定 request_id + from_agent + owner + key_id + intent + 时间戳。
    """
    payload = dict(env.payload or {})
    payload.pop("transfer_key", None)
    payload.pop("username", None)
    owner = grant.owner or ""
    auth: AuthStamp | None = None
    if owner or grant.via:
        if owner:
            payload["user"] = owner
        stamped_at = int(time.time())
        auth = AuthStamp(
            via=grant.via or "account_key",
            owner=owner,
            key_id=grant.key_id,
            from_agent=env.from_agent,
            stamped_at=stamped_at,
            mac=stamp.sign(request_id=rid, from_agent=env.from_agent, owner=owner,
                           key_id=grant.key_id, intent=env.intent,
                           stamped_at=stamped_at),
        )
    return env.model_copy(update={"request_id": rid, "transfer_key": None,
                                  "owner": owner or None, "auth": auth,
                                  "payload": payload})


def check_stamp(env: RequestEnvelope) -> Verdict:
    """校验网关盖章：MAC + 归属绑定（防止有人绕过入口直接往流里塞消息）。"""
    auth = env.auth
    if auth is None:                             # 未盖章（旧消息 / 直接调用 deep_check）
        return PASS
    owner = str(env.owner or "")
    if not owner or owner != auth.owner:
        return Verdict(Level.BLOCK, "stamp_owner_mismatch", "")
    if auth.from_agent != env.from_agent:
        return Verdict(Level.BLOCK, "stamp_agent_mismatch", "")
    ok, why = stamp.verify(
        request_id=str(env.request_id or ""),
        from_agent=auth.from_agent, owner=auth.owner, key_id=auth.key_id,
        intent=env.intent, stamped_at=auth.stamped_at, mac=auth.mac,
    )
    if not ok:
        return Verdict(Level.BLOCK, why, "")
    if ACCOUNT_KEY_REQUIRED:
        # reader 侧读的是 payload.user；盖章后必须与 auth.owner 完全一致，不许自报
        bound = str((env.payload or {}).get("user") or "")
        if bound != auth.owner:
            return Verdict(Level.BLOCK, "stamp_owner_not_bound", bound[:32])
        if "transfer_key" in (env.payload or {}):
            return Verdict(Level.BLOCK, "stamp_key_not_stripped", "")
    return PASS


async def deep_check(env) -> Verdict:
    """raw 流上的深检：快检 + 盖章 + 结构限制 + 可疑项隔离。"""
    verdict = await fast_check(env)
    if not verdict.ok:
        return verdict

    verdict = check_stamp(env)
    if not verdict.ok:
        return verdict

    depth = payload_depth(env.payload)
    if depth > MAX_PAYLOAD_DEPTH:
        return Verdict(Level.BLOCK, "payload_too_deep", str(depth))

    verdict = _scan_payload(env.payload, fast_only=False)
    if not verdict.ok:
        return verdict

    # 挂载点：需要时可在此接 LLM 提示注入复核 / 恶意载荷分类器，
    # 返回 QUARANTINE 即可让消息进 DLQ 而不进 reader agent。
    return PASS
