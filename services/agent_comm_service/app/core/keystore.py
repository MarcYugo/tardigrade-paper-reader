"""transfer_key 的持久化存储（MongoDB / 进程内）。

安全模型与 LLM API key 一致：
  * 明文只在签发响应里返回一次，服务端此后不保留；
  * 库里只存不可逆摘要 HMAC-SHA256(pepper, secret)，所以**没有"第二个解密密钥"**
    —— 没有密文，也就无从解密。拖库只能拿到摘要，凭摘要无法还原出可用 key；
  * 吊销 = 置 revoked_at（软删，保留审计）或 purge 硬删；
  * 过期 = 校验时兜底判断 + exp_at 上的 TTL 索引自动清理。
"""
from __future__ import annotations

import abc
import asyncio
from datetime import datetime, timezone
from typing import Any

from env_load import KEY_COLLECTION, MONGO_DB, MONGO_URL

try:  # pymongo>=4.9 才有原生 async 客户端
    from pymongo import ASCENDING, AsyncMongoClient
except Exception:  # pragma: no cover - 未安装时仅 Mongo 后端不可用
    ASCENDING = None  # type: ignore[assignment]
    AsyncMongoClient = None  # type: ignore[assignment]


class KeyStore(abc.ABC):
    """最小接口：签发写入 / 按 id 取 / 列出 / 吊销 / 记录使用时间。"""

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    @abc.abstractmethod
    async def add(self, doc: dict[str, Any]) -> None: ...

    @abc.abstractmethod
    async def get(self, key_id: str) -> dict[str, Any] | None: ...

    @abc.abstractmethod
    async def list_owner(self, owner: str, limit: int = 200) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    async def revoke(self, key_id: str, owner: str, *, purge: bool = False) -> bool: ...

    @abc.abstractmethod
    async def touch(self, key_id: str, ts: datetime) -> None: ...


class MemoryKeyStore(KeyStore):
    """仅用于测试 / 未配置 MONGO_URL 的本地调试；进程重启即清空。"""

    def __init__(self) -> None:
        self._docs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def add(self, doc: dict[str, Any]) -> None:
        async with self._lock:
            if doc["_id"] in self._docs:
                raise KeyError(doc["_id"])
            self._docs[doc["_id"]] = dict(doc)

    async def get(self, key_id: str) -> dict[str, Any] | None:
        async with self._lock:
            doc = self._docs.get(key_id)
            return dict(doc) if doc else None

    async def list_owner(self, owner: str, limit: int = 200) -> list[dict[str, Any]]:
        async with self._lock:
            rows = [dict(d) for d in self._docs.values() if d.get("owner") == owner]
        rows.sort(key=lambda d: d.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return rows[:limit]

    async def revoke(self, key_id: str, owner: str, *, purge: bool = False) -> bool:
        async with self._lock:
            doc = self._docs.get(key_id)
            if doc is None or doc.get("owner") != owner:
                return False
            if purge:
                self._docs.pop(key_id, None)
            else:
                doc["revoked_at"] = doc.get("revoked_at") or datetime.now(timezone.utc)
            return True

    async def touch(self, key_id: str, ts: datetime) -> None:
        async with self._lock:
            if key_id in self._docs:
                self._docs[key_id]["last_used_at"] = ts


class MongoKeyStore(KeyStore):
    """生产后端。集合文档见 guard.issue_transfer_key 里构造的 doc。"""

    def __init__(self, url: str, db: str, collection: str) -> None:
        self._url = url
        self._db_name = db
        self._col_name = collection
        self._client: Any = None
        self._col: Any = None

    async def connect(self) -> None:
        if AsyncMongoClient is None:
            raise RuntimeError("启用了 MongoDB 密钥存储，但未安装 pymongo>=4.9")
        self._client = AsyncMongoClient(self._url, tz_aware=True, serverSelectionTimeoutMS=5_000)
        self._col = self._client[self._db_name][self._col_name]
        await self._col.create_index([("owner", ASCENDING)], name="owner_idx")
        # expireAfterSeconds=0 表示"到 exp_at 即删"；Mongo 后台每分钟扫一次，
        # 所以过期判定必须由业务代码兜底，不能依赖 TTL。
        await self._col.create_index(
            [("exp_at", ASCENDING)], expireAfterSeconds=0, name="ttl_exp_at"
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._col = None

    async def add(self, doc: dict[str, Any]) -> None:
        await self._col.insert_one(doc)

    async def get(self, key_id: str) -> dict[str, Any] | None:
        return await self._col.find_one({"_id": key_id})

    async def list_owner(self, owner: str, limit: int = 200) -> list[dict[str, Any]]:
        cursor = self._col.find({"owner": owner}).sort("created_at", -1).limit(limit)
        return [doc async for doc in cursor]

    async def revoke(self, key_id: str, owner: str, *, purge: bool = False) -> bool:
        if purge:
            result = await self._col.delete_one({"_id": key_id, "owner": owner})
            return bool(result.deleted_count)
        result = await self._col.update_one(
            {"_id": key_id, "owner": owner, "revoked_at": None},
            {"$set": {"revoked_at": datetime.now(timezone.utc)}},
        )
        if result.matched_count:
            return True
        # 已经吊销过也视为成功（幂等），但别人的 key 一律当作不存在
        return bool(await self._col.find_one({"_id": key_id, "owner": owner}, {"_id": 1}))

    async def touch(self, key_id: str, ts: datetime) -> None:
        await self._col.update_one({"_id": key_id}, {"$set": {"last_used_at": ts}})


_store: KeyStore | None = None


def build_store() -> KeyStore:
    """默认连 MongoDB；MONGO_URL=memory:// 时退回进程内存储（仅本地调试/测试）。"""
    if MONGO_URL.strip().lower().startswith("memory"):
        print("[warn] MONGO_URL=memory：transfer_key 存进程内存，重启即失效", flush=True)
        return MemoryKeyStore()
    return MongoKeyStore(MONGO_URL, MONGO_DB, KEY_COLLECTION)


def set_store(store: KeyStore | None) -> None:
    global _store
    _store = store


def get_store() -> KeyStore:
    global _store
    if _store is None:
        _store = build_store()
    return _store
