"""Real Telegram TL objects, local ad rules, Unicode links, and bounded regex."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from telethon import types, utils

from tgmover.config import ConfigError, load_config
from tgmover.filters import AdFilter, FilterConfigError, FilterSettings, validate_filter_settings


def message(identifier=1, text="ordinary text", *, peer=123, entities=None, buttons=None, filename=None, group=None):
    media = None
    if filename:
        media = types.MessageMediaDocument(document=types.Document(
            id=identifier * 10, access_hash=99, file_reference=b"reference",
            date=datetime.now(timezone.utc), mime_type="application/octet-stream", size=4,
            dc_id=1, attributes=[types.DocumentAttributeFilename(file_name=filename)],
        ))
    return types.Message(
        id=identifier, peer_id=types.PeerChannel(peer), message=text,
        date=datetime.now(timezone.utc), entities=entities, grouped_id=group, media=media,
        reply_markup=types.ReplyInlineMarkup(rows=[types.KeyboardInlineButtonRow(buttons=buttons)]) if buttons else None,
    )


class AdFilterTests(unittest.TestCase):
    def filtered(self, batch, **settings):
        return AdFilter(replace(FilterSettings(), **settings)).evaluate(batch)

    def test_defaults_use_high_confidence_keywords_and_preserve_ordinary_mentions(self):
        self.assertFalse(self.filtered([message(text="故事里提到了广告，但没有推广合作")]).blocked)
        for text in ("商务合作请联系", "广\u200b 告 合\n作", "ＳＰＯＮＳＯＲＥＤ　ＰＯＳＴ", "Paid Promotion"):
            with self.subTest(text=text):
                result = self.filtered([message(text=text)])
                self.assertTrue(result.blocked)
                self.assertEqual(result.matched_ids, (1,))
                self.assertTrue(any("关键词" in reason for reason in result.reasons))

    def test_caption_and_filename_are_detected_without_reading_media(self):
        result = self.filtered([message(text="ordinary caption", filename="sponsored post.pdf")])
        self.assertTrue(result.blocked)
        self.assertFalse(self.filtered([message(text="ordinary caption", filename="annual_report.pdf")]).blocked)

    def test_album_any_member_blocks_whole_group_in_source_order(self):
        batch = [message(1, group=99), message(2, text="付费推广", group=99), message(3, group=99)]
        result = self.filtered(batch)
        self.assertTrue(result.blocked)
        self.assertEqual(result.matched_ids, (2,))
        self.assertEqual([item.id for item in batch], [1, 2, 3])

    def test_hidden_text_url_target_and_utf16_url_entities_are_detected(self):
        hidden = message(text="click here", entities=[types.MessageEntityTextUrl(0, 5, "https://ads.example/deal")])
        self.assertTrue(self.filtered([hidden], keywords=(), blocked_domains=("ads.example",)).blocked)
        visible = message(text="😀 ads.example/path", entities=[types.MessageEntityUrl(3, len("ads.example/path"))])
        result = self.filtered([visible], keywords=(), blocked_domains=("ads.example",))
        self.assertTrue(result.blocked)
        self.assertIn("域名黑名单：ads.example", result.reasons)

    def test_url_button_target_button_text_and_optional_all_buttons(self):
        linked = message(buttons=[types.KeyboardInlineButton("ordinary button", types.InlineButtonTypeUrl("https://ads.example/"))])
        self.assertTrue(self.filtered([linked], keywords=(), blocked_domains=("ads.example",)).blocked)
        named = message(buttons=[types.KeyboardInlineButton("商务合作", types.InlineButtonTypeUrl("https://safe.example/"))])
        self.assertTrue(self.filtered([named]).blocked)
        plain = message(buttons=[types.KeyboardInlineButton("Read next", types.InlineButtonTypeCallback(data=b"next"))])
        self.assertFalse(self.filtered([plain]).blocked)
        self.assertTrue(self.filtered([plain], block_buttons=True).blocked)

    def test_domain_boundaries_subdomains_and_userinfo_use_actual_host(self):
        settings = dict(keywords=(), blocked_domains=("ads.example",))
        for url in ("https://ads.example/x", "https://sub.ads.example/x", "www.ads.example/x",
                    "https://safe.example@ads.example/x", "https://%61ds.example/x"):
            with self.subTest(url=url):
                self.assertTrue(self.filtered([message(text=url)], **settings).blocked)
        for url in ("https://notads.example/x", "https://ads.example.safe.example/x",
                    "https://ads.example@safe.example/x", "https://safe.example/?next=ads.example"):
            with self.subTest(url=url):
                self.assertFalse(self.filtered([message(text=url)], **settings).blocked)

    def test_idna_normalization_and_bare_telegram_links(self):
        unicode_host = "例子.测试"
        ascii_host = unicode_host.encode("idna").decode("ascii")
        self.assertTrue(self.filtered([message(text=f"https://{unicode_host}/")],
                                      keywords=(), blocked_domains=(ascii_host,)).blocked)
        self.assertTrue(self.filtered([message(text=f"https://sub.{ascii_host}/")],
                                      keywords=(), blocked_domains=(unicode_host,)).blocked)
        self.assertTrue(self.filtered([message(text="join t.me/example_channel")],
                                      keywords=(), blocked_domains=("t.me",)).blocked)
        self.assertFalse(self.filtered([message(text="not.me/example_channel")],
                                       keywords=(), blocked_domains=("t.me",)).blocked)

    def test_keyword_whitelist_explicitly_releases_whole_album(self):
        first = message(1, text="广告合作", group=77)
        second = message(2, text="verified editorial", group=77)
        result = self.filtered([first, second], allowed_keywords=("verified editorial",))
        self.assertFalse(result.blocked)
        self.assertEqual(result.matched_ids, ())
        self.assertIn("整组放行", result.reasons[0])

    def test_manual_block_takes_priority_over_keyword_whitelist(self):
        item = message(5, text="verified editorial")
        key = f"{utils.get_peer_id(item.peer_id)}:{item.id}"
        result = self.filtered([item], allowed_keywords=("verified editorial",), blocked_message_keys=(key,))
        self.assertTrue(result.blocked)
        self.assertEqual(result.matched_ids, (5,))
        self.assertIn("手动屏蔽", result.reasons[0])

    def test_allowed_domain_only_exempts_all_links_not_keywords_or_blacklist(self):
        settings = dict(block_all_links=True, allowed_domains=("safe.example",))
        self.assertFalse(self.filtered([message(text="https://sub.safe.example/path")], **settings).blocked)
        self.assertTrue(self.filtered([message(text="https://outside.example/path")], **settings).blocked)
        self.assertTrue(self.filtered([message(text="商务合作 https://safe.example/path")], **settings).blocked)
        self.assertTrue(self.filtered([message(text="https://safe.example/path")],
                                      blocked_domains=("safe.example",), **settings).blocked)

    def test_manual_message_keys_are_scoped_to_signed_chat_id(self):
        item = message(5, peer=123)
        key = f"{utils.get_peer_id(item.peer_id)}:{item.id}"
        self.assertTrue(self.filtered([item], blocked_message_keys=(key,)).blocked)
        self.assertFalse(self.filtered([message(5, peer=124)], blocked_message_keys=(key,)).blocked)

    def test_disabled_comments_and_download_only_can_be_explicitly_exempted(self):
        item = message(text="广告合作")
        self.assertFalse(AdFilter(FilterSettings(enabled=False)).evaluate([item]).blocked)
        filter_ = AdFilter(FilterSettings(apply_to_comments=False))
        self.assertFalse(filter_.evaluate([item], scope="comment").blocked)
        self.assertTrue(filter_.evaluate([item], scope="post").blocked)
        self.assertFalse(filter_.evaluate([item], mode="download_only").blocked)
        self.assertTrue(AdFilter(FilterSettings(apply_to_downloads=True)).evaluate([item], mode="download_only").blocked)
        self.assertTrue(filter_.evaluate([item], mode="download_then_forward").blocked)

    def test_regex_matches_natural_or_compact_normalized_text(self):
        self.assertTrue(self.filtered([message(text="PROMO   CODE 123")], keywords=(),
                                      regex_patterns=(r"promo\s+code\s+\d+",)).blocked)
        self.assertTrue(self.filtered([message(text="特 别\u200b优 惠123")], keywords=(),
                                      regex_patterns=(r"特别优惠\d+",)).blocked)

    def test_catastrophic_regex_times_out_and_blocks_with_explanation(self):
        filter_ = AdFilter(FilterSettings(keywords=(), regex_patterns=(r"(a|aa)+$",)))
        started = time.monotonic()
        result = filter_.evaluate([message(text="a" * 10000 + "!")])
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(result.blocked)
        self.assertTrue(any("0.05" in reason and "保守拦截" in reason for reason in result.reasons))

    def test_direct_settings_are_strict_and_compile_patterns_before_use(self):
        for setting in (
            FilterSettings(keywords=("",)), FilterSettings(keywords=("\u200b",)),
            FilterSettings(regex_patterns=("(",)), FilterSettings(regex_patterns=("x" * 1025,)),
            FilterSettings(regex_patterns=("x",) * 31), FilterSettings(keywords=("x",) * 201),
            FilterSettings(blocked_domains=("https://ads.example",)),
            FilterSettings(blocked_domains=("user@ads.example",)),
            FilterSettings(blocked_domains=("*.ads.example",)),
            FilterSettings(blocked_domains=("ads..example",)),
            FilterSettings(blocked_domains=("ads.example..",)),
            FilterSettings(blocked_message_keys=("123",)), FilterSettings(blocked_message_keys=("-0:1",)),
            FilterSettings(blocked_message_keys=("-100123:0",)), FilterSettings(enabled=1),
        ):
            with self.subTest(setting=setting), self.assertRaises(FilterConfigError):
                validate_filter_settings(setting)


class FilterConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.toml"

    def write(self, values=""):
        self.path.write_text(
            '[telegram]\napi_id=12345\napi_hash="' + "a" * 32 + '"\n'
            '[sync]\nsource="@source"\ntarget="@target"\n' + values, encoding="utf-8")

    def load(self):
        with patch.dict(os.environ, {"API_ID": "12345", "API_HASH": "a" * 32}):
            return load_config(self.path)

    def test_defaults_and_complete_valid_filters_section(self):
        self.write()
        self.assertEqual(self.load().filters, FilterSettings())
        self.write(r'''[filters]
enabled=true
keywords=["custom promotion"]
regex_patterns=['promo\s+code']
blocked_domains=["例子.测试"]
allowed_keywords=["editorial"]
allowed_domains=["safe.example"]
block_all_links=true
block_buttons=true
apply_to_comments=false
apply_to_downloads=true
blocked_message_keys=["-1001234567890:42"]
''')
        filters = self.load().filters
        self.assertEqual(filters.keywords, ("custom promotion",))
        self.assertEqual(filters.blocked_domains, ("例子.测试".encode("idna").decode("ascii"),))
        self.assertEqual(filters.blocked_message_keys, ("-1001234567890:42",))
        self.assertTrue(filters.block_buttons)
        self.assertFalse(filters.apply_to_comments)

    def test_invalid_filter_types_unknown_fields_and_invalid_rules_are_config_errors(self):
        for setting in (
            'enabled=1', 'keywords="spam"', 'keywords=[42]', 'keywords=[""]',
            'regex_patterns=["("]', 'blocked_domains=["https://ads.example/path"]',
            'blocked_domains=["ads.example:443"]', 'blocked_message_keys=["42"]',
            'blocked_message_keys=["-100123:2147483648"]', 'unknown_rule=true',
            'allowed_domains=[false]', 'block_all_links="yes"',
        ):
            with self.subTest(setting=setting):
                self.write(f"[filters]\n{setting}\n")
                with self.assertRaises(ConfigError):
                    self.load()

    def test_account_only_allows_unconfigured_peers_but_preserves_other_validation(self):
        self.path.write_text('[sync]\nsource=""\ntarget=""\n', encoding="utf-8")
        with patch.dict(os.environ, {"API_ID": "12345", "API_HASH": "a" * 32}):
            with self.assertRaises(ConfigError):
                load_config(self.path)
            account = load_config(self.path, account_only=True)
            self.assertEqual((account.source, account.target), ("@source_channel", "@target_channel"))
            self.path.write_text("[sync]\n[filters]\nregex_patterns=['(']\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(self.path, account_only=True)
            self.path.write_text('[sync]\nmode="invalid"\n', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(self.path, account_only=True)
            self.path.write_text("[sync]\n", encoding="utf-8")
            self.assertEqual(load_config(self.path, account_only=True).source, "@source_channel")
            with patch.dict(os.environ, {"API_ID": ""}):
                with self.assertRaises(ConfigError):
                    load_config(self.path, account_only=True)


if __name__ == "__main__":
    unittest.main()
