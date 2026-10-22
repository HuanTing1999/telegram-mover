"""Synthetic app credential sources, private status, and real config fallback."""

import io
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tgmover.config import ConfigError, load_config
from tgmover.credentials import (
    CredentialsError, application_directory, bundle_directory, credentials_status,
    main, read_application_credentials, resolve_credentials,
)
from tgmover.gui_config import read_draft, write_draft


HASH_A, HASH_B, HASH_C = "a" * 32, "b" * 32, "c" * 32
ID_A, ID_B, ID_C = 123456789, 234567891, 345678912


class CredentialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.app, self.bundle = self.base / "application", self.base / "bundle"
        self.app.mkdir()
        self.bundle.mkdir()
        clean = {key: value for key, value in os.environ.items()
                 if key not in {"API_ID", "API_HASH", "TGMOVER_APP_CREDENTIALS"}}
        for patcher in (
            patch.dict(os.environ, clean, clear=True),
            patch("tgmover.credentials.application_directory", return_value=self.app),
            patch("tgmover.credentials.bundle_directory", return_value=self.bundle),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def app_file(self, directory=None, identifier=ID_A, secret=HASH_A):
        path = (directory or self.app) / "app_credentials.toml"
        path.write_text(f'[telegram]\napi_id={identifier}\napi_hash="{secret}"\n', encoding="utf-8")
        return path

    def pair(self, resolved, identifier, secret):
        self.assertEqual((resolved.api_id, resolved.api_hash), (identifier, secret))
        self.assertTrue(resolved.source_label)
        for value in (str(identifier), secret):
            self.assertNotIn(value, repr(resolved))
            self.assertNotIn(value, resolved.source_label)

    def test_complete_environment_overrides_all_damaged_lower_sources(self):
        self.app_file().write_text("invalid TOML private lower value", encoding="utf-8")
        with patch.dict(os.environ, {"API_ID": str(ID_C), "API_HASH": HASH_C,
                                     "TGMOVER_APP_CREDENTIALS": str(self.base / "missing.toml")}):
            self.pair(resolve_credentials({"api_id": "invalid", "api_hash": "invalid"}), ID_C, HASH_C)
            with self.assertRaises(CredentialsError):
                resolve_credentials("invalid telegram structure")

    def test_each_environment_field_overrides_complete_config(self):
        config = {"api_id": ID_A, "api_hash": HASH_A, "session": "data/account"}
        with patch.dict(os.environ, {"API_ID": str(ID_B)}):
            self.pair(resolve_credentials(config), ID_B, HASH_A)
        with patch.dict(os.environ, {"API_HASH": HASH_B}):
            self.pair(resolve_credentials(config), ID_A, HASH_B)

    def test_partial_config_is_completed_only_by_matching_environment_field(self):
        self.app_file(identifier=ID_C, secret=HASH_C)
        for config in ({"api_id": ID_A}, {"api_hash": HASH_A}):
            with self.subTest(config=config), self.assertRaises(CredentialsError):
                resolve_credentials(config)
        with patch.dict(os.environ, {"API_HASH": HASH_A}):
            self.pair(resolve_credentials({"api_id": ID_A}), ID_A, HASH_A)
        with patch.dict(os.environ, {"API_ID": str(ID_A)}):
            self.pair(resolve_credentials({"api_hash": HASH_A}), ID_A, HASH_A)

    def test_external_config_takes_precedence_over_software_files(self):
        self.app_file(identifier=ID_B, secret=HASH_B)
        self.app_file(self.bundle, identifier=ID_C, secret=HASH_C)
        self.pair(resolve_credentials({"api_id": ID_A, "api_hash": HASH_A}), ID_A, HASH_A)

    def test_zero_empty_placeholders_fallback_then_environment_overrides_field(self):
        self.app_file()
        for config in ({}, {"api_id": 0, "api_hash": ""}, {"api_id": "0", "api_hash": ""}):
            with self.subTest(config=config):
                self.pair(resolve_credentials(config), ID_A, HASH_A)
        with patch.dict(os.environ, {"API_ID": str(ID_B)}):
            self.pair(resolve_credentials({}), ID_B, HASH_A)

    def test_explicit_file_then_external_then_bundle_source_precedence(self):
        self.app_file(self.bundle, ID_C, HASH_C)
        self.pair(resolve_credentials({}), ID_C, HASH_C)
        self.app_file(identifier=ID_B, secret=HASH_B)
        self.pair(resolve_credentials({}), ID_B, HASH_B)
        selected = self.base / "selected.toml"
        selected.write_text(f'[telegram]\napi_id={ID_A}\napi_hash="{HASH_A}"\n', encoding="utf-8")
        with patch.dict(os.environ, {"TGMOVER_APP_CREDENTIALS": str(selected)}):
            self.pair(resolve_credentials({}), ID_A, HASH_A)
        with patch.dict(os.environ, {"TGMOVER_APP_CREDENTIALS": str(self.base / "missing.toml")}):
            with self.assertRaises(CredentialsError):
                resolve_credentials({})

    def test_nonempty_invalid_and_empty_environment_never_fall_back(self):
        self.app_file()
        for environment in ({"API_ID": ""}, {"API_HASH": ""}, {"API_ID": "0"},
                            {"API_ID": "secret-invalid-value"}, {"API_HASH": "secret-invalid-value"}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment):
                with self.assertRaises(CredentialsError) as caught:
                    resolve_credentials({})
                self.assertNotIn("secret-invalid-value", str(caught.exception))

    def test_app_file_requires_only_complete_app_pair_and_hides_parse_errors(self):
        path = self.app_file()
        self.pair(read_application_credentials(path), ID_A, HASH_A)
        for payload in (
            '[telegram]\napi_id=0\napi_hash="' + HASH_A + '"',
            '[telegram]\napi_id=true\napi_hash="' + HASH_A + '"',
            '[telegram]\napi_id=12345',
            '[telegram]\napi_id=12345\napi_hash="bad-secret-value"',
            '[telegram]\napi_id=12345\napi_hash="' + HASH_A + '"\nsession="private"',
            '[telegram]\napi_id=12345\napi_hash="' + HASH_A + '"\n[sync]\nsource="private"',
            '[telegram]\napi_hash="bad-secret-value',
        ):
            with self.subTest(payload=payload):
                path.write_text(payload, encoding="utf-8")
                with self.assertRaises(CredentialsError) as caught:
                    read_application_credentials(path)
                self.assertNotIn("bad-secret-value", str(caught.exception))
                self.assertNotIn(HASH_A, str(caught.exception))

    def test_missing_status_and_draft_ready_status_never_expose_values(self):
        missing = self.base / "does-not-exist.toml"
        not_ready = credentials_status(missing)
        self.assertFalse(not_ready.ready)
        self.assertTrue(not_ready.message)
        ready = credentials_status(missing, telegram={"api_id": ID_A, "api_hash": HASH_A})
        self.assertTrue(ready.ready)
        for value in (str(ID_A), HASH_A):
            self.assertNotIn(value, repr(ready))
            self.assertNotIn(value, ready.message)
        self.assertFalse(missing.exists())

    def test_actual_config_loader_uses_fallback_without_creating_state_or_copying_draft_keys(self):
        self.app_file()
        config_path = self.base / "config.toml"
        config_path.write_text('[telegram]\napi_id=0\napi_hash=""\n[sync]\nsource="@source"\ntarget="@target"\n', encoding="utf-8")
        before = config_path.read_bytes()
        loaded = load_config(config_path)
        self.assertEqual((loaded.api_id, loaded.api_hash), (ID_A, HASH_A))
        self.assertEqual(config_path.read_bytes(), before)
        self.assertFalse((self.base / "data").exists())
        draft = read_draft(config_path)
        self.assertEqual((draft["telegram"]["api_id"], draft["telegram"]["api_hash"]), (0, ""))
        write_draft(config_path, draft)
        saved = config_path.read_text(encoding="utf-8")
        self.assertNotIn(HASH_A, saved)
        self.assertNotIn(str(ID_A), saved)

    def test_config_invalid_pair_and_structure_remain_errors(self):
        self.app_file()
        path = self.base / "config.toml"
        path.write_text('[telegram]\napi_id=12345\n[sync]\nsource="@source"\ntarget="@target"', encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)
        path.write_text('telegram="invalid"\n[sync]\nsource="@source"\ntarget="@target"', encoding="utf-8")
        with patch.dict(os.environ, {"API_ID": str(ID_A), "API_HASH": HASH_A}):
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_resolution_is_read_only_and_never_connects_network(self):
        self.app_file()
        before = {path: path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        with patch("socket.create_connection", side_effect=AssertionError("Network access is forbidden")):
            self.pair(resolve_credentials({}), ID_A, HASH_A)
            self.assertTrue(credentials_status(self.base / "missing.toml").ready)
        after = {path: path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_explicit_build_staging_strips_comments_and_never_prints_values(self):
        source = self.app_file()
        source.write_text("# private comment must not be embedded\n" + source.read_text(encoding="utf-8"), encoding="utf-8")
        staged = self.base / "staged.toml"
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(["--validate-file", str(source), "--stage-file", str(staged)]), 0)
        self.pair(read_application_credentials(staged), ID_A, HASH_A)
        self.assertNotIn("private comment", staged.read_text(encoding="utf-8"))
        self.assertNotIn(HASH_A, stdout.getvalue() + stderr.getvalue())
        source.write_text('[telegram]\napi_id=12345\napi_hash="' + HASH_A + '"\nsession="private"', encoding="utf-8")
        before = staged.read_bytes()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(["--validate-file", str(source), "--stage-file", str(staged)]), 1)
        self.assertEqual(staged.read_bytes(), before)

    def test_boolean_ids_are_explicit_invalid_config_not_missing_placeholders(self):
        self.app_file()
        for value in (False, True):
            with self.subTest(value=value), self.assertRaises(CredentialsError):
                resolve_credentials({"api_id": value, "api_hash": ""})


class CredentialPathsTests(unittest.TestCase):
    def test_source_and_frozen_directory_selection(self):
        with patch("tgmover.credentials.sys.frozen", False, create=True):
            self.assertEqual(application_directory(), Path(__file__).resolve().parents[2])
            self.assertIsNone(bundle_directory())
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            folder = Path(temporary)
            with patch("tgmover.credentials.sys.frozen", True, create=True), \
                    patch("tgmover.credentials.sys.executable", str(folder / "Application.exe")), \
                    patch("tgmover.credentials.sys._MEIPASS", str(folder / "bundle"), create=True):
                self.assertEqual(application_directory(), folder.resolve())
                self.assertEqual(bundle_directory(), folder / "bundle")


if __name__ == "__main__":
    unittest.main()
