"""模块七 · MongoDB 存储实现

把「论文阅读记录 / 版式文档 / 高亮 / 笔记 / 译文缓存」以及「账号 / 会话 / 设置」
落到 MongoDB；接口与 `records.Store`（本地 JSON 版）保持一致，所以上层代码不用分叉。

集合设计（`doc_id` 用论文 id，`block_id` 是前端文字块 id）：

| 集合 | `_id` | 说明 |
| --- | --- | --- |
| `records` | `doc_id` | 论文元数据/进度/状态（对应原来的 records.json）；`owner` 字段 = 归属账号，**论文按账号隔离** |
| `documents` | `doc_id` | 版式数据（原 doc.json），放在 `doc` 子字段里，查元数据时不带上它 |
| `highlights` | `doc_id\\|block_id` | 一条高亮一个文档，增删改都是单文档原子操作 |
| `notes` | 随机 id | 笔记，`(doc, block_id)` 唯一（同一句重复添加=更新，与原逻辑一致）；`by` = 写它的账号，共享论文里前端据此标识作者（回退到 JSON 实现时同一个字段落在 annotations.json） |
| `translations` | `doc_id\\|译文缓存键` | 译文缓存，一条一段；键 = 「目标语言 + 文本」的哈希（见 `translate.cache_key`），所以同一段文的不同语言译文并存、互不覆盖 |
| `images.files` / `images.chunks` | `"<doc_id>/<fname>"` | **插图本体**（GridFS 桶）：解析出来的 PNG 也进库，服务重启/换容器/换机器都不会再丢图 |
| `users` | 用户名(小写) | 账号，口令为 PBKDF2 加盐哈希 |
| `sessions` | 会话 token | 登录会话，带 TTL 索引自动过期 |
| `settings` | `"llm"` | LLM 服务配置（对应原来的 llm.json） |

插图与版式数据同在库里（`images` 是 GridFS 桶，`_id = "<doc_id>/<fname>"`），
所以 `mongodump` 一把就能把论文连图一起备走；磁盘上的 `data/docs/<id>/images/`
只在 JSON 回退模式下使用（读取时仍兼容老数据，见 `get_image()`）。

**版式文档的内存缓存带跨进程自动失效**：`documents` 每条记录带 `rev`
（写入版本号，见 `save_doc`），`read_doc` 每次先用**轻查询**比对 rev，
一致才用缓存 —— 重解析、迁移脚本、另一个 worker/实例写了库，本进程下次
读取会自动重读新数据，**不需要重启服务或手动清缓存**。
"""
from __future__ import annotations

import shutil
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import gridfs
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .db import mongo as default_mongo
from .records import reading_of


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _uid(n: int = 8) -> str:
    return uuid.uuid4().hex[:n]


def _key(doc_id: str, sub: str) -> str:
    return f"{doc_id}|{sub}"


