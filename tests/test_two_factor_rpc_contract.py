"""Exercise real Telethon 1.45 password sign-in without network access."""

from __future__ import annotations

import asyncio
import logging
import unittest
from unittest.mock import patch

from telethon import errors, functions, password as pwd_mod, types
from telethon.client import auth as auth_module

from test_login_rpc_contract import InterceptedAuthClient, InterceptedSender


# Telegram's known 2048-bit safe prime, accepted by Telethon's SRP validation.
# Public protocol parameters and the passwords below are offline test fixtures.
GOOD_PRIME = bytes.fromhex(
    "c71caeb9c6b1c9048e6c522f70f13f73980d40238e3e21c14934d037563d930f"
    "48198a0aa7c14058229493d22530f4dbfa336f6e0ac925139543aed44cce7c37"
    "20fd51f69458705ac68cd4fe6b6b13abdc9746512969328454f18faf8c595f64"
    "2477fe96bb2a941d5bcd1d4ac8cc49880708fa9b378e3c4f3a9060bee67cf9a4"
    "a4a695811051907e162753b56b0f6b410dba74d8a84b2a14b3144e0ef1284754"
    "fd17ed950d5965b4b9dd46582db1178d169c6bc465b0d6ff9ca3928fef5b9ae4"
    "e418fc15e83ebea0f87fa9ff5eed70050ded2849f47bf959d956850ce929851f"
    "0d8115f635b105ee2e4e15d04b2454bf6f4fadf034b10403119cd8e3b92fcc5b"
)


