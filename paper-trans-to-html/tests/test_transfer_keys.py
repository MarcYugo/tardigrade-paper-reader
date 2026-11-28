"""转交 Key（transfer key）：生成 / 校验 / 用量 / 删除 / 与账号存储同时创建。

只测 `JsonAuthBackend`（MongoDB 后端接口与它一一对应，逻辑在 `AuthStore` 里共用）。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.auth import TRANSFER_PREFIX, AuthError, AuthStore, JsonAuthBackend


class TransferKeyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.auth = AuthStore(self.root, backend=JsonAuthBackend(self.root))
        self.auth.create_user("Alice", "password123")
        self.auth.create_user("Bob", "password123")

    def tearDown(self):
        self._tmp.cleanup()

    def test_storage_created_with_accounts(self):
        # 回退模式：transfer_keys.json 与账号存储一并建好（Mongo 版在 backend 初始化时建集合）
        self.assertTrue((self.root / "transfer_keys.json").exists())

    def test_create_and_resolve(self):
        rec = self.auth.create_transfer_key("Alice", "  obsidian  插件 ")
        self.assertTrue(rec["key"].startswith(TRANSFER_PREFIX))
        self.assertEqual(rec["name"], "obsidian 插件")       # 连续空白压成一个空格
        self.assertEqual([k["id"] for k in self.auth.list_transfer_keys("Alice")],
                         [rec["id"]])
        who = self.auth.resolve_transfer_key(rec["key"])
        self.assertEqual(who["username"], "Alice")
        self.assertEqual(who["id"], rec["id"])
        self.assertEqual(who["name"], "obsidian 插件")
        # 每次调用累计用量与最近使用时间
        self.auth.resolve_transfer_key(rec["key"])
        one = self.auth.list_transfer_keys("Alice")[0]
        self.assertEqual(one["uses"], 2)
        self.assertTrue(one["last_used_at"])
        # key 按账号隔离：别的账号看不到
        self.assertEqual(self.auth.list_transfer_keys("Bob"), [])

    def test_invalid_and_delete(self):
        rec = self.auth.create_transfer_key("Alice")
        self.assertIsNone(self.auth.resolve_transfer_key("ptk_不存在"))
        self.assertIsNone(self.auth.resolve_transfer_key(""))
        self.assertTrue(self.auth.delete_transfer_key("Alice", rec["id"]))
        self.assertIsNone(self.auth.resolve_transfer_key(rec["key"]))
        self.assertFalse(self.auth.delete_transfer_key("Alice", rec["id"]))

    def test_limit(self):
        with patch("backend.auth.TRANSFER_MAX", 2):
            self.auth.create_transfer_key("Alice")
            self.auth.create_transfer_key("Alice")
            with self.assertRaises(AuthError):
                self.auth.create_transfer_key("Alice")

    def test_close_account_revokes_keys(self):
        rec = self.auth.create_transfer_key("Alice")
        self.auth.close_account("Alice", "password123", None)   # None = 跳过用户名确认
        self.assertIsNone(self.auth.resolve_transfer_key(rec["key"]))
        self.assertEqual(self.auth.list_transfer_keys("Alice"), [])


if __name__ == "__main__":
    unittest.main()
