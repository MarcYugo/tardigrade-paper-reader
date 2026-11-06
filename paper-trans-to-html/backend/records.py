"""模块三 · 论文阅读记录

管理每篇论文的元数据/阅读进度/标注/笔记/译文缓存与插图。
全部以 JSON 落盘在 data/ 目录，轻量且易于查看（**回退模式**的布局）：
  data/records.json            论文阅读记录列表(元数据，带 owner 归属账号)
  data/docs/<id>/doc.json      版式文档数据
  data/docs/<id>/images/       抽取图片
  data/docs/<id>/annotations.json  标注(高亮+笔记)
  data/docs/<id>/translations.json 译文缓存(「目标语言+文本」哈希 -> 译文)
  data/docs/<id>/seltrans.json     “所选句译文”卡片(带目标语言 lang)

默认后端是 MongoDB（`mongo_store.py`）：那边插图与其它数据一样存库（GridFS），
只有回退到这个 JSON 实现时插图才落在 `images/` 目录里。

论文按 **owner（账号）隔离**：`list_by_owner()` 只返回该账号的论文；
标注/笔记/译文/图片都挂在 doc_id 下，所以只要卡住“论文能不能看”就自然隔离了。

记录里还有两个与多用户相关的字段：
  `shared`    共享名单 `[{user, perm, by, at}]`（perm = `read` 只读 / `write` 可写）
  `reading`   **非所有者**的阅读进度 `{用户名: {progress, last_page, status, last_read_at}}`
             （所有者的进度仍在记录顶层的同名字段，保持老数据兼容）
权限判定与进度读写用本模块的 `perm_of()` / `reading_of()` / `reading_patch()`，
两个存储后端共用同一套规则。
笔记里带 `by`（写它的账号）；共享论文里前端据此标识「谁写的」，规则见
`anno_with_authors()` —— **没共享给别人时不下发 `by`**。
"""
from __future__ import annotations

import json
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _uid(n: int = 8) -> str:
    return uuid.uuid4().hex[:n]


SHARE_PERMS = ("read", "write")        # 共享权限：只读 / 可写

# 阅读状态跟着**人**走：论文所有者存在记录顶层（兼容老数据），
# 被共享的人存在 `reading.<用户名>` 里，互不覆盖。
# `last_pos` 是**精确的阅读位置**（`{pi, ratio}` = 第几页 + 页内比例，
# `{pi:-1, fromBottom}` = 已经滚到文末 AI 面板），与 `last_page`（只记最远那一页）
# 互补：老数据没有它时前端退回按页码定位。
READING_KEYS = ("last_page", "progress", "status", "last_read_at", "last_pos")


def owner_of(rec: dict) -> str:
    """论文归属账号；历史数据没有 owner 时返回空串。"""
    return ((rec or {}).get("owner") or "").strip()


def shared_entries(rec: dict) -> list[dict]:
    """论文的共享名单（`[{user, perm, by, at}]`），顺手把脏数据滤掉。"""
    out: list[dict] = []
    for s in (rec or {}).get("shared") or []:
        if not isinstance(s, dict):
            continue
        name = str(s.get("user") or "").strip()
        if not name:
            continue
        out.append({"user": name,
                    "perm": "write" if str(s.get("perm") or "").lower() == "write" else "read",
                    "by": str(s.get("by") or "").strip(),
                    "at": s.get("at") or ""})
    return out


def perm_of(rec: dict, username: str) -> str:
    """`username` 对这篇论文的权限：`owner` / `write` / `read` / `""`（没有权限）。"""
    if not rec or not username:
        return ""
    if owner_of(rec) == username:
        return "owner"
    for s in shared_entries(rec):
        if s["user"] == username:
            return s["perm"]
    return ""


def reading_of(rec: dict, username: str) -> dict:
    """某个账号在这篇论文上的阅读状态（没读过时各项为空，由调用方兜底）。"""
    base = (rec or {}) if owner_of(rec) == username else \
        ((rec or {}).get("reading") or {}).get(username) or {}
    return {k: base.get(k) for k in READING_KEYS}


