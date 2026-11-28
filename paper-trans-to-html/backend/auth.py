"""模块四 · 身份认证与登录会话

- 账号：口令只存 **PBKDF2-SHA256 加盐哈希**(20 万轮)，不存明文；校验用常量时间比较。
- 会话：登录成功后签发随机 token，写进 HttpOnly Cookie(`ptsid`)，默认 14 天过期。
- 首次使用判定：库里没有任何账号时 `initialized=False`，前端据此引导
  “创建管理员账号”（而不是登录）。
- 登录限流：同一「用户名 + IP」连续失败 `_MAX_FAILS` 次后锁定 `_LOCK_SECONDS` 秒
  （内存计数，重启即清，属“够用”级别）。

**存储后端可插拔**（由 `storage.py` 装配）：

| 后端 | 账号 | 会话 | transfer key |
| --- | --- | --- | --- |
| MongoDB | 集合 `users` | 集合 `sessions`（TTL 索引自动过期） | 集合 `transfer_keys` |
| 本地 JSON | `data/users.json` | `data/sessions.json` | `data/transfer_keys.json` |

本模块只定义“后端要提供哪些方法”（`users_count/users_all/user_get/user_put/user_delete`、
`session_get/session_put/session_delete/sessions_prune/sessions_delete_user`、
`transfer_list/transfer_add/transfer_remove/transfer_find/transfer_touch/
 transfer_drop_user`），实现分别在 `mongo_store.MongoAuthBackend` 与下面的
`JsonAuthBackend`。

**transfer key**（「转交 Key」）：账号在导出弹窗里生成的一次性可见令牌，
其他 Agent 拿着它（`Authorization: Bearer <key>`）就能读取**该账号**的论文阅读数据，
不需要账号口令、也不占用登录会话。库里按账号存：`_id = 用户名(小写)`，
`keys = [{id, name, key, created_at, last_used_at, uses}]`。

只做单机自用场景的“够用”安全：口令加盐哈希 + HttpOnly/SameSite=Lax Cookie +
登录限流；不含找回密码、邮箱验证、多角色权限（账号一律等同管理员）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

SESSION_COOKIE = "ptsid"          # 会话 Cookie 名
SESSION_DAYS = 14                 # 会话有效期(天)
TRANSFER_PREFIX = "ptk_"          # transfer key 前缀（与 agent_comm_service 的 tk: 区分）
TRANSFER_MAX = 20                 # 每个账号最多同时存在多少把 transfer key
TRANSFER_NAME_MAX = 40            # 备注名长度上限
_PBKDF2_ROUNDS = 200_000
_ALGO = "pbkdf2_sha256"
MIN_PASSWORD = 8
_USER_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,32}$")
_MAX_FAILS = 8
_LOCK_SECONDS = 300


class AuthError(Exception):
    """入参不合法 / 业务规则不满足，接口层转 400。"""


def _now() -> datetime:
    return datetime.now()


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _expired(rec: dict, now: datetime | None = None) -> bool:
    try:
        return datetime.fromisoformat(rec["expires_at"]) <= (now or _now())
    except Exception:
        return True


def hash_password(password: str, *, rounds: int = _PBKDF2_ROUNDS) -> str:
    """返回 `pbkdf2_sha256$轮数$盐(hex)$哈希(hex)`。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"{_ALGO}${rounds}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, hash_hex = (stored or "").split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
    except Exception:
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def validate_credentials(username: str, password: str) -> tuple[str, str]:
    """用户名/口令的基本格式校验，返回清洗后的 (username, password)。"""
    username = (username or "").strip()
    password = password or ""
    if not _USER_RE.match(username):
        raise AuthError("用户名需 3-32 位，仅限字母、数字、_ . @ -")
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"密码至少 {MIN_PASSWORD} 位")
    if len(password) > 200:
        raise AuthError("密码过长")
    return username, password


