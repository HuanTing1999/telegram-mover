"""Offline launch tests: no Telegram connection, interactive login or real EXE."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import launcher


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.exe_directory = self.base / "application"
        self.source_directory = self.base / "source"
        self.bundle = self.base / "bundle"
        self.other_directory = self.base / "unrelated-working-folder"
        for directory in (self.exe_directory, self.source_directory, self.bundle, self.other_directory):
            directory.mkdir()
        self.example = b'[telegram]\nsession = "data/user"\n[sync]\nsource = "@source"\n'
        (self.bundle / "config.example.toml").write_bytes(self.example)
        (self.source_directory / "config.example.toml").write_bytes(self.example)
        self.original_directory = Path.cwd()
        os.chdir(self.other_directory)
        self.addCleanup(os.chdir, self.original_directory)

    def invoke(self, arguments=(), *, frozen=True, main_result=0, failure=None, pause_failure=None):
        invoked_arguments = []

        def application_main():
            invoked_arguments.extend(sys.argv)
            if failure:
                raise failure
            return main_result

        application = Mock(side_effect=application_main)
        prompt = Mock(side_effect=pause_failure) if pause_failure else Mock(return_value="")
        argv = [str(self.exe_directory / "TGMover.exe"), *arguments]
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", argv))
            stack.enter_context(patch.object(sys, "frozen", frozen, create=True))
            stack.enter_context(patch.object(sys, "executable", str(self.exe_directory / "TGMover.exe")))
            stack.enter_context(patch.object(sys, "_MEIPASS", str(self.bundle), create=True))
            stack.enter_context(patch.object(launcher, "__file__", str(self.source_directory / "launcher.py")))
            stack.enter_context(patch.object(launcher.main, "main", application))
            stack.enter_context(patch("builtins.input", prompt))
            stack.enter_context(redirect_stdout(output))
            result = launcher.entrypoint()
            self.assertIs(sys.argv, argv)
        self.assertEqual(Path.cwd(), self.other_directory)
        return result, application, prompt, invoked_arguments, output.getvalue()

    @staticmethod
    def selected_config(arguments):
        for index, value in reversed(list(enumerate(arguments))):
            if value.startswith("--config="):
                return Path(value.partition("=")[2])
            if value == "--config":
                return Path(arguments[index + 1])
        raise AssertionError("No config argument was passed to main")

    def test_frozen_default_uses_exe_directory_independent_of_cwd(self):
        result, application, _, arguments, output = self.invoke(["--once"])

        expected = self.exe_directory / "config.toml"
        self.assertEqual(result, 0)
        application.assert_called_once()
        self.assertEqual(self.selected_config(arguments), expected)
        self.assertTrue(self.selected_config(arguments).is_absolute())
        self.assertEqual(expected.read_bytes(), self.example)
        self.assertFalse((self.other_directory / "config.toml").exists())
        self.assertIn(str(expected), output)

    def test_source_default_uses_launcher_directory(self):
        _, _, prompt, arguments, _ = self.invoke(["--validate-config"], frozen=False)

        config = self.source_directory / "config.toml"
        self.assertEqual(self.selected_config(arguments), config)
        self.assertEqual(config.read_bytes(), self.example)
        self.assertFalse((self.exe_directory / "config.toml").exists())
        prompt.assert_not_called()

    def test_first_config_is_copied_from_frozen_bundle(self):
        (self.source_directory / "config.example.toml").write_bytes(b"wrong source template")

        self.invoke(["--validate-config"])

        self.assertEqual((self.exe_directory / "config.toml").read_bytes(), self.example)

    def test_existing_config_is_never_overwritten_even_when_template_missing(self):
        config = self.exe_directory / "config.toml"
        existing = b"keep this personal configuration exactly"
        config.write_bytes(existing)
        (self.bundle / "config.example.toml").unlink()

        result, application, _, _, _ = self.invoke(["--once"])

        self.assertEqual(result, 0)
        application.assert_called_once()
        self.assertEqual(config.read_bytes(), existing)

    def test_both_explicit_config_forms_override_default_and_preserve_cwd(self):
        for inline in (False, True):
            with self.subTest(inline=inline):
                relative = "custom-equals.toml" if inline else "custom-separated.toml"
                options = [f"--config={relative}"] if inline else ["--config", relative]
                result, _, prompt, arguments, _ = self.invoke([*options, "--once"])
                expected = self.other_directory / relative
                self.assertEqual(result, 0)
                self.assertEqual(self.selected_config(arguments), expected)
                self.assertEqual(expected.read_bytes(), self.example)
                self.assertFalse((self.exe_directory / "config.toml").exists())
                prompt.assert_not_called()

    def test_no_argument_frozen_launch_prints_config_and_pauses_after_main(self):
        result, application, prompt, _, output = self.invoke()

        self.assertEqual(result, 0)
        application.assert_called_once()
        prompt.assert_called_once_with("按回车关闭…")
        self.assertIn(str(self.exe_directory / "config.toml"), output)
        self.assertIn("API_ID / API_HASH", output)
        self.assertIn("source", output)
        self.assertIn("target", output)

    def test_command_line_arguments_disable_pause_and_keep_main_exit_code(self):
        result, application, prompt, _, _ = self.invoke(["--once"], main_result=2)

        self.assertEqual(result, 2)
        application.assert_called_once()
        prompt.assert_not_called()

    def test_unexpected_error_returns_two_without_disclosing_exception_contents(self):
        secret = "api-hash-secret +8613800000000 OTP=12345"
        result, _, prompt, _, output = self.invoke(failure=RuntimeError(secret))

        self.assertEqual(result, 2)
        self.assertIn("RuntimeError", output)
        self.assertNotIn(secret, output)
        self.assertNotIn("12345", output)
        self.assertNotIn("Traceback", output)
        prompt.assert_called_once()

    def test_interrupt_or_eof_does_not_show_traceback(self):
        for failure in (KeyboardInterrupt(), EOFError()):
            with self.subTest(error=type(failure).__name__):
                result, _, prompt, _, output = self.invoke(failure=failure, pause_failure=EOFError())
                self.assertEqual(result, 2)
                self.assertIn("已停止", output)
                self.assertNotIn("Traceback", output)
                prompt.assert_called_once()

    def test_argparse_system_exit_is_converted_to_an_exit_code(self):
        result, application, prompt, _, _ = self.invoke(["--help"], failure=SystemExit(0))

        self.assertEqual(result, 0)
        application.assert_called_once()
        prompt.assert_not_called()

    def test_template_failure_is_friendly_and_does_not_run_main(self):
        (self.bundle / "config.example.toml").unlink()

        result, application, prompt, _, output = self.invoke()

        self.assertEqual(result, 2)
        application.assert_not_called()
        prompt.assert_called_once()
        self.assertIn("FileNotFoundError", output)
        self.assertNotIn("Traceback", output)

    def test_only_redirected_output_is_reconfigured_and_input_is_untouched(self):
        stdout = Mock()
        stdout.isatty.return_value = False
        stderr = Mock()
        stderr.isatty.return_value = True
        stdin = Mock()

        with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr), patch.object(sys, "stdin", stdin):
            launcher._configure_redirected_output()

        stdout.reconfigure.assert_called_once_with(encoding="utf-8")
        stderr.reconfigure.assert_not_called()
        stdin.reconfigure.assert_not_called()

    def test_redirected_stderr_is_utf8_but_console_stdout_is_preserved(self):
        stdout = Mock()
        stdout.isatty.return_value = True
        stderr = Mock()
        stderr.isatty.return_value = False

        with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            launcher._configure_redirected_output()

        stdout.reconfigure.assert_not_called()
        stderr.reconfigure.assert_called_once_with(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
