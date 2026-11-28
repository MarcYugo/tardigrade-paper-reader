"""把本地 JSON 数据迁移到 MongoDB

把 `data/` 下原有的东西搬到 MongoDB（默认目标就是 `mongo_configuration/` 那份 compose）：

| 本地文件 | → MongoDB |
| --- | --- |
| `data/records.json` | `records` 集合 |
| `data/docs/<id>/doc.json` | `documents` 集合 |
| `data/docs/<id>/images/*` | `images` 桶（GridFS，插图本体） |
| `data/docs/<id>/annotations.json` | `highlights` + `notes` 集合 |
| `data/docs/<id>/translations.json` | `translations` 集合 |
| `data/docs/<id>/seltrans.json` | `seltrans` 集合 |
| `data/users.json` | `users` 集合（口令哈希原样搬，密码不变） |
| `data/transfer_keys.json` | `transfer_keys` 集合（转交 Key，随账号一起搬） |
| `data/llm.json` | `settings` 集合（`_id="llm"`） |

插图也搬进库里（GridFS），所以迁完就只有 `documents.images` 一个来源，
服务重启/换机器不再丢图；确认无误后 `data/docs/` 可以整个删掉。

用法：
    python tools/migrate_to_mongo.py                 # 迁移（已存在的同 id 会被覆盖）
    python tools/migrate_to_mongo.py --dry-run       # 只统计不写入
    python tools/migrate_to_mongo.py --skip-users    # 不迁移账号
    python tools/migrate_to_mongo.py --skip-images   # 不迁移插图
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from backend import db as dbmod                      # noqa: E402
from backend.mongo_store import (                    # noqa: E402
    MongoAuthBackend, MongoSettingsBackend, MongoStore)


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  ⚠️ 跳过（解析失败）{path}: {exc}")
        return default


def main() -> int:
    ap = argparse.ArgumentParser(description="本地 JSON 数据 → MongoDB")
    ap.add_argument("--data-dir", default=str(BASE_DIR / "data"), help="本地数据目录(默认 data/)")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写入数据库")
    ap.add_argument("--skip-users", action="store_true", help="不迁移账号")
    ap.add_argument("--skip-images", action="store_true", help="不迁移插图(GridFS)")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    cfg = dbmod.mongo.cfg
    print(f"目标 MongoDB：{dbmod.mask_uri(cfg['uri'])}  库={cfg['db']}")

    if args.dry_run:
        print("（--dry-run：只统计，不连接数据库、不写入）")
    else:
        if not dbmod.mongo.refresh():
            print(f"❌ 连不上 MongoDB：{dbmod.mongo.error}")
            print("   先启动数据库：cd mongo_configuration && cp .env.example .env && docker compose up -d")
            return 1
        print("✅ 已连接")

    store = None if args.dry_run else MongoStore(data_dir)
    users_be = None if args.dry_run else MongoAuthBackend()
    set_be = None if args.dry_run else MongoSettingsBackend()

    # ---------------- 设置 ----------------
    llm = _read_json(data_dir / "llm.json", {})
    if llm:
        if set_be:
            set_be.save(llm)
        print(f"设置      ：llm.json → settings（{', '.join(k for k in llm if k != 'api_key')}, api_key={'有' if llm.get('api_key') else '无'}）")
    else:
        print("设置      ：无 llm.json，跳过")

    # ---------------- 账号 ----------------
    users = _read_json(data_dir / "users.json", {})
    if users and not args.skip_users:
        if users_be:
            for key, rec in users.items():
                users_be.user_put(key, rec)
        print(f"账号      ：users.json → users（{len(users)} 个：{', '.join(u.get('username', '?') for u in users.values())}）")
    elif args.skip_users:
        print("账号      ：按参数跳过")
    else:
        print("账号      ：无 users.json，跳过")

    # ---------------- 转交 Key（跟着账号走） ----------------
    tk_recs: list[tuple[str, dict]] = []
    tkeys = _read_json(data_dir / "transfer_keys.json", {})
    if isinstance(tkeys, dict):
        for uname, doc in tkeys.items():
            if not isinstance(doc, dict):
                continue
            for rec in doc.get("keys") or []:
                tk_recs.append((doc.get("username") or uname, rec))
    if tk_recs and not args.skip_users:
        if users_be:
            for uname, rec in tk_recs:
                users_be.transfer_add(uname, rec)
        print(f"转交 Key  ：transfer_keys.json → transfer_keys（{len(tk_recs)} 把）")
    elif args.skip_users:
        print("转交 Key  ：按参数跳过")
    else:
        print("转交 Key  ：无 transfer_keys.json，跳过")

    # ---------------- 论文 ----------------
    records = _read_json(data_dir / "records.json", [])
    print(f"论文记录  ：records.json → records（{len(records)} 篇）")
    n_doc = n_anno = n_trans = 0
    n_img_papers = n_img_files = 0
    total_trans = 0
    for meta in records:
        doc_id = meta.get("id")
        if not doc_id:
            continue
        d = data_dir / "docs" / doc_id
        doc = _read_json(d / "doc.json", None)
        if doc and store:
            store.save_doc(doc_id, doc)
        n_doc += 1 if doc else 0
        # 插图：data/docs/<id>/images/ → GridFS
        img_dir = d / "images"
        img_n = 0
        if not args.skip_images and img_dir.is_dir():
            img_n = sum(1 for p in img_dir.iterdir() if p.is_file())
            if store and img_n:
                store.put_images(doc_id, img_dir)
            n_img_files += img_n
            n_img_papers += 1 if img_n else 0
        anno = _read_json(d / "annotations.json", None)
        if anno and store:
            store.import_annotations(doc_id, anno)
            n_anno += 1
            note_n = len(anno.get("notes") or [])
            hl_n = len(anno.get("highlights") or {})
        else:
            note_n = hl_n = 0
        trans = _read_json(d / "translations.json", None)
        if trans and store:
            store.set_translations(doc_id, trans)
            n_trans += 1
            total_trans += len(trans)
        if store:
            store.records.replace_one({"_id": doc_id}, dict(meta), upsert=True)
        print(f"  · {doc_id}  {str(meta.get('title'))[:40]:42s} 高亮 {hl_n:>3} 笔记 {note_n:>3} 译文 {len(trans or {}):>3} 图片 {img_n:>3}")
    print(f"汇总      ：版式文档 {n_doc} 篇、标注 {n_anno} 篇、译文 {n_trans} 篇/共 {total_trans} 条"
          + ("、插图已跳过" if args.skip_images
             else f"、插图 {n_img_files} 张（{n_img_papers} 篇）已进 GridFS"))
    if not args.dry_run:
        print("\n完成 ✅ 现在直接启动服务即可（APP_STORAGE 默认 auto，会优先用 MongoDB）。")
        print("      如果服务已在运行：打开右上角「⚙ LLM 设置」→「重试连接 MongoDB」。")
        if not args.skip_images and n_img_files:
            print("      插图已进库，确认阅读页图片正常后，data/docs/ 可以整个删掉。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