def reading_patch(rec: dict, username: str, fields: dict) -> dict:
    """更新某人的阅读状态，返回**要写回记录的字段**（owner 写顶层，其余写 reading）。"""
    if owner_of(rec) == username:
        return dict(fields)
    reading = dict((rec or {}).get("reading") or {})
    one = dict(reading.get(username) or {})
    one.update(fields)
    reading[username] = one
    return {"reading": reading}


def drop_reading(rec: dict, username: str) -> dict:
    """把某个人的阅读状态从记录里摘掉（共享被解除时用），返回要写回的字段。"""
    if owner_of(rec) == username:
        return {}                       # owner 的进度在顶层，不动
    reading = dict((rec or {}).get("reading") or {})
    if username not in reading:
        return {}
    reading.pop(username, None)
    return {"reading": reading}


def note_author(rec: dict, note: dict) -> str:
    """一条笔记是谁写的。

    老笔记（这条规则之前写的）没有 `by`，算**论文所有者**的：那时候要么只有所有者
    能写，要么还没共享给别人。有 `by` 就用它 —— 同一句只能存一条笔记，被共享的人
    改写后 `by` 跟着内容走（谁最后写就是谁写的）。
    """
    return str((note or {}).get("by") or "").strip() or owner_of(rec)


def anno_with_authors(anno: dict, rec: dict) -> dict:
    """把笔记的作者补上，供前端在**共享论文**里标识「这条笔记是谁写的」。

    规则只在服务端这一处：
      * 论文**有共享名单**时，每条笔记都带 `by`（无记录的算所有者的）；
      * **没共享**给别人时把 `by` 摘掉 —— 那种情况下只有自己能看，标了也没意义，
        前端见不到字段就自然不显示。
    """
    out = dict(anno or {})
    shared = bool(shared_entries(rec))
    for field in ("notes", "free_notes"):
        notes = out.get(field)
        if not isinstance(notes, list):
            continue
        if not shared:
            out[field] = [{k: v for k, v in n.items() if k != "by"}
                          if isinstance(n, dict) else n for n in notes]
        else:
            out[field] = [dict(n, by=note_author(rec, n)) if isinstance(n, dict) else n
                          for n in notes]
    return out