def _read_json(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class JsonAuthBackend:
    """账号/会话后端：落盘 `data/users.json`、`data/sessions.json`（无 MongoDB 时的回退）。"""

    name = "json"

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.users_file = self.root / "users.json"
        self.sessions_file = self.root / "sessions.json"
        self.transfer_file = self.root / "transfer_keys.json"
        self._lock = threading.RLock()
        self._users: dict[str, dict] = _read_json(self.users_file, {})
        self._sessions: dict[str, dict] = _read_json(self.sessions_file, {})
        self._transfer: dict[str, dict] = _read_json(self.transfer_file, {})
        if not self.transfer_file.exists():
            # 与账号存储同时建好（MongoDB 版在 backend 初始化时 create_index 建集合）
            _write_json(self.transfer_file, {})

    # ---- 账号 ----
    def users_count(self) -> int:
        with self._lock:
            return len(self._users)

    def users_all(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._users.values()]

    def user_get(self, key: str) -> dict | None:
        with self._lock:
            rec = self._users.get(key)
            return dict(rec) if rec else None

    def user_put(self, key: str, rec: dict) -> None:
        with self._lock:
            self._users[key] = dict(rec)
            _write_json(self.users_file, self._users)

    def user_delete(self, key: str) -> bool:
        with self._lock:
            if self._users.pop(key, None) is None:
                return False
            _write_json(self.users_file, self._users)
            return True

    # ---- 会话 ----
    def session_get(self, token: str) -> dict | None:
        with self._lock:
            rec = self._sessions.get(token)
            return dict(rec) if rec else None

    def session_put(self, token: str, rec: dict) -> None:
        with self._lock:
            self._sessions[token] = dict(rec)
            _write_json(self.sessions_file, self._sessions)

    def session_delete(self, token: str) -> None:
        with self._lock:
            if self._sessions.pop(token, None) is not None:
                _write_json(self.sessions_file, self._sessions)

    def sessions_prune(self, now_iso: str) -> None:
        try:
            now = datetime.fromisoformat(now_iso)
        except Exception:
            now = _now()
        with self._lock:
            alive = {t: s for t, s in self._sessions.items() if not _expired(s, now)}
            if len(alive) != len(self._sessions):
                self._sessions = alive
                _write_json(self.sessions_file, self._sessions)

    def sessions_delete_user(self, username: str, keep_token: str | None = None) -> None:
        with self._lock:
            alive = {t: s for t, s in self._sessions.items()
                     if s.get("username") != username or t == keep_token}
            if len(alive) != len(self._sessions):
                self._sessions = alive
                _write_json(self.sessions_file, self._sessions)

    # ---- transfer key（_id = 用户名小写，keys = 该账号的多把 key）----
    def transfer_list(self, username: str) -> list[dict]:
        key = (username or "").strip().lower()
        with self._lock:
            doc = self._transfer.get(key) or {}
            return [dict(k) for k in (doc.get("keys") or [])]

    def transfer_add(self, username: str, rec: dict) -> list[dict]:
        key = (username or "").strip().lower()
        with self._lock:
            doc = dict(self._transfer.get(key) or {})
            doc.setdefault("username", username)
            doc["keys"] = list(doc.get("keys") or []) + [dict(rec)]
            self._transfer[key] = doc
            _write_json(self.transfer_file, self._transfer)
            return [dict(k) for k in doc["keys"]]

    def transfer_remove(self, username: str, key_id: str) -> list[dict]:
        key = (username or "").strip().lower()
        with self._lock:
            doc = dict(self._transfer.get(key) or {})
            doc["keys"] = [k for k in (doc.get("keys") or [])
                           if k.get("id") != key_id]
            if doc.get("keys") or key in self._transfer:
                self._transfer[key] = doc
                _write_json(self.transfer_file, self._transfer)
            return [dict(k) for k in doc["keys"]]

    def transfer_find(self, token: str) -> dict | None:
        token = (token or "").strip()
        if not token:
            return None
        with self._lock:
            for key, doc in self._transfer.items():
                for rec in doc.get("keys") or []:
                    if rec.get("key") == token:
                        out = dict(rec)
                        out["username"] = doc.get("username") or key
                        return out
        return None

    def transfer_touch(self, token: str) -> None:
        token = (token or "").strip()
        if not token:
            return
        with self._lock:
            for doc in self._transfer.values():
                for rec in doc.get("keys") or []:
                    if rec.get("key") == token:
                        rec["uses"] = int(rec.get("uses") or 0) + 1
                        rec["last_used_at"] = _now_iso()
                        _write_json(self.transfer_file, self._transfer)
                        return

    def transfer_drop_user(self, username: str) -> None:
        key = (username or "").strip().lower()
        with self._lock:
            if self._transfer.pop(key, None) is not None:
                _write_json(self.transfer_file, self._transfer)


class AuthStore:
    """账号 + 会话（存储后端可插拔：MongoDB / 本地 JSON，见 `storage.py`）。"""

    def __init__(self, data_dir: Path | str, session_days: int = SESSION_DAYS, backend=None):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.session_days = session_days
        self.backend = backend if backend is not None else JsonAuthBackend(self.root)
        self._lock = threading.RLock()
        self._fails: dict[str, list] = {}      # 限流：key -> [失败次数, 最近失败时间]
        self.backend.sessions_prune(_now_iso())

    # ================= 账号 =================
    @property
    def initialized(self) -> bool:
        return self.backend.users_count() > 0

    def count(self) -> int:
        return self.backend.users_count()

    def public_user(self, username: str) -> dict | None:
        u = self._user(username)
        if not u:
            return None
        return {"username": u["username"], "created_at": u.get("created_at"),
                "updated_at": u.get("updated_at")}

    def _user(self, username: str) -> dict | None:
        return self.backend.user_get((username or "").strip().lower())

    def create_user(self, username: str, password: str) -> dict:
        username, password = validate_credentials(username, password)
        key = username.lower()
        if self._user(key):
            raise AuthError("该用户名已存在")
        rec = {"username": username, "password": hash_password(password),
               "created_at": _now_iso()}
        try:
            self.backend.user_put(key, rec)
        except RuntimeError as exc:          # 并发下唯一索引冲突
            raise AuthError("该用户名已存在") from exc
        return rec

    def authenticate(self, username: str, password: str) -> dict | None:
        u = self._user(username)
        if not u or not verify_password(password, u.get("password", "")):
            return None
        return u

    # ================= 账号管理（多账号/切换账号用） =================
    def list_users(self) -> list[dict]:
        """全部账号的公开信息，按创建时间排序（不返回口令哈希）。"""
        rows = []
        for rec in self.backend.users_all():
            rows.append({"username": rec.get("username"),
                         "created_at": rec.get("created_at"),
                         "updated_at": rec.get("updated_at")})
        rows.sort(key=lambda r: (r.get("created_at") or "", r.get("username") or ""))
        return rows

    # ================= 账号偏好（跟着账号走，如自定义高亮色） =================
    def get_prefs(self, username: str) -> dict:
        """读当前账号的偏好设置（不存在时返回空 dict）。"""
        u = self._user(username)
        return dict((u or {}).get("prefs") or {})

    def set_prefs(self, username: str, patch: dict) -> dict:
        """合并保存偏好设置，返回合并后的完整偏好。"""
        u = self._user(username)
        if not u:
            raise AuthError("账号不存在或已被注销")
        prefs = dict(u.get("prefs") or {})
        prefs.update(patch or {})
        rec = dict(u)
        rec["prefs"] = prefs
        self.backend.user_put(u["username"].lower(), rec)
        return prefs

    # ================= transfer key（给其它 Agent 读取本账号论文数据） =================
    # 设计：**每个账号一行**（`_id` = 用户名小写），`keys` 是该账号下的多把 key。
    # 明文 Key 存在库里（与 `users.prefs.llm.api_key` 同级别），因为所有者要能随时复制；
    # 校验只认「库里存在 + 账号还在」，删除即立刻失效。
    def list_transfer_keys(self, username: str) -> list[dict]:
        """某个账号名下的全部 transfer key（含完整 Key，只给所有者本人看）。"""
        return self.backend.transfer_list(username)

    def create_transfer_key(self, username: str, name: str = "") -> dict:
        """给账号新签一把 transfer key，返回刚建的那条 `{id, name, key, ...}`。"""
        keys = self.backend.transfer_list(username)
        if len(keys) >= TRANSFER_MAX:
            raise AuthError(f"每个账号最多 {TRANSFER_MAX} 把 transfer key，请先删掉不用的")
        rec = {
            "id": secrets.token_hex(6),
            "name": " ".join(str(name or "").split())[:TRANSFER_NAME_MAX],
            "key": TRANSFER_PREFIX + secrets.token_urlsafe(32),
            "created_at": _now_iso(),
            "last_used_at": None,
            "uses": 0,
        }
        self.backend.transfer_add(username, rec)
        return rec

    def delete_transfer_key(self, username: str, key_id: str) -> bool:
        """吊销一把 key（按 `id` 定位）；不存在返回 False。"""
        key_id = (key_id or "").strip()
        if not key_id:
            raise AuthError("缺少 transfer key 的 id")
        if not any((k.get("id") or "") == key_id
                   for k in self.backend.transfer_list(username)):
            return False
        self.backend.transfer_remove(username, key_id)
        return True

    def resolve_transfer_key(self, token: str) -> dict | None:
        """transfer key → `{username, id, name}`；无效/账号已删返回 None（并累计用量）。"""
        rec = self.backend.transfer_find(token)
        if not rec:
            return None
        u = self._user(rec.get("username") or "")
        if not u:                       # 账号已注销：key 立即视为无效
            return None
        try:
            self.backend.transfer_touch(token)
        except Exception:               # 统计失败不影响鉴权
            pass
        return {"username": u["username"], "id": rec.get("id") or "",
                "name": rec.get("name") or ""}

    def delete_user(self, username: str, by: str | None = None) -> None:
        """删除账号（连同它的登录会话）。不允许删自己，也不允许删掉最后一个账号。"""
        name = (username or "").strip()
        u = self._user(name)
        if not u:
            raise AuthError("账号不存在")
        if by and u["username"].lower() == (by or "").strip().lower():
            raise AuthError("不能删除当前登录的账号；要换账号请用「切换账号」")
        if self.backend.users_count() <= 1:
            raise AuthError("至少要保留一个账号")
        self.backend.user_delete(u["username"].lower())
        self.backend.sessions_delete_user(u["username"])
        self.backend.transfer_drop_user(u["username"])   # 该账号的 transfer key 一并吊销

    def close_account(self, username: str, password: str,
                      confirm: str | None = None) -> dict:
        """**注销（自助删除）自己的账号**：删完即登出、数据由其调用方清除。

        与 `delete_user()`（管理员删别人）的区别：
          - 必须再次校验当前口令，防止会话被他人接管后一键销号；
          - `confirm` 要求重输用户名（服务端也校验，不只靠前端弹窗）；
            接口层必须显式传入（缺省传空串），传 `None` 才会跳过校验，那是给受控调用方留的；
          - **允许注销最后一个账号** —— 清空后会回到“首次使用”初始化流程，
            不这样做的话“只剩一个账号”的用户就永远无法销号。
        """
        if not self._user(username):
            raise AuthError("账号不存在或已被注销")
        u = self.authenticate(username, password)
        if not u:
            raise AuthError("密码不正确，无法注销")
        if confirm is not None and (confirm or "").strip().lower() != u["username"].lower():
            raise AuthError(f"请输入用户名 {u['username']} 以确认注销")
        self.backend.user_delete(u["username"].lower())
        self.backend.sessions_delete_user(u["username"])   # 该账号所有会话立即失效
        self.backend.transfer_drop_user(u["username"])      # 该账号的 transfer key 一并吊销
        return u

    def change_password(self, username: str, old_password: str, new_password: str,
                        keep_token: str | None = None) -> None:
        u = self.authenticate(username, old_password)
        if not u:
            raise AuthError("原密码不正确")
        _, new_password = validate_credentials(u["username"], new_password)
        rec = dict(u)
        rec["password"] = hash_password(new_password)
        rec["updated_at"] = _now_iso()
        self.backend.user_put(u["username"].lower(), rec)
        # 改密后清掉该用户的其它会话（当前这次请求的会话保留，避免刚改完就被踢出）
        self.backend.sessions_delete_user(u["username"], keep_token)

    # ================= 登录限流 =================
    def locked_seconds(self, username: str, ip: str = "") -> int:
        """返回还需等待的秒数，0 表示未锁定。"""
        k = f"{(username or '').strip().lower()}|{ip}"
        with self._lock:
            n, ts = self._fails.get(k, [0, 0.0])
        left = _LOCK_SECONDS - (_now().timestamp() - ts) if n >= _MAX_FAILS else 0
        return max(0, int(left))

    def note_failure(self, username: str, ip: str = "") -> None:
        k = f"{(username or '').strip().lower()}|{ip}"
        with self._lock:
            n, _ = self._fails.get(k, [0, 0.0])
            self._fails[k] = [n + 1, _now().timestamp()]

    def clear_failures(self, username: str, ip: str = "") -> None:
        with self._lock:
            self._fails.pop(f"{(username or '').strip().lower()}|{ip}", None)

    # ================= 会话 =================
    def new_session(self, username: str, ip: str = "") -> str:
        token = secrets.token_urlsafe(32)
        self.backend.session_put(token, {
            "username": username,
            "ip": ip,
            "created_at": _now_iso(),
            "expires_at": (_now() + timedelta(days=self.session_days)).isoformat(timespec="seconds"),
        })
        return token

    def drop_session(self, token: str | None) -> None:
        if token:
            self.backend.session_delete(token)

    def user_for_token(self, token: str | None) -> dict | None:
        """token → 用户公开信息；过期/伪造一律返回 None。"""
        if not token:
            return None
        rec = self.backend.session_get(token)
        if not rec:
            return None
        if _expired(rec):
            self.backend.session_delete(token)
            return None
        return self.public_user(rec.get("username"))

    @property
    def cookie_max_age(self) -> int:
        return int(timedelta(days=self.session_days).total_seconds())
