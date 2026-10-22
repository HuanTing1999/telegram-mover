"""No-network phone/code/optional-2FA tests with synthetic input values."""

from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from telethon import errors

from tgmover.auth import AuthError, login
from tgmover.config import ConfigError, load_config
from tgmover.gui_config import ConfigDraftError, write_draft


class FakeClient:
    def __init__(self, *, authorized=False, bot=False, sign_in_errors=()):
        self.authorized = authorized
        self.user = SimpleNamespace(id=123, bot=bot)
        self.sign_in_errors = list(sign_in_errors)
        self.connected = False
        self.requests = []
        self.sign_ins = []

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return self.authorized

    async def send_code_request(self, phone):
        self.requests.append(phone)
        return SimpleNamespace(phone_code_hash="server-code-hash")

    async def sign_in(self, **kwargs):
        self.sign_ins.append(kwargs)
        if self.sign_in_errors:
            raise self.sign_in_errors.pop(0)
        self.authorized = True
        return self.user

    async def get_me(self):
        return self.user if self.authorized else None


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_session_never_prompts(self):
        client = FakeClient(authorized=True)
        phone_input = Mock(side_effect=AssertionError("phone must not be requested"))
        code_input = Mock(side_effect=AssertionError("code must not be requested"))
        password_input = Mock(side_effect=AssertionError("password must not be requested"))

        user = await login(client, phone_input, code_input, password_fn=password_input)

        self.assertIs(user, client.user)
        self.assertTrue(client.connected)
        self.assertEqual(client.requests, [])
        self.assertEqual(client.sign_ins, [])
        phone_input.assert_not_called()
        code_input.assert_not_called()
        password_input.assert_not_called()

    async def test_phone_and_code_use_server_hash(self):
        client = FakeClient()
        phone_input = Mock(return_value=" +8613800000000 ")
        code_input = Mock(return_value=" 12345 ")
        password_input = Mock(side_effect=AssertionError("2FA must not be requested without server challenge"))

        user = await login(client, phone_input, code_input, password_fn=password_input)

        self.assertIs(user, client.user)
        self.assertTrue(client.connected)
        self.assertEqual(client.requests, ["+8613800000000"])
        self.assertEqual(client.sign_ins, [{
            "phone": "+8613800000000",
            "code": "12345",
            "phone_code_hash": "server-code-hash",
        }])
        password_input.assert_not_called()

    async def test_2fa_challenge_requests_password_once_without_resending_code(self):
        client = FakeClient(sign_in_errors=[errors.SessionPasswordNeededError(None)])
        code_input = Mock(return_value="12345")
        password_input = Mock(return_value="synthetic-fixture-value")

        with self.assertNoLogs("tgmover.auth", level="DEBUG"):
            user = await login(client, lambda _: "+8613800000000", code_input, password_fn=password_input)

        self.assertIs(user, client.user)
        code_input.assert_called_once()
        password_input.assert_called_once()
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.sign_ins[1], {"password": "synthetic-fixture-value"})

    async def test_2fa_preserves_leading_trailing_and_all_space_passwords(self):
        for raw in (" synthetic fixture ", " " * 3):
            with self.subTest(kind="spaces" if raw.isspace() else "padded"):
                client = FakeClient(sign_in_errors=[errors.SessionPasswordNeededError(None)])
                await login(client, lambda _: "+12025550123", lambda _: "12345", password_fn=lambda _: raw)
                self.assertEqual(client.sign_ins[-1], {"password": raw})
                self.assertTrue(client.authorized)

    async def test_empty_2fa_inputs_never_reach_rpc_and_stop_after_three(self):
        client = FakeClient(sign_in_errors=[errors.SessionPasswordNeededError(None)])
        password_input = Mock(return_value="")
        with self.assertRaisesRegex(AuthError, "3 次两步验证密码"):
            await login(client, lambda _: "+12025550123", lambda _: "12345", password_fn=password_input)
        self.assertEqual(password_input.call_count, 3)
        self.assertEqual(len(client.sign_ins), 1)
        self.assertEqual(len(client.requests), 1)
        self.assertFalse(client.authorized)

    async def test_2fa_hash_invalid_can_retry_then_succeed(self):
        client = FakeClient(sign_in_errors=[errors.SessionPasswordNeededError(None), errors.PasswordHashInvalidError(None)])
        code_input = Mock(return_value="12345")
        password_input = Mock(side_effect=["synthetic-incorrect", "synthetic-correct"])
        user = await login(client, lambda _: "+12025550123", code_input, password_fn=password_input)
        self.assertIs(user, client.user)
        self.assertEqual(password_input.call_count, 2)
        self.assertEqual(len(client.requests), 1)
        code_input.assert_called_once()
        self.assertEqual(client.sign_ins[-2:], [{"password": "synthetic-incorrect"}, {"password": "synthetic-correct"}])

    async def test_three_wrong_2fa_passwords_stop_without_secret_in_error(self):
        client = FakeClient(sign_in_errors=[errors.SessionPasswordNeededError(None)]
                            + [errors.PasswordHashInvalidError(None) for _ in range(3)])
        code_input = Mock(return_value="12345")
        password_input = Mock(return_value="synthetic-private-value")
        with self.assertRaisesRegex(AuthError, "3 次两步验证密码") as caught:
            await login(client, lambda _: "+12025550123", code_input, password_fn=password_input)
        self.assertNotIn("synthetic-private-value", str(caught.exception))
        self.assertEqual(password_input.call_count, 3)
        self.assertEqual(len(client.sign_ins), 4)
        self.assertEqual(len(client.requests), 1)
        code_input.assert_called_once()
        self.assertFalse(client.authorized)

    async def test_password_cannot_be_saved_in_runtime_config_or_gui_draft(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / "config.toml"
            path.write_text('[telegram]\npassword="synthetic-private-value"\n[sync]\nsource="@source"\ntarget="@target"\n', encoding="utf-8")
            with patch.dict("os.environ", {"API_ID": "12345", "API_HASH": "a" * 32}):
                with self.assertRaises(ConfigError) as caught:
                    load_config(path)
            self.assertNotIn("synthetic-private-value", str(caught.exception))
            path.write_text("original draft\n", encoding="utf-8")
            with self.assertRaises(ConfigDraftError):
                write_draft(path, {"telegram": {"password": "synthetic-private-value"}})
            self.assertEqual(path.read_text(encoding="utf-8"), "original draft\n")

    async def test_bot_session_is_rejected(self):
        client = FakeClient(authorized=True, bot=True)

        with self.assertRaisesRegex(AuthError, "机器人账号"):
            await login(client, Mock(), Mock())

        self.assertEqual(client.requests, [])

    async def test_invalid_phone_formats_are_rejected_before_request(self):
        for phone in ["639171234567", "+63 12", "12345:bot-token", "+63x9171234567", "", "+0123456789"]:
            with self.subTest(phone=phone):
                client = FakeClient()
                code_input = Mock()
                with self.assertRaisesRegex(AuthError, "手机号格式无效"):
                    await login(client, lambda _: phone, code_input)
                self.assertEqual(client.requests, [])
                code_input.assert_not_called()

    async def test_invalid_codes_are_limited_to_three_attempts(self):
        client = FakeClient(sign_in_errors=[errors.PhoneCodeInvalidError(None) for _ in range(3)])
        code_input = Mock(return_value="00000")

        with self.assertRaisesRegex(AuthError, "3 次"):
            await login(client, lambda _: "+8613800000000", code_input)

        self.assertEqual(code_input.call_count, 3)
        self.assertEqual(len(client.requests), 1)

    async def test_invalid_code_can_be_retried(self):
        client = FakeClient(sign_in_errors=[errors.PhoneCodeInvalidError(None)])
        code_input = Mock(side_effect=["00000", "12345"])

        await login(client, lambda _: "+8613800000000", code_input)

        self.assertTrue(client.authorized)
        self.assertEqual(code_input.call_count, 2)
        self.assertEqual(len(client.requests), 1)

    async def test_international_phone_formats_and_formatted_codes_are_normalized(self):
        for original, canonical in (
            ("+63 917-123-4567", "+639171234567"),
            ("+1 (202) 555-0123", "+12025550123"),
            ("0044 7700 900123", "+447700900123"),
            ("+7 (912) 345-67-89", "+79123456789"),
            ("+49 151 12345678", "+4915112345678"),
            ("+852 5123 4567", "+85251234567"),
        ):
            with self.subTest(country_prefix=canonical[:3]):
                client = FakeClient()
                await login(client, lambda _: original, lambda _: "１２ ３-４５")
                self.assertTrue(client.requests == [canonical])
                self.assertTrue(client.sign_ins[0]["phone"] == canonical)
                self.assertTrue(client.sign_ins[0]["code"] == "12345")

    async def test_malformed_codes_are_counted_locally_without_sending_or_resending(self):
        client = FakeClient()
        code_input = Mock(side_effect=["abcde", "12/34", "+12345"])
        with self.assertRaisesRegex(AuthError, "3 次"):
            await login(client, lambda _: "+639171234567", code_input)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.sign_ins, [])
        self.assertEqual(code_input.call_count, 3)

    async def test_expired_code_stops_without_resending(self):
        client = FakeClient(sign_in_errors=[errors.PhoneCodeExpiredError(None)])
        code_input = Mock(return_value="12345")

        with self.assertRaisesRegex(AuthError, "已过期"):
            await login(client, lambda _: "+8613800000000", code_input)

        self.assertEqual(len(client.requests), 1)
        self.assertEqual(code_input.call_count, 1)

    async def test_empty_codes_never_reach_sign_in_or_trigger_resends(self):
        client = FakeClient()
        code_input = Mock(return_value=" ")

        with self.assertRaisesRegex(AuthError, "3 次"):
            await login(client, lambda _: "+8613800000000", code_input)

        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.sign_ins, [])
        self.assertEqual(code_input.call_count, 3)


if __name__ == "__main__":
    unittest.main()
