"""Offline draft storage: incomplete setup, atomic saves and no secret leakage."""

import copy
import os
import sys
import tempfile
import tomllib
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from tgmover.config import load_config
from tgmover.filters import FilterSettings
from tgmover.gui_config import (
    ConfigDraftError,
    default_config_path,
    draft_defaults,
    effective_credentials_hint,
    read_draft,
    write_draft,
)
from tgmover.limits import LimitSettings


class GuiConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / "config.toml"
        clean_environment = {key: value for key, value in os.environ.items() if key not in {"API_ID", "API_HASH"}}
        self.environment = patch.dict(os.environ, clean_environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def read_saved(self):
        with self.path.open("rb") as handle:
            return tomllib.load(handle)

    def test_missing_file_has_all_defaults_and_never_creates_a_file(self):
        draft = read_draft(self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(draft["telegram"]["api_id"], 0)
        self.assertEqual(draft["telegram"]["api_hash"], "")
        self.assertTrue(set(asdict(LimitSettings())) <= set(draft["limits"]))
        self.assertTrue(set(asdict(FilterSettings())) <= set(draft["filters"]))
        self.assertIn("source", draft["sync"])
        self.assertIn("database", draft["storage"])
        self.assertEqual(draft["sync"]["source_topic_id"], 0)
        self.assertEqual(draft["sync"]["target_topic_id"], 0)
        self.assertIs(draft["storage"]["auto_cleanup_forwarded"], False)
        self.assertEqual(draft["storage"]["download_connections"], 4)

    def test_legacy_download_connections_default_and_selected_value_are_persisted(self):
        self.path.write_text('[storage]\ndownload_dir = "media"\n', encoding="utf-8")
        draft = read_draft(self.path)
        self.assertEqual(draft["storage"]["download_connections"], 4)
        draft["storage"]["download_connections"] = 6
        write_draft(self.path, draft)
        self.assertEqual(read_draft(self.path)["storage"]["download_connections"], 6)

    def test_cleanup_option_and_selected_download_directory_survive_draft_roundtrip(self):
        self.path.write_text('[storage]\ndownload_dir = "media"\nauto_cleanup_forwarded = true\n', encoding="utf-8")
        draft = read_draft(self.path)
        self.assertTrue(draft["storage"]["auto_cleanup_forwarded"])
        self.assertEqual(draft["storage"]["download_dir"], "media")
        write_draft(self.path, draft)
        self.assertEqual(self.read_saved()["storage"]["download_dir"], "media")
        self.assertTrue(self.read_saved()["storage"]["auto_cleanup_forwarded"])

    def test_legacy_draft_gains_topic_defaults_and_selected_ids_survive_save(self):
        self.path.write_text('[sync]\nsource = "@mine"\nsource_topic_id = 1\n', encoding="utf-8")
        draft = read_draft(self.path)
        self.assertEqual(draft["sync"]["source_topic_id"], 1)
        self.assertEqual(draft["sync"]["target_topic_id"], 0)
        draft["sync"]["target_topic_id"] = 990
        write_draft(self.path, draft)
        self.assertEqual(read_draft(self.path)["sync"]["target_topic_id"], 990)

    def test_half_configuration_can_be_opened_and_saved(self):
        self.path.write_text('[telegram]\napi_id = 123\n[sync]\nsource = "@mine"\n', encoding="utf-8")
        draft = read_draft(self.path)
        self.assertEqual(draft["telegram"]["api_id"], 123)
        self.assertEqual(draft["telegram"]["api_hash"], "")
        self.assertEqual(draft["sync"]["source"], "@mine")
        write_draft(self.path, draft)
        self.assertEqual(self.read_saved()["telegram"]["api_hash"], "")

    def test_environment_credentials_never_leak_into_defaults_drafts_or_save(self):
        with patch.dict(os.environ, {"API_ID": "987654321", "API_HASH": "very-sensitive-application-hash"}):
            defaults = draft_defaults()
            draft = read_draft(self.path)
            hint = effective_credentials_hint()
            write_draft(self.path, draft)
        for value in (str(defaults), str(draft), hint, self.path.read_text(encoding="utf-8")):
            self.assertNotIn("987654321", value)
            self.assertNotIn("very-sensitive-application-hash", value)
        self.assertIn("API_ID", hint)
        self.assertIn("API_HASH", hint)

    def test_explicit_draft_credentials_are_preserved_despite_environment(self):
        draft = {"telegram": {"api_id": 42, "api_hash": "a" * 32}}
        with patch.dict(os.environ, {"API_ID": "123", "API_HASH": "b" * 32}):
            write_draft(self.path, draft)
            loaded = read_draft(self.path)
        self.assertEqual(loaded["telegram"], {**draft_defaults()["telegram"], **draft["telegram"]})
        self.assertEqual(self.read_saved(), draft)

    def test_environment_hint_covers_partial_empty_and_absent_variables(self):
        self.assertIn("未检测到", effective_credentials_hint())
        with patch.dict(os.environ, {"API_ID": ""}):
            hint = effective_credentials_hint()
            self.assertIn("API_ID", hint)
            self.assertNotIn("API_HASH", hint)

    def test_unknown_nested_values_arrays_and_tables_survive_round_trip(self):
        unknown = {"telegram": {"future": {"nested": ["alpha", "beta"]}}, "future": {"rules": [{"name": "first", "value": 7}]}, "format_version": 2}
        write_draft(self.path, unknown)
        draft = read_draft(self.path)
        write_draft(self.path, draft)
        saved = self.read_saved()
        self.assertEqual(saved["telegram"]["future"], unknown["telegram"]["future"])
        self.assertEqual(saved["future"], unknown["future"])
        self.assertEqual(saved["format_version"], 2)

    def test_only_supplied_draft_is_serialized_without_implicit_defaults(self):
        draft = {"telegram": {"api_id": 0, "api_hash": ""}, "sync": {"source": ""}}
        before = copy.deepcopy(draft)
        write_draft(self.path, draft)
        self.assertEqual(self.read_saved(), before)
        self.assertEqual(draft, before)

    def test_defaults_are_independent_between_calls(self):
        first = draft_defaults()
        first["telegram"]["api_id"] = 999
        first["limits"]["enabled"] = False
        second = draft_defaults()
        self.assertEqual(second["telegram"]["api_id"], 0)
        self.assertTrue(second["limits"]["enabled"])

    def test_read_and_save_do_not_change_process_directory_or_relative_paths(self):
        before = Path.cwd()
        draft = {"telegram": {"api_id": 123, "api_hash": "a" * 32, "session": "nested/user"}, "sync": {"source": "@source", "target": "@target"}, "storage": {"database": "nested/state.db", "download_dir": "media"}}
        write_draft(self.path, draft)
        loaded = read_draft(self.path)
        runtime = load_config(self.path)
        self.assertEqual(Path.cwd(), before)
        self.assertEqual(loaded["telegram"]["session"], "nested/user")
        self.assertEqual(runtime.session, (self.base / "nested/user").resolve())
        self.assertEqual(runtime.download_dir, (self.base / "media").resolve())

    def test_replace_failure_preserves_original_and_removes_temporary_file(self):
        self.path.write_bytes(b"# retained exactly\n[telegram]\napi_id = 1\n")
        before = self.path.read_bytes()
        with patch("tgmover.gui_config.os.replace", side_effect=PermissionError("private-value")):
            with self.assertRaises(ConfigDraftError) as caught:
                write_draft(self.path, {"telegram": {"api_id": 2}})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("private-value", str(caught.exception))
        self.assertEqual(list(self.base.iterdir()), [self.path])

    def test_fsync_failure_never_replaces_original(self):
        self.path.write_bytes(b"original")
        with patch("tgmover.gui_config.os.fsync", side_effect=OSError("disk")), patch("tgmover.gui_config.os.replace") as replace:
            with self.assertRaises(ConfigDraftError):
                write_draft(self.path, {"telegram": {"api_id": 2}})
        replace.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b"original")
        self.assertEqual(list(self.base.iterdir()), [self.path])

    def test_login_keys_are_rejected_even_deep_in_arrays_case_or_separator_variants(self):
        self.path.write_bytes(b"original")
        for key in ("phone", "Phone_Number", "OTP", "verification-code", "login_code", "password", "bot-token", "access_token", "token", "string_session"):
            with self.subTest(key=key):
                draft = {"unknown": {"nested": [{key: "do-not-echo-this-secret"}]}}
                with self.assertRaises(ConfigDraftError) as caught:
                    write_draft(self.path, draft)
                self.assertNotIn("do-not-echo-this-secret", str(caught.exception))
                self.assertEqual(self.path.read_bytes(), b"original")

    def test_unsupported_values_invalid_tables_and_non_dict_roots_fail_before_write(self):
        self.path.write_bytes(b"original")
        for draft in ([], {1: "wrong key"}, {"telegram": "wrong table"}, {"nested": None}, {"unknown": {"value": object()}}, {"unknown": {"value": {1, 2}}}):
            with self.subTest(draft_type=type(draft).__name__):
                with self.assertRaises(ConfigDraftError):
                    write_draft(self.path, draft)
                self.assertEqual(self.path.read_bytes(), b"original")

    def test_cycle_is_reported_without_changing_original(self):
        draft = {}
        draft["loop"] = draft
        with self.assertRaises(ConfigDraftError):
            write_draft(self.path, draft)
        self.assertFalse(self.path.exists())

    def test_malformed_toml_errors_do_not_quote_source_or_secret(self):
        self.path.write_text('[telegram]\napi_hash = secret-sensitive-text\n', encoding="utf-8")
        with self.assertRaises(ConfigDraftError) as caught:
            read_draft(self.path)
        self.assertNotIn("secret-sensitive-text", str(caught.exception))
        self.assertIn("TOML", str(caught.exception))

    def test_read_rejects_non_table_known_sections_and_forbidden_login_fields(self):
        for content in ('telegram = "wrong"\n', '[telegram]\nphone = "+86123"\n'):
            self.path.write_text(content, encoding="utf-8")
            with self.assertRaises(ConfigDraftError):
                read_draft(self.path)
            self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    def test_source_default_path_stays_in_user_root(self):
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(default_config_path(), Path(__file__).resolve().parents[2] / "config.toml")

    def test_frozen_paths_use_executable_directory_and_bundled_template(self):
        bundle = self.base / "bundle"
        bundle.mkdir()
        (bundle / "config.example.toml").write_text('[telegram]\nsession = "frozen-session"\n[sync]\nsource = "@frozen"\n', encoding="utf-8")
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(self.base / "TelegramMover.exe")), patch.object(sys, "_MEIPASS", str(bundle), create=True):
            self.assertEqual(default_config_path(), self.path)
            self.assertEqual(draft_defaults()["telegram"]["session"], "frozen-session")


if __name__ == "__main__":
    unittest.main()