class Store:
    backend_name = "json"
    images_in_db = False          # 回退模式：图片依旧是磁盘文件（见 images_dir）

    def __init__(self, data_dir: Path | str):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.records_file = self.root / "records.json"
        self._lock = threading.Lock()
        # {doc_id: (mtime_ns, doc)} —— 按文件 mtime 自动失效（见 save_doc/read_doc）
        self._doc_cache: dict[str, tuple] = {}
        if not self.records_file.exists():
            self._write_records([])

    # ---------------- 路径 ----------------
    def doc_dir(self, doc_id: str) -> Path:
        return self.root / "docs" / doc_id

    def doc_file(self, doc_id: str) -> Path:
        return self.doc_dir(doc_id) / "doc.json"

    def images_dir(self, doc_id: str) -> Path:
        return self.doc_dir(doc_id) / "images"

    def anno_file(self, doc_id: str) -> Path:
        return self.doc_dir(doc_id) / "annotations.json"

    def trans_file(self, doc_id: str) -> Path:
        return self.doc_dir(doc_id) / "translations.json"

    def seltrans_file(self, doc_id: str) -> Path:
        return self.doc_dir(doc_id) / "seltrans.json"

    # ---------------- 阅读记录(records.json) ----------------
    def _read_records(self) -> list[dict]:
        with self._lock:
            try:
                return json.loads(self.records_file.read_text(encoding="utf-8"))
            except Exception:
                return []

    def _write_records(self, rows: list[dict]) -> None:
        with self._lock:
            self.records_file.write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    def ensure_record(self, meta: dict) -> None:
        rows = self._read_records()
        rows = [r for r in rows if r.get("id") != meta["id"]]
        rows.append(meta)
        self._write_records(rows)

    def list_records(self) -> list[dict]:
        rows = self._read_records()
        rows.sort(key=lambda r: r.get("last_read_at") or r.get("created_at") or "", reverse=True)
        return rows

    def list_by_owner(self, owner: str | None = None) -> list[dict]:
        """某个账号的论文列表（`owner=None` = 全部，管理/迁移用）。"""
        rows = self._read_records()
        if owner is not None:
            rows = [r for r in rows if owner_of(r) == owner]
        rows.sort(key=lambda r: r.get("last_read_at") or r.get("created_at") or "", reverse=True)
        return rows

    def list_for_user(self, username: str) -> list[dict]:
        """某个账号**能看到的**论文：自己上传的 + 别人共享给他的。"""
        rows = [r for r in self._read_records() if perm_of(r, username)]
        rows.sort(key=lambda r: (reading_of(r, username).get("last_read_at")
                                 or r.get("created_at") or ""), reverse=True)
        return rows

    def adopt_orphans(self, owner: str) -> int:
        """把没有 owner 的历史论文归给指定账号（升级到“按账号隔离”后跑一次）。"""
        rows = self._read_records()
        n = 0
        for r in rows:
            if not owner_of(r):
                r["owner"] = owner
                n += 1
        if n:
            self._write_records(rows)
        return n

    def purge_owner(self, owner: str) -> int:
        """删掉某账号的全部论文（含标注/笔记/译文/图片目录），返回篇数。"""
        mine = self.list_by_owner(owner)
        for r in mine:
            self.delete(r["id"])
        return len(mine)

    def get_record(self, doc_id: str) -> dict | None:
        for r in self._read_records():
            if r.get("id") == doc_id:
                return r
        return None

    def update_record(self, doc_id: str, **fields) -> dict | None:
        rows = self._read_records()
        for r in rows:
            if r.get("id") == doc_id:
                r.update(fields)
                self._write_records(rows)
                return r
        return None

    def delete(self, doc_id: str) -> None:
        rows = [r for r in self._read_records() if r.get("id") != doc_id]
        self._write_records(rows)
        self._doc_cache.pop(doc_id, None)
        if self.doc_dir(doc_id).exists():
            shutil.rmtree(self.doc_dir(doc_id), ignore_errors=True)

    # ---------------- 图片 ----------------
    # 接口与 `MongoStore` 对齐（那边存 GridFS，这边落盘）：`converter` 与
    # `/api/doc/{id}/img/{fname}` 用同一套调用，不用为两种后端分叉。
    def put_images(self, doc_id: str, src_dir: Path | str) -> int:
        """把 `src_dir` 里的图片收入这篇论文（返回张数）。"""
        src = Path(src_dir)
        if not src.exists():
            return 0
        dst = self.images_dir(doc_id)
        dst.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in sorted(src.iterdir()):
            if f.is_file():
                shutil.copyfile(f, dst / f.name)
                n += 1
        return n

    def get_image(self, doc_id: str, fname: str) -> bytes | None:
        f = self.images_dir(doc_id) / fname
        return f.read_bytes() if f.exists() else None

    def list_images(self, doc_id: str) -> list[str]:
        d = self.images_dir(doc_id)
        if not d.exists():
            return []
        return sorted(p.name for p in d.iterdir() if p.is_file())

    def delete_images(self, doc_id: str) -> int:
        d = self.images_dir(doc_id)
        if not d.exists():
            return 0
        n = sum(1 for p in d.iterdir() if p.is_file())
        shutil.rmtree(d, ignore_errors=True)
        return n

    # ---------------- 版式文档 ----------------
    def save_doc(self, doc_id: str, doc: dict) -> None:
        f = self.doc_file(doc_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        self._doc_cache[doc_id] = (f.stat().st_mtime_ns, doc)

    def read_doc(self, doc_id: str) -> dict:
        """读版式文档（带按文件 mtime 自动失效的内存缓存）。

        与 MongoStore.read_doc 是同一机制（那边用 rev）：每次先 stat 文件的
        mtime，与缓存条目不一致（或没有缓存）就重新读盘 —— 别的进程（重解析、
        脚本工具）写完文件后，本进程下一个请求自动看到新数据，不用重启服务。
        """
        f = self.doc_file(doc_id)
        try:
            mtime = f.stat().st_mtime_ns
        except OSError:
            raise FileNotFoundError(doc_id)
        ent = self._doc_cache.get(doc_id)
        if ent and ent[0] == mtime:
            return ent[1]
        doc = json.loads(f.read_text(encoding="utf-8"))
        self._doc_cache[doc_id] = (mtime, doc)
        return doc

    def clear_cache(self, doc_id: str) -> None:
        self._doc_cache.pop(doc_id, None)

    # ---------------- 标注与笔记 ----------------
    def _read_json(self, path: Path, default):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _write_json(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_annotations(self, doc_id: str) -> dict:
        anno = self._read_json(self.anno_file(doc_id),
                               {"highlights": {}, "notes": [], "free_notes": []})
        anno.setdefault("highlights", {})
        anno.setdefault("notes", [])
        anno.setdefault("free_notes", [])
        return anno

    def get_translations(self, doc_id: str) -> dict:
        return self._read_json(self.trans_file(doc_id), {})

    def set_translations(self, doc_id: str, items: dict[str, str]) -> None:
        cache = self.get_translations(doc_id)
        cache.update(items)
        self._write_json(self.trans_file(doc_id), cache)

    # ---------------- 所选句译文卡片（跨刷新/重新登录保留） ----------------
    def get_seltrans(self, doc_id: str) -> list[dict]:
        rows = self._read_json(self.seltrans_file(doc_id), [])
        return rows if isinstance(rows, list) else []

    def add_seltrans(self, doc_id: str, card: dict) -> list[dict]:
        rows = [r for r in self.get_seltrans(doc_id) if r.get("key") != card["key"]]
        rows.append({**card, "created_at": card.get("created_at") or _now()})
        self._write_json(self.seltrans_file(doc_id), rows)
        return rows

    def del_seltrans(self, doc_id: str, key: str) -> list[dict]:
        rows = [r for r in self.get_seltrans(doc_id) if r.get("key") != key]
        self._write_json(self.seltrans_file(doc_id), rows)
        return rows

    def clear_seltrans(self, doc_id: str) -> list[dict]:
        self.seltrans_file(doc_id).unlink(missing_ok=True)
        return []

    def apply_anno(self, doc_id: str, action: str, block_id: str | None = None,
                   color: str | None = None, text: str | None = None,
                   note_id: str | None = None, by: str | None = None,
                   title: str | None = None) -> dict:
        """返回更新后的 annotations。动作:
          add_highlight / remove_highlight / add_note / remove_note /
          add_free_note / update_free_note / remove_free_note

        `by` = 写这条笔记的账号（共享论文里要标识「谁写的」，见 `anno_with_authors`）。
        """
        anno = self.get_annotations(doc_id)
        hl = anno.setdefault("highlights", {})
        notes = anno.setdefault("notes", [])
        free_notes = anno.setdefault("free_notes", [])

        if action == "add_highlight" and block_id:
            hl[block_id] = {"color": color or "yellow", "at": _now()}
        elif action == "remove_highlight" and block_id:
            hl.pop(block_id, None)
        elif action == "add_note" and block_id and text is not None:
            existing = next((n for n in notes if n.get("block_id") == block_id), None)
            if existing:
                existing["text"] = text
                existing["updated_at"] = _now()
                if by:                 # 同一句只有一条笔记：改写后归最后写的人
                    existing["by"] = by
            else:
                note = {"id": _uid(), "block_id": block_id, "text": text,
                        "created_at": _now(), "updated_at": _now()}
                if by:
                    note["by"] = by
                notes.append(note)
        elif action == "remove_note" and note_id:
            anno["notes"] = [n for n in notes if n.get("id") != note_id]
        elif action == "add_free_note" and text is not None:
            now = _now()
            note = {"id": _uid(), "title": (title or "").strip(), "text": text,
                    "created_at": now, "updated_at": now}
            if by:
                note["by"] = by
            free_notes.append(note)
        elif action == "update_free_note" and note_id and text is not None:
            existing = next((n for n in free_notes if n.get("id") == note_id), None)
            if existing:
                existing["title"] = (title or "").strip()
                existing["text"] = text
                existing["updated_at"] = _now()
                if by:
                    existing["by"] = by
        elif action == "remove_free_note" and note_id:
            anno["free_notes"] = [n for n in free_notes if n.get("id") != note_id]

        self._write_json(self.anno_file(doc_id), anno)
        self.update_record(doc_id,
                           highlights=len(anno["highlights"]),
                           notes=len(anno["notes"]) + len(anno["free_notes"]))
        return anno
