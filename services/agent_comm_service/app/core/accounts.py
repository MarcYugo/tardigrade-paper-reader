"""账号级 transfer key（`ptk_…`）校验 —— 网关是**唯一**的准入决策点。

为什么放在本服务里：`ptk_` 是 tardigrade 应用签发给**账号**的钥匙（每个账号一行，
`keys[]` 里是可以复制的明文 key），存在同一个 MongoDB 的 `paper.transfer_keys`。
本服务与它共用这个库（业务账号 `paper` 本来就有 readWrite），所以**直读即可**：
不回调应用、不需要额外授权，吊销/删除也立刻生效（每次都查库，不做缓存）。

文档结构（由应用写入，本服务只读 + 旁路累加用量）：

    transfer_keys: {_id: 用户名(小写), username: 用户名,
                    keys: [{id, name, key, created_at, last_used_at, uses}]}
    users:         {_id: 用户名(小写), username, password, prefs, ...}

另外这里还托管「网关盖章密钥」的持久化：盖章（见 `stamp.py`）需要一个**跨重启、
跨副本稳定**的密钥。它不取自容器启动环境变量（那是之前的问题来源），而是由本服务
在 Mongo 的 `agent_gateway_meta` 里 get-or-create 一次，之后一直复用。
"""
from __future__ import annotations

import abc
from datetime import datetime, timezone
from typing import Any, Callable

from env_load import (
    ACCOUNT_KEY_COLLECTION,
    GATEWAY_META_COLLECTION,
    MONGO_DB,
    MONGO_URL,
    USER_COLLECTION,
)

try:  # pymongo>=4.9 才有原生 async 客户端
    from pymongo import AsyncMongoClient, ReturnDocument
except Exception:  # pragma: no cover - 未安装时仅 Mongo 后端不可用
    AsyncMongoClient = None  # type: ignore[assignment]
    ReturnDocument = None  # type: ignore[assignment]


class AccountKeyStore(abc.ABC):
    """最小接口：按 key 反查账号 / 读写网关元数据（盖章密钥）。"""

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    @abc.abstractmethod
    async def resolve(self, key: str) -> dict[str, Any] | None:
        """`ptk_…` → `{username, key_id, name}`；无效 / 账号已注销返回 None。"""

    @abc.abstractmethod
    async def get_meta(self, doc_id: str, factory: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """读取（不存在则按 `factory()` 创建）一条网关元数据。"""


class MemoryAccountKeyStore(AccountKeyStore):
    """仅用于测试 / 未配置 MONGO_URL 的本地调试；进程重启即清空。

    `add(username, key, key_id=...)` / `remove(key)` 用来在测试里模拟应用的
    “签发 / 删除”动作（与真实库里 `transfer_keys` 的语义一致：删掉即失效）。
    """

    def __init__(self) -> None:
        self._keys: dict[str, dict[str, Any]] = {}
        self._meta: dict[str, dict[str, Any]] = {}

    # ---- 测试辅助 ----
    def add(self, username: str, key: str, *, key_id: str = "", name: str = "") -> None:
        self._keys[key] = {"username": username, "key_id": key_id, "name": name}

    def remove(self, key: str) -> None:
        self._keys.pop(key, None)

    # ---- AccountKeyStore ----
    async def resolve(self, key: str) -> dict[str, Any] | None:
        rec = self._keys.get((key or "").strip())
        return dict(rec) if rec else None

    async def get_meta(self, doc_id: str, factory: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        if doc_id not in self._meta:
            self._meta[doc_id] = dict(factory())
        return dict(self._meta[doc_id])


class MongoAccountKeyStore(AccountKeyStore):
    """生产后端：直读应用那套 `transfer_keys` / `users` 集合。"""

    def __init__(self, url: str, db: str, key_col: str, user_col: str, meta_col: str) -> None:
        self._url = url
        self._db_name = db
        self._key_col = key_col
        self._user_col = user_col
        self._meta_col = meta_col
        self._client: Any = None
        self._keys: Any = None
        self._users: Any = None
        self._meta: Any = None

    async def connect(self) -> None:
        if AsyncMongoClient is None:
            raise RuntimeError("启用了账号 key 校验，但未安装 pymongo>=4.9")
        self._client = AsyncMongoClient(self._url, tz_aware=True, serverSelectionTimeoutMS=5_000)
        db = self._client[self._db_name]
        self._keys = db[self._key_col]
        self._users = db[self._user_col]
        self._meta = db[self._meta_col]
        # 应用侧已建过 keys.key 索引；这里显式探一次，连不上库就尽早失败
        await self._keys.find_one({}, {"_id": 1})

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._keys = self._users = self._meta = None

    async def resolve(self, key: str) -> dict[str, Any] | None:
        token = (key or "").strip()
        if not token:
            return None
        doc = await self._keys.find_one({f"keys.key": token}, {"username": 1, "keys.$": 1})
        if not doc:
            return None
        one = (doc.get("keys") or [{}])[0]
        username = str(doc.get("username") or doc.get("_id") or "").strip()
        if not username:
            return None
        # 账号注销后 key 必须立刻失效（应用侧 `resolve_transfer_key` 也是这么判的）
        alive = await self._users.find_one({"_id": username.lower()}, {"_id": 1})
        if not alive:
            return None
        # 用量统计是旁路信息：写失败不影响放行（与 `AuthStore.resolve_transfer_key` 一致）
        try:
            await self._keys.update_one(
                {f"keys.key": token},
                {"$inc": {"keys.$.uses": 1},
                 "$set": {"keys.$.last_used_at": datetime.now(timezone.utc)}},
            )
        except Exception:
            pass
        return {"username": username,
                "key_id": str(one.get("id") or ""),
                "name": str(one.get("name") or "")}

    async def get_meta(self, doc_id: str, factory: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        doc = await self._meta.find_one_and_update(
            {"_id": doc_id},
            {"$setOnInsert": dict(factory())},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return dict(doc or {})


_store: AccountKeyStore | None = None


def build_store() -> AccountKeyStore:
    """默认连 MongoDB；MONGO_URL=memory:// 时退回进程内存储（仅本地调试/测试）。"""
    if MONGO_URL.strip().lower().startswith("memory"):
        print("[warn] MONGO_URL=memory：账号 transfer key 存进程内存，重启即失效", flush=True)
        return MemoryAccountKeyStore()
    return MongoAccountKeyStore(MONGO_URL, MONGO_DB,
                               ACCOUNT_KEY_COLLECTION, USER_COLLECTION,
                               GATEWAY_META_COLLECTION)


def set_store(store: AccountKeyStore | None) -> None:
    global _store
    _store = store


def get_store() -> AccountKeyStore:
    global _store
    if _store is None:
        _store = build_store()
    return _store
