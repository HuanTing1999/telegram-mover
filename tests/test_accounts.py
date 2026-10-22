"""Local account index privacy, atomic replacement, and session identities."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tgmover.accounts import AccountStore, AccountStoreError


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = AccountStore(self.base / "config.toml")

    def session(self, name="existing"):
        path = self.base / f"{name}.session"
        path.write_bytes(b"unchanged-session-bytes")
        return path

    def test_phone_session_is_canonical_opaque_and_does_not_create_index(self):
        phone = "+8613800138000"
        expected = self.base / "data/accounts" / hashlib.sha256(phone.encode()).hexdigest()[:24]
        self.assertEqual(self.store.session_for_phone(phone), expected)
        self.assertEqual(self.store.session_for_phone("0086 (138) 0013-8000"), expected)
        self.assertEqual(self.store.session_for_phone("＋８６ １３８００１３８０００"), expected)
        self.assertNotEqual(self.store.session_for_phone("+12025550123"), expected)
        self.assertNotIn(phone.lstrip("+"), str(expected))
        self.assertEqual(self.store.list_accounts(), [])
        self.assertFalse((self.base / "data").exists())

    def test_remember_upserts_normalized_session_and_never_changes_its_bytes(self):
        path = self.session()
        first = self.store.remember(42, "Alice", path.with_suffix(""))
        second = self.store.remember(42, "Alice updated", path)
        self.assertEqual(first["key"], second["key"])
        self.assertEqual(set(second), {"id", "key", "label", "session"})
        self.assertEqual(self.store.list_accounts(), [second])
        self.assertEqual(path.read_bytes(), b"unchanged-session-bytes")
        self.assertEqual(AccountStore(self.base / "config.toml").list_accounts(), [second])
        self.assertEqual(Path(second["session"]), path.resolve())

    def test_external_sessions_are_registered_without_moving_or_deleting(self):
        external = self.base / "external" / "different.session"
        external.parent.mkdir()
        external.write_bytes(b"external-account")
        self.store.remember(42, "Alice", self.session())
        row = self.store.remember(43, "Bob", external)
        self.assertEqual(len(self.store.list_accounts()), 2)
        self.assertEqual(external.read_bytes(), b"external-account")
        self.assertEqual(Path(row["session"]), external.resolve())

    def test_unlogged_session_and_invalid_user_or_phone_do_not_create_index(self):
        with self.assertRaises(AccountStoreError):
            self.store.remember(42, "Alice", self.base / "missing")
        for identifier in (0, -1, True, "42"):
            with self.subTest(identifier=identifier), self.assertRaises(AccountStoreError):
                self.store.remember(identifier, "Alice", self.base / "missing")
        with self.assertRaises(ValueError) as caught:
            self.store.session_for_phone("private invalid input")
        self.assertNotIn("private invalid input", str(caught.exception))
        self.assertFalse(self.store.path.exists())

    def test_phone_labels_are_not_persisted_and_only_expected_fields_are_written(self):
        row = self.store.remember(42, "+86 138-0013-8000", self.session())
        self.assertEqual(row["label"], "账号 42")
        text = self.store.path.read_text(encoding="utf-8")
        self.assertNotIn("138", text)
        self.assertEqual(set(json.loads(text)[0]), {"id", "key", "label", "session"})

    def test_conflicting_identity_preserves_original_index_and_session(self):
        path = self.session()
        self.store.remember(42, "Alice", path)
        before = self.store.path.read_bytes()
        with self.assertRaises(AccountStoreError):
            self.store.remember(43, "Other account", path)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(path.read_bytes(), b"unchanged-session-bytes")

    def test_corrupt_unknown_fields_duplicates_and_modified_keys_are_not_overwritten(self):
        path = self.session()
        row = self.store.remember(42, "Alice", path)
        for payload in ("{malformed", json.dumps({"accounts": []}), json.dumps([row, row]),
                        json.dumps([{**row, "phone": "+12025550123"}]),
                        json.dumps([{**row, "key": "a" * 24}]),
                        json.dumps([row]).replace('"id": 42', '"id": 43, "id": 42')):
            with self.subTest(payload=payload):
                self.store.path.write_text(payload, encoding="utf-8")
                before = self.store.path.read_bytes()
                with self.assertRaises(AccountStoreError):
                    self.store.remember(42, "Alice", path)
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_atomic_write_failure_preserves_old_index_and_removes_temporary(self):
        path = self.session()
        self.store.remember(42, "Alice", path)
        before = self.store.path.read_bytes()
        with patch("tgmover.accounts.os.replace", side_effect=OSError("private-value-must-not-appear")):
            with self.assertRaises(AccountStoreError) as caught:
                self.store.remember(42, "New label", path)
        self.assertNotIn("private-value-must-not-appear", str(caught.exception))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(list(self.store.path.parent.glob(".accounts-*.tmp")), [])

    def test_writer_lock_prevents_lost_updates_and_readers_keep_old_snapshot(self):
        first = self.store.remember(42, "Alice", self.session())
        other = AccountStore(self.base / "config.toml")
        second_session = self.session("second-account")
        with self.store._lock():
            self.assertEqual(other.list_accounts(), [first])
            with self.assertRaises(AccountStoreError):
                other.remember(43, "Bob", second_session)
            self.assertEqual(self.store.list_accounts(), [first])
        other.remember(43, "Bob", second_session)
        self.assertEqual([row["id"] for row in self.store.list_accounts()], [42, 43])

    @unittest.skipUnless(os.name == "nt", "Windows paths compare without case")
    def test_windows_case_aliases_share_one_session_identity(self):
        path = self.session("MixedCase")
        row = self.store.remember(42, "Alice", path)
        updated = self.store.remember(42, "Updated", Path(str(path).upper()))
        self.assertEqual(row["key"], updated["key"])
        self.assertEqual(len(self.store.list_accounts()), 1)


if __name__ == "__main__":
    unittest.main()