def password_parameters(srp_id=123):
    algo = types.PasswordKdfAlgoSHA256SHA256PBKDF2HMACSHA512iter100000SHA256ModPow(
        salt1=b"offline-salt-one", salt2=b"offline-salt-two", g=3, p=GOOD_PRIME,
    )
    return types.account.Password(
        has_password=True, current_algo=algo,
        srp_B=(int.from_bytes(GOOD_PRIME, "big") // 2).to_bytes(256, "big"),
        srp_id=srp_id, hint="offline hint",
        new_algo=types.PasswordKdfAlgoUnknown(),
        new_secure_algo=types.SecurePasswordKdfAlgoUnknown(), secure_random=b"offline-random",
    )


class PasswordSender(InterceptedSender):
    def __init__(self, client, check_outcomes=()):
        super().__init__(client, ())
        self.check_outcomes = list(check_outcomes)
        self.parameters = []

    def send(self, request, ordered=False):
        while isinstance(request, functions.InvokeWithoutUpdatesRequest):
            request = request.query
        if not isinstance(request, (functions.account.GetPasswordRequest, functions.auth.CheckPasswordRequest)):
            return super().send(request, ordered=ordered)
        self.requests.append(request)
        self.requested.set()
        future = asyncio.get_running_loop().create_future()
        if isinstance(request, functions.account.GetPasswordRequest):
            params = password_parameters(123 + len(self.parameters))
            self.parameters.append(params)
            future.set_result(params)
        else:
            outcome = self.check_outcomes.pop(0) if self.check_outcomes else None
            if isinstance(outcome, asyncio.CancelledError):
                future.cancel()
            elif isinstance(outcome, BaseException):
                future.set_exception(outcome)
            else:
                future.set_result(types.auth.Authorization(user=self.client.user))
        return future


def password_client(check_outcomes=()):
    client = InterceptedAuthClient()
    client._sender = PasswordSender(client, check_outcomes)
    return client


class TwoFactorRpcContractTests(unittest.IsolatedAsyncioTestCase):
    def assert_only_password_auth(self, client, *, checks=1):
        requests = client._sender.requests
        self.assertEqual(sum(isinstance(item, functions.account.GetPasswordRequest) for item in requests), checks)
        self.assertEqual(sum(isinstance(item, functions.auth.CheckPasswordRequest) for item in requests), checks)
        forbidden = (
            functions.auth.SendCodeRequest, functions.auth.ResendCodeRequest,
            functions.auth.SignInRequest, functions.auth.ImportBotAuthorizationRequest,
        )
        self.assertFalse(any(isinstance(item, forbidden) for item in requests))

    async def test_password_branch_uses_srp_and_preserves_spaces_and_unicode(self):
        raw = "  密码 café\t🔒  "
        client = password_client()
        proof = types.InputCheckPasswordSRP(srp_id=123, A=b"a" * 256, M1=b"m" * 32)
        with patch.object(auth_module.pwd_mod, "compute_check", return_value=proof) as compute:
            user = await client.sign_in(password=raw)

        self.assertIs(user, client.user)
        self.assertTrue(client._authorized)
        compute.assert_called_once_with(client._sender.parameters[0], raw)
        checks = [item for item in client._sender.requests if isinstance(item, functions.auth.CheckPasswordRequest)]
        self.assertIs(checks[0].password, proof)
        self.assertIsInstance(checks[0].password, types.InputCheckPasswordSRP)
        self.assertNotIn(raw.encode("utf-8"), bytes(checks[0]))
        self.assert_only_password_auth(client)

    async def test_real_srp_computation_emits_serializable_proof_without_plaintext(self):
        raw = "  Unicode密码-🔐-café  "
        client = password_client()
        with patch.object(auth_module.pwd_mod, "compute_check", wraps=pwd_mod.compute_check) as compute:
            result = await client.sign_in(password=raw)

        self.assertEqual(result.id, client.user.id)
        compute.assert_called_once_with(client._sender.parameters[0], raw)
        check = next(item for item in client._sender.requests if isinstance(item, functions.auth.CheckPasswordRequest))
        self.assertEqual(check.password.srp_id, 123)
        self.assertEqual(len(check.password.A), 256)
        self.assertEqual(len(check.password.M1), 32)
        self.assertNotIn(raw, repr(check))
        self.assertNotIn(raw.encode("utf-8"), bytes(check))
        self.assert_only_password_auth(client)

    async def test_password_and_srp_errors_propagate_without_implicit_retry_or_code_request(self):
        for error_type in (errors.PasswordHashInvalidError, errors.SrpIdInvalidError,
                           errors.SrpPasswordChangedError):
            with self.subTest(error=error_type.__name__):
                client = password_client([error_type(None)])
                proof = types.InputCheckPasswordSRP(srp_id=123, A=b"a" * 256, M1=b"m" * 32)
                with patch.object(auth_module.pwd_mod, "compute_check", return_value=proof):
                    with self.assertRaises(error_type):
                        await client.sign_in(password="offline wrong password")
                self.assertFalse(client._authorized)
                self.assert_only_password_auth(client)

    async def test_explicit_retry_fetches_new_srp_parameters_without_repeating_code(self):
        client = password_client([errors.SrpIdInvalidError(None)])
        proofs = []

        def compute(params, raw):
            proof = types.InputCheckPasswordSRP(srp_id=params.srp_id, A=b"a" * 256, M1=b"m" * 32)
            proofs.append(proof)
            return proof

        with patch.object(auth_module.pwd_mod, "compute_check", side_effect=compute):
            with self.assertRaises(errors.SrpIdInvalidError):
                await client.sign_in(password="offline password")
            result = await client.sign_in(password="offline password")

        self.assertEqual(result.id, client.user.id)
        self.assertEqual([item.srp_id for item in proofs], [123, 124])
        checks = [item for item in client._sender.requests if isinstance(item, functions.auth.CheckPasswordRequest)]
        self.assertEqual([item.password.srp_id for item in checks], [123, 124])
        self.assert_only_password_auth(client, checks=2)

    async def test_empty_password_cannot_trigger_code_send_or_bot_authorization(self):
        client = password_client()
        with self.assertRaises(ValueError):
            await client.sign_in(password="")
        forbidden = (functions.account.GetPasswordRequest, functions.auth.CheckPasswordRequest,
                     functions.auth.SendCodeRequest, functions.auth.ResendCodeRequest,
                     functions.auth.ImportBotAuthorizationRequest)
        self.assertFalse(any(isinstance(item, forbidden) for item in client._sender.requests))

    async def test_cancelled_password_rpc_cannot_mark_user_authorized(self):
        client = password_client([asyncio.CancelledError()])
        proof = types.InputCheckPasswordSRP(srp_id=123, A=b"a" * 256, M1=b"m" * 32)
        with patch.object(auth_module.pwd_mod, "compute_check", return_value=proof):
            with self.assertRaises(asyncio.CancelledError):
                await client.sign_in(password="offline password")
        self.assertFalse(client._authorized)
        self.assert_only_password_auth(client)

    async def test_password_failure_does_not_log_plaintext_or_put_it_in_rpc_repr(self):
        raw = "  日志不应包含此测试密码\t🔏  "
        client = password_client([errors.PasswordHashInvalidError(None)])
        proof = types.InputCheckPasswordSRP(srp_id=123, A=b"a" * 256, M1=b"m" * 32)
        messages = []

        class Capture(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        logger = logging.getLogger("telethon")
        handler = Capture()
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            with patch.object(auth_module.pwd_mod, "compute_check", return_value=proof):
                with self.assertRaises(errors.PasswordHashInvalidError) as caught:
                    await client.sign_in(password=raw)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

        rendered = "\n".join(messages + [str(caught.exception)] + [repr(item) for item in client._sender.requests])
        self.assertNotIn(raw, rendered)
        self.assertNotIn(raw.strip(), rendered)
        self.assert_only_password_auth(client)


if __name__ == "__main__":
    unittest.main()