def _parse_dt(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except Exception:
        return None


class MongoStore:
    """MongoDB 版论文存储（接口对齐 `records.Store`）。"""

    backend_name = "mongo"
    images_in_db = True          # 插图存 GridFS，不再依赖磁盘目录
    IMG_BUCKET = "images"        # 集合：images.files / images.chunks
    _CACHE_MAX = 6            # 版式文档较大，缓存最近几篇就够

    def __init__(self, data_dir: Path | str, mongo=None):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.m = mongo or default_mongo
        db = self.m.db()
        self.records = db["records"]
        self.documents = db["documents"]
        self.highlights = db["highlights"]
        self.notes = db["notes"]
        self.translations = db["translations"]
        self.seltrans = db["seltrans"]
        # 插图：GridFS 桶（images.files + images.chunks），_id = "<doc_id>/<fname>"
        self.fs = gridfs.GridFS(db, self.IMG_BUCKET)
        self.img_files = db[self.IMG_BUCKET + ".files"]
        self.img_chunks = db[self.IMG_BUCKET + ".chunks"]
        self._lock = threading.RLock()
        # {doc_id: (rev, doc)} —— rev 比对实现跨进程自动失效（见 save_doc/read_doc）
        self._doc_cache: OrderedDict[str, tuple] = OrderedDict()
        self.ensure_indexes()

    def ensure_indexes(self) -> None:
        self.records.create_index([("owner", 1), ("last_read_at", -1)])
        self.records.create_index([("owner", 1), ("created_at", -1)])
        self.highlights.create_index([("doc", 1)])
        self.notes.create_index([("doc", 1)])
        self.notes.create_index([("doc", 1), ("block_id", 1)], unique=True)
        self.translations.create_index([("doc", 1)])
        self.seltrans.create_index([("doc", 1)])
        # GridFS 官方建议的分片索引（读图靠它），以及“按论文批量删图”用的索引
        self.img_chunks.create_index([("files_id", 1), ("n", 1)], unique=True)
        self.img_files.create_index([("metadata.doc_id", 1)])

    # ---------------- 路径（JSON 回退时代的图片目录，今日只用于兼容老数据） ----------------
    def doc_dir(self, doc_id: str) -> Path:
        return self.root / "docs" / doc_id

    def images_dir(self, doc_id: str) -> Path:
        return self.doc_dir(doc_id) / "images"

    # ---------------- 插图（GridFS） ----------------
    # _id 用 "<doc_id>/<fname>"：既能按论文前缀定位，又能精确取单张，
    # 而且重传同一张图不会积下旧分片（put 前先清一次同 id 的 files/chunks）。
    @staticmethod
    def img_key(doc_id: str, fname: str) -> str:
        return f"{doc_id}/{fname}"

    def put_image(self, doc_id: str, fname: str, data: bytes) -> int:
        key = self.img_key(doc_id, fname)
        self.img_files.delete_one({"_id": key})
        self.img_chunks.delete_many({"files_id": key})     # 清可能残留的孤儿分片
        self.fs.put(data, _id=key, filename=fname, metadata={"doc_id": doc_id})
        return len(data)

    def put_images(self, doc_id: str, src_dir: Path | str) -> int:
        """把 `src_dir` 里的图片批量存进 GridFS（返回张数）。"""
        src = Path(src_dir)
        if not src.exists():
            return 0
        n = 0
        for f in sorted(src.iterdir()):
            if f.is_file():
                self.put_image(doc_id, f.name, f.read_bytes())
                n += 1
        return n

    def get_image(self, doc_id: str, fname: str) -> bytes | None:
        """取插图字节；库里没有再回退磁盘（兼容还没迁移的老图片，见 images_dir）。"""
        try:
            return self.fs.get(self.img_key(doc_id, fname)).read()
        except Exception:
            pass
        f = self.images_dir(doc_id) / fname
        return f.read_bytes() if f.exists() else None

    def list_images(self, doc_id: str) -> list[str]:
        rows = self.img_files.find({"metadata.doc_id": doc_id}, {"filename": 1})
        names = {r.get("filename") for r in rows}
        d = self.images_dir(doc_id)
        if d.exists():
            names.update(p.name for p in d.iterdir() if p.is_file())
        return sorted(n for n in names if n)

    def delete_images(self, doc_id: str) -> int:
        keys = [r["_id"] for r in self.img_files.find({"metadata.doc_id": doc_id}, {"_id": 1})]
        if keys:
            self.img_files.delete_many({"_id": {"$in": keys}})
            self.img_chunks.delete_many({"files_id": {"$in": keys}})
        d = self.doc_dir(doc_id)
        if d.exists():                                   # 兼容：老图片还在磁盘的也一起清
            shutil.rmtree(d, ignore_errors=True)
        return len(keys)

    # ---------------- 阅读记录 ----------------
    def ensure_record(self, meta: dict) -> None:
        self.records.replace_one({"_id": meta["id"]}, dict(meta), upsert=True)

    def list_records(self) -> list[dict]:
        rows = list(self.records.find({}, {"_id": 0}))
        rows.sort(key=lambda r: r.get("last_read_at") or r.get("created_at") or "", reverse=True)
        return rows

    def list_by_owner(self, owner: str | None = None) -> list[dict]:
        """某个账号的论文列表（`owner=None` = 全部，管理/迁移用）。"""
        q: dict = {} if owner is None else {"owner": owner}
        rows = list(self.records.find(q, {"_id": 0}))
        rows.sort(key=lambda r: r.get("last_read_at") or r.get("created_at") or "", reverse=True)
        return rows

    def list_for_user(self, username: str) -> list[dict]:
        """某个账号**能看到的**论文：自己上传的 + 别人共享给他的。"""
        q = {"$or": [{"owner": username}, {"shared.user": username}]}
        rows = list(self.records.find(q, {"_id": 0}))
        rows.sort(key=lambda r: (reading_of(r, username).get("last_read_at")
                                 or r.get("created_at") or ""), reverse=True)
        return rows

    def adopt_orphans(self, owner: str) -> int:
        """把没有 owner 的历史论文归给指定账号（升级到“按账号隔离”后跑一次）。"""
        res = self.records.update_many(
            {"$or": [{"owner": {"$exists": False}}, {"owner": None}, {"owner": ""}]},
            {"$set": {"owner": owner}})
        return res.modified_count

    def purge_owner(self, owner: str) -> int:
        """删掉某账号的全部论文（含版式数据/标注/笔记/译文/图片），返回篇数。"""
        ids = [d["_id"] for d in self.records.find({"owner": owner}, {"_id": 1})]
        for doc_id in ids:
            self.delete(doc_id)
        return len(ids)

    def get_record(self, doc_id: str) -> dict | None:
        return self.records.find_one({"_id": doc_id}, {"_id": 0})

    def update_record(self, doc_id: str, **fields) -> dict | None:
        if not fields:
            return self.get_record(doc_id)
        return self.records.find_one_and_update(
            {"_id": doc_id}, {"$set": fields},
            return_document=ReturnDocument.AFTER, projection={"_id": 0})

    def delete(self, doc_id: str) -> None:
        self.records.delete_one({"_id": doc_id})
        self.documents.delete_one({"_id": doc_id})
        self.highlights.delete_many({"doc": doc_id})
        self.notes.delete_many({"doc": doc_id})
        self.translations.delete_many({"doc": doc_id})
        self.seltrans.delete_many({"doc": doc_id})
        with self._lock:
            self._doc_cache.pop(doc_id, None)
        self.delete_images(doc_id)          # 插图在 GridFS，别漏删

    # ---------------- 版式文档 ----------------
    # 缓存条目 = (rev, doc)：rev 是库里的写入版本号（save_doc 每次写入新值），
    # read_doc 先做一次轻查询比对它 —— 任何进程写了库（重解析、脚本工具、
    # 另一个 worker/实例），这里都会**自动**发现并重读。
    def save_doc(self, doc_id: str, doc: dict) -> None:
        rev = time.time_ns()               # 每次写入一个新版本号
        self.documents.replace_one({"_id": doc_id},
                                   {"doc": doc, "rev": rev}, upsert=True)
        self._cache_put(doc_id, doc, rev)

    def _cache_put(self, doc_id: str, doc: dict, rev: int | None) -> None:
        with self._lock:
            self._doc_cache[doc_id] = (rev, doc)
            self._doc_cache.move_to_end(doc_id)
            while len(self._doc_cache) > self._CACHE_MAX:
                self._doc_cache.popitem(last=False)

    def read_doc(self, doc_id: str) -> dict:
        """读版式文档（带跨进程自动失效的内存缓存）。

        文档在库里带 `rev`（写入版本号）：这里每次先做一次**轻查询只取 rev**，
        与缓存上的 rev 不一致（或没有缓存）才重读全量 —— 重解析、脚本工具、
        另一个 worker/实例写完库后，本进程的下一个请求就会自动拿到新数据，
        **不需要重启服务或手动清缓存**。历史文档没有 rev（读到 None），
        经新代码 save_doc 写一次就会带上（写路径已全部收敛到 save_doc）。
        """
        row = self.documents.find_one({"_id": doc_id}, {"rev": 1})
        if not row:
            raise FileNotFoundError(doc_id)
        rev = row.get("rev")
        with self._lock:
            ent = self._doc_cache.get(doc_id)
            if ent and ent[0] == rev:
                self._doc_cache.move_to_end(doc_id)
                return ent[1]
        row = self.documents.find_one({"_id": doc_id}, {"doc": 1, "rev": 1})
        if not row or not row.get("doc"):
            raise FileNotFoundError(doc_id)
        doc = row["doc"]
        self._cache_put(doc_id, doc, row.get("rev"))
        return doc

    def clear_cache(self, doc_id: str | None = None) -> None:
        with self._lock:
            if doc_id:
                self._doc_cache.pop(doc_id, None)
            else:
                self._doc_cache.clear()

    # ---------------- 标注与笔记 ----------------
    def get_annotations(self, doc_id: str) -> dict:
        hl = {d["block_id"]: {"color": d.get("color") or "yellow", "at": d.get("at")}
              for d in self.highlights.find({"doc": doc_id}, {"_id": 0})}
        # 笔记 id 统一转成字符串：早期/手工插入的文档 _id 可能是 ObjectId，
        # 直接返回会导致 JSON 序列化 500
        note_docs = list(self.notes.find({"doc": doc_id}).sort("created_at", 1))
        notes = [{"id": str(d.get("_id")), "block_id": d.get("block_id"),
                  "text": d.get("text") or "", "by": d.get("by") or "",
                  "created_at": d.get("created_at"), "updated_at": d.get("updated_at")}
                 for d in note_docs if d.get("kind") != "free" and d.get("block_id")]
        free_notes = [{"id": str(d.get("_id")), "title": d.get("title") or "",
                       "text": d.get("text") or "", "by": d.get("by") or "",
                       "created_at": d.get("created_at"), "updated_at": d.get("updated_at")}
                      for d in note_docs if d.get("kind") == "free"]
        return {"highlights": hl, "notes": notes, "free_notes": free_notes}

    def apply_anno(self, doc_id: str, action: str, block_id: str | None = None,
                   color: str | None = None, text: str | None = None,
                   note_id: str | None = None, by: str | None = None,
                   title: str | None = None) -> dict:
        """动作: add_highlight / remove_highlight / add_note / remove_note /
        add_free_note / update_free_note / remove_free_note。

        每个动作都是**单文档**的 upsert/delete（不再整份文件读改写），
        所以并发下不会互相覆盖。`by` = 写这条笔记的账号（共享论文标识作者用）。
        """
        if action == "add_highlight" and block_id:
            self.highlights.replace_one(
                {"_id": _key(doc_id, block_id)},
                {"_id": _key(doc_id, block_id), "doc": doc_id, "block_id": block_id,
                 "color": color or "yellow", "at": _now()}, upsert=True)
        elif action == "remove_highlight" and block_id:
            self.highlights.delete_one({"_id": _key(doc_id, block_id)})
        elif action == "add_note" and block_id and text is not None:
            fields = {"text": text, "updated_at": _now()}
            if by:                     # 同一句只有一条笔记：改写后归最后写的人
                fields["by"] = by
            # 显式生成字符串 _id（让笔记 id 与本地 JSON 版一致、直接可返回给前端）
            try:
                self.notes.update_one(
                    {"doc": doc_id, "block_id": block_id},
                    {"$set": fields,
                     "$setOnInsert": {"_id": _uid(), "created_at": _now()}},
                    upsert=True)
            except DuplicateKeyError:
                # 并发插入同一句笔记：退化成更新
                self.notes.update_one({"doc": doc_id, "block_id": block_id},
                                      {"$set": fields})
        elif action == "remove_note" and note_id:
            self.notes.delete_one({"_id": note_id, "doc": doc_id, "kind": {"$ne": "free"}})
        elif action == "add_free_note" and text is not None:
            now = _now()
            note = {"_id": _uid(), "doc": doc_id, "kind": "free",
                    "title": (title or "").strip(), "text": text,
                    "created_at": now, "updated_at": now}
            if by:
                note["by"] = by
            self.notes.insert_one(note)
        elif action == "update_free_note" and note_id and text is not None:
            fields = {"title": (title or "").strip(), "text": text, "updated_at": _now()}
            if by:
                fields["by"] = by
            self.notes.update_one({"_id": note_id, "doc": doc_id, "kind": "free"},
                                  {"$set": fields})
        elif action == "remove_free_note" and note_id:
            self.notes.delete_one({"_id": note_id, "doc": doc_id, "kind": "free"})

        anno = self.get_annotations(doc_id)
        self.update_record(doc_id, highlights=len(anno["highlights"]),
                           notes=len(anno["notes"]) + len(anno["free_notes"]))
        return anno

    # ---------------- 译文缓存 ----------------
    def get_translations(self, doc_id: str) -> dict:
        return {d["h"]: d["zh"] for d in self.translations.find({"doc": doc_id}, {"h": 1, "zh": 1})}

    def set_translations(self, doc_id: str, items: dict[str, str]) -> None:
        if not items:
            return
        from pymongo import UpdateOne
        ops = [UpdateOne({"_id": _key(doc_id, h)},
                         {"$set": {"doc": doc_id, "h": h, "zh": zh, "at": _now()}},
                         upsert=True)
               for h, zh in items.items()]
        self.translations.bulk_write(ops, ordered=False)

    # ---------------- 所选句译文卡片（跨刷新/重新登录保留） ----------------
    def get_seltrans(self, doc_id: str) -> list[dict]:
        return [{"key": d.get("key"), "item_id": d.get("item_id"),
                 "si_from": d.get("si_from"), "text": d.get("text"),
                 "lang": d.get("lang"),
                 "created_at": d.get("created_at")}
                for d in self.seltrans.find({"doc": doc_id}).sort("created_at", 1)]

    def add_seltrans(self, doc_id: str, card: dict) -> list[dict]:
        key = card["key"]
        self.seltrans.replace_one(
            {"_id": _key(doc_id, key)},
            {"_id": _key(doc_id, key), "doc": doc_id, "key": key,
             "item_id": card.get("item_id"), "si_from": card.get("si_from") or 0,
             "text": card.get("text") or "",
             "lang": card.get("lang"),
             "created_at": card.get("created_at") or _now()},
            upsert=True)
        return self.get_seltrans(doc_id)

    def del_seltrans(self, doc_id: str, key: str) -> list[dict]:
        self.seltrans.delete_one({"_id": _key(doc_id, key)})
        return self.get_seltrans(doc_id)

    def clear_seltrans(self, doc_id: str) -> list[dict]:
        self.seltrans.delete_many({"doc": doc_id})
        return []

    # ---------------- 迁移用（tools/migrate_to_mongo.py） ----------------
    def import_annotations(self, doc_id: str, anno: dict) -> None:
        """整体导入本地 JSON 的 annotations 结构（注释 id 保留，前端链接不会失效）。"""
        self.highlights.delete_many({"doc": doc_id})
        self.notes.delete_many({"doc": doc_id})
        for block_id, hl in (anno.get("highlights") or {}).items():
            hl = hl or {}
            self.highlights.replace_one(
                {"_id": _key(doc_id, block_id)},
                {"_id": _key(doc_id, block_id), "doc": doc_id, "block_id": block_id,
                 "color": hl.get("color") or "yellow", "at": hl.get("at")},
                upsert=True)
        for n in (anno.get("notes") or []):
            if not n.get("block_id"):
                continue
            note_id = n.get("id") or _uid()
            self.notes.replace_one(
                {"_id": note_id},
                {"_id": note_id, "doc": doc_id, "block_id": n["block_id"],
                 "text": n.get("text") or "", "by": n.get("by") or "",
                 "created_at": n.get("created_at"),
                 "updated_at": n.get("updated_at")},
                upsert=True)
        for n in (anno.get("free_notes") or []):
            note_id = n.get("id") or _uid()
            self.notes.replace_one(
                {"_id": note_id},
                {"_id": note_id, "doc": doc_id, "kind": "free",
                 "title": n.get("title") or "", "text": n.get("text") or "",
                 "by": n.get("by") or "", "created_at": n.get("created_at"),
                 "updated_at": n.get("updated_at")},
                upsert=True)
        self.update_record(doc_id, highlights=len(anno.get("highlights") or {}),
                           notes=len(anno.get("notes") or []) + len(anno.get("free_notes") or []))


class MongoAuthBackend:
    """账号与会话的 MongoDB 后端（供 `auth.AuthStore` 使用）。"""

    name = "mongo"

    def __init__(self, mongo=None):
        self.m = mongo or default_mongo
        db = self.m.db()
        self.users = db["users"]
        self.sessions = db["sessions"]
        self.users.create_index([("username", 1)])
        # TTL：会话按 expires_at 自动清理（代码里也会显式校验过期）
        try:
            self.sessions.create_index("expire_at", expireAfterSeconds=0)
        except Exception:
            pass

    # ---------------- 账号 ----------------
    def users_count(self) -> int:
        return self.users.count_documents({})

    def users_all(self) -> list[dict]:
        return list(self.users.find({}, {"_id": 0}))

    def user_get(self, key: str) -> dict | None:
        return self.users.find_one({"_id": key}, {"_id": 0})

    def user_put(self, key: str, rec: dict) -> None:
        try:
            self.users.replace_one({"_id": key}, dict(rec), upsert=True)
        except DuplicateKeyError as exc:
            raise RuntimeError(f"用户名冲突：{key}") from exc

    def user_delete(self, key: str) -> bool:
        return self.users.delete_one({"_id": key}).deleted_count > 0

    # ---------------- 会话 ----------------
    def session_get(self, token: str) -> dict | None:
        return self.sessions.find_one({"_id": token}, {"_id": 0})

    def session_put(self, token: str, rec: dict) -> None:
        doc = dict(rec)
        exp = _parse_dt(rec.get("expires_at"))
        if exp:
            doc["expire_at"] = exp        # TTL 索引用
        self.sessions.replace_one({"_id": token}, doc, upsert=True)

    def session_delete(self, token: str) -> None:
        self.sessions.delete_one({"_id": token})

    def sessions_prune(self, now_iso: str) -> None:
        now = _parse_dt(now_iso) or datetime.now()
        self.sessions.delete_many({"expire_at": {"$lte": now}})

    def sessions_delete_user(self, username: str, keep_token: str | None = None) -> None:
        """改密后清掉该用户的其它会话（`keep_token` 是当前这次请求的会话，保留）。"""
        q: dict = {"username": username}
        if keep_token:
            q["_id"] = {"$ne": keep_token}
        self.sessions.delete_many(q)


class MongoSettingsBackend:
    """**旧版**的“全局一份” LLM 配置（`settings` 集合 `_id="llm"`）。

    现在 LLM 配置按账号存在 `users.prefs.llm`，这个后端只用于升级时读一次旧值
    （见 `settings.SettingsStore.adopt_legacy()`），不再写入。
    """

    name = "mongo"
    _ID = "llm"

    def __init__(self, mongo=None):
        self.m = mongo or default_mongo
        self.col = self.m.db()["settings"]

    def load(self) -> dict:
        doc = self.col.find_one({"_id": self._ID}) or {}
        doc.pop("_id", None)
        return doc

    def save(self, data: dict) -> None:
        self.col.replace_one({"_id": self._ID}, dict(data), upsert=True)

    def exists(self) -> bool:
        return self.col.count_documents({"_id": self._ID}, limit=1) > 0
