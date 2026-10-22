"""Shared phone/code normalization is international, explicit and secret-safe."""

import unittest

from tgmover.login_input import normalize_code, normalize_phone


class LoginInputTests(unittest.TestCase):
    def test_international_country_codes_are_never_rewritten(self):
        for value in ("+639171234567", "+12025550123", "+447700900123", "+79123456789", "+4915112345678", "+85251234567", "+8613800000000"):
            with self.subTest(prefix=value[:3]):
                self.assertTrue(normalize_phone(value) == value)

    def test_phone_spaces_parentheses_and_hyphens_are_accepted(self):
        self.assertTrue(normalize_phone(" +1 (202) 555-0123 ") == "+12025550123")
        self.assertTrue(normalize_phone("+63\u00a0917\u202f123\u20094567") == "+639171234567")
        self.assertTrue(normalize_phone("+7 (912) 345–67‑89") == "+79123456789")

    def test_explicit_00_prefix_is_converted_without_guessing_country(self):
        self.assertTrue(normalize_phone("00 63 (917) 123-4567") == "+639171234567")
        self.assertTrue(normalize_phone("0044 7700 900123") == "+447700900123")

    def test_nfkc_and_unicode_decimal_digits_become_ascii(self):
        self.assertTrue(normalize_phone("＋６３（９１７）１２３－４５６７") == "+639171234567")
        self.assertTrue(normalize_phone("+٦٣٩١٧١٢٣٤٥٦٧") == "+639171234567")

    def test_national_phone_numbers_require_an_explicit_country_prefix(self):
        for value in ("09171234567", "2025550123", "639171234567", "13800000000"):
            with self.subTest(length=len(value)):
                with self.assertRaisesRegex(ValueError, "手机号格式无效"):
                    normalize_phone(value)

    def test_phone_length_and_nonzero_calling_code_are_enforced(self):
        self.assertTrue(normalize_phone("+1234567") == "+1234567")
        self.assertTrue(normalize_phone("+123456789012345") == "+123456789012345")
        for value in ("+123456", "+1234567890123456", "+0123456789", "00012345678", "+", "00", ""):
            with self.subTest(length=len(value)):
                with self.assertRaises(ValueError):
                    normalize_phone(value)

    def test_phone_extensions_links_tokens_and_ambiguous_symbols_are_rejected(self):
        for value in ("+639171234567 ext12", "tel:+639171234567", "12345:bot-token", "++639171234567", "+63+9171234567", "+63/9171234567", "+63917.1234567", "+63917\u200b1234567"):
            with self.subTest(length=len(value)):
                with self.assertRaises(ValueError) as caught:
                    normalize_phone(value)
                self.assertFalse(value in str(caught.exception))

    def test_code_accepts_only_explicit_digits_with_whitespace_or_hyphens(self):
        for value in ("12345", " 12 345 ", "12-345", "12–345", "12‑345", "１２ ３４５", "١٢٣٤٥", "1\u202f2\u00a0345"):
            with self.subTest(length=len(value)):
                self.assertTrue(normalize_code(value) == "12345")

    def test_empty_or_separator_only_code_has_an_explicit_error(self):
        for value in ("", "  ", "---", "\t\n"):
            with self.assertRaisesRegex(ValueError, "请输入"):
                normalize_code(value)

    def test_code_does_not_extract_digits_from_untrusted_surrounding_text(self):
        for value in ("Your code is 12345", "验证码：12345", "+12345", "(12345)", "12/345", "1\u200b2345", "abc"):
            with self.subTest(length=len(value)):
                with self.assertRaisesRegex(ValueError, "格式无效") as caught:
                    normalize_code(value)
                self.assertFalse(value in str(caught.exception))

    def test_non_text_input_fails_with_value_error(self):
        for normalizer in (normalize_phone, normalize_code):
            for value in (None, 12345, [], {}):
                with self.assertRaises(ValueError):
                    normalizer(value)


if __name__ == "__main__":
    unittest.main()
