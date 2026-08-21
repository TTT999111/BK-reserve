#!/usr/bin/env python3
"""lark_expense_alert.py のテスト（依存なしで `python3 test_lark_expense_alert.py` で実行可）"""

import os
import sys
import unittest
from datetime import date

# テスト中はネットワークを見に行かせない（組み込み計算だけで検証する）
os.environ["USE_HOLIDAY_CSV"] = "false"

import lark_expense_alert as bot  # noqa: E402


def make_cal(env=None):
    old = dict(os.environ)
    os.environ.update(env or {})
    try:
        cfg = bot.Config()
        cal = bot.HolidayCalendar((2025, 2026, 2027), cfg)
        return cal, cfg
    finally:
        os.environ.clear()
        os.environ.update(old)


class HolidayTest(unittest.TestCase):
    def test_2026_holidays(self):
        h = bot.calc_japanese_holidays(2026)
        self.assertEqual(h[date(2026, 1, 12)], "成人の日")
        self.assertEqual(h[date(2026, 3, 20)], "春分の日")
        self.assertEqual(h[date(2026, 5, 6)], "振替休日")  # 5/3(日)の振替
        self.assertEqual(h[date(2026, 9, 22)], "国民の休日")  # 敬老の日と秋分の日に挟まれる
        self.assertEqual(h[date(2026, 9, 23)], "秋分の日")
        self.assertNotIn(date(2026, 6, 1), h)  # 6月に祝日はない

    def test_business_day(self):
        cal, _ = make_cal()
        self.assertTrue(cal.is_business_day(date(2026, 8, 25)))  # 火
        self.assertFalse(cal.is_business_day(date(2026, 8, 22)))  # 土
        self.assertFalse(cal.is_business_day(date(2026, 8, 11)))  # 山の日
        self.assertEqual(cal.previous_business_day(date(2026, 1, 25)), date(2026, 1, 23))
        self.assertEqual(cal.next_business_day(date(2026, 5, 2)), date(2026, 5, 7))  # GW跨ぎ

    def test_extra_holidays(self):
        cal, _ = make_cal({"EXTRA_HOLIDAYS": "12-29,12-30,2026-08-14"})
        self.assertFalse(cal.is_business_day(date(2026, 12, 29)))
        self.assertFalse(cal.is_business_day(date(2027, 12, 29)))  # 毎年指定
        self.assertFalse(cal.is_business_day(date(2026, 8, 14)))
        self.assertTrue(cal.is_business_day(date(2027, 8, 13)))  # 年指定は当年のみ


class ScheduleTest(unittest.TestCase):
    def test_deadline_not_moved_on_business_day(self):
        cal, cfg = make_cal()
        actual, nominal = bot.deadline_date(2026, 8, cal, cfg)
        self.assertEqual(actual, date(2026, 8, 25))
        self.assertEqual(actual, nominal)

    def test_deadline_moved_from_sunday(self):
        cal, cfg = make_cal()
        # 2026-01-25(日) → 24(土) も休みなので 23(金)
        actual, nominal = bot.deadline_date(2026, 1, cal, cfg)
        self.assertEqual(nominal, date(2026, 1, 25))
        self.assertEqual(actual, date(2026, 1, 23))

    def test_deadline_moved_from_saturday(self):
        cal, cfg = make_cal()
        actual, _ = bot.deadline_date(2026, 4, cal, cfg)  # 4/25(土) → 4/24(金)
        self.assertEqual(actual, date(2026, 4, 24))

    def test_deadline_moved_from_public_holiday(self):
        cal, cfg = make_cal({"EXTRA_HOLIDAYS": "2026-12-25"})
        actual, _ = bot.deadline_date(2026, 12, cal, cfg)  # 12/25(金)休 → 12/24(木)
        self.assertEqual(actual, date(2026, 12, 24))

    def test_reminder_policy(self):
        # 2026-09-20 は日曜
        cal, cfg = make_cal()
        self.assertEqual(bot.reminder_date(2026, 9, cal, cfg)[0], date(2026, 9, 20))
        cal, cfg = make_cal({"REMINDER_HOLIDAY_POLICY": "before"})
        self.assertEqual(bot.reminder_date(2026, 9, cal, cfg)[0], date(2026, 9, 18))
        cal, cfg = make_cal({"REMINDER_HOLIDAY_POLICY": "after"})
        # 9/21(敬老) 9/22(国民の休日) 9/23(秋分) を飛ばして 9/24(木)
        self.assertEqual(bot.reminder_date(2026, 9, cal, cfg)[0], date(2026, 9, 24))

    def test_decide_kind(self):
        cal, cfg = make_cal()
        self.assertEqual(bot.decide_kind(date(2026, 8, 20), cal, cfg), "reminder")
        self.assertEqual(bot.decide_kind(date(2026, 8, 25), cal, cfg), "deadline")
        self.assertEqual(bot.decide_kind(date(2026, 1, 23), cal, cfg), "deadline")
        self.assertIsNone(bot.decide_kind(date(2026, 1, 25), cal, cfg))  # 本来の締切日は配信しない
        self.assertIsNone(bot.decide_kind(date(2026, 8, 21), cal, cfg))

    def test_deadline_wins_when_days_collide(self):
        # 20日と締切が同日になった場合は締切アラートのみ
        cal, cfg = make_cal({"DEADLINE_DAY": "21", "EXTRA_HOLIDAYS": "2026-08-21"})
        self.assertEqual(bot.decide_kind(date(2026, 8, 20), cal, cfg), "deadline")

    def test_month_end_clamp(self):
        cal, cfg = make_cal({"DEADLINE_DAY": "31"})
        # 2026-02-31 は存在しないので 2/28(土) → 2/27(金)
        actual, nominal = bot.deadline_date(2026, 2, cal, cfg)
        self.assertEqual(nominal, date(2026, 2, 28))
        self.assertEqual(actual, date(2026, 2, 27))

    def test_business_days_between(self):
        cal, _ = make_cal()
        # 8/20(木) 21(金) 24(月) 25(火)
        self.assertEqual(cal.business_days_between(date(2026, 8, 20), date(2026, 8, 25)), 4)
        self.assertEqual(cal.business_days_between(date(2026, 8, 26), date(2026, 8, 25)), 0)


class MessageTest(unittest.TestCase):
    def test_reminder_card(self):
        cal, cfg = make_cal({"EXPENSE_URL": "https://example.com/expense"})
        card = bot.build_card("reminder", date(2026, 8, 20), cal, cfg)
        body = card["elements"][0]["text"]["content"]
        self.assertIn("8月25日(火)", body)
        self.assertIn("4営業日", body)
        self.assertEqual(card["header"]["template"], "orange")
        self.assertEqual(card["elements"][1]["actions"][0]["url"], "https://example.com/expense")

    def test_deadline_card_mentions_shift(self):
        cal, cfg = make_cal()
        card = bot.build_card("deadline", date(2026, 1, 23), cal, cfg)
        body = card["elements"][0]["text"]["content"]
        self.assertIn("本日 1月23日(金)", body)
        self.assertIn("1月25日(日)", body)
        self.assertEqual(card["header"]["template"], "red")

    def _mentions(self, env=None):
        """(リマインドに@全員が入るか, 締切に@全員が入るか)"""
        cal, cfg = make_cal(env)
        return tuple(
            "<at id=all></at>" in bot.build_card(kind, day, cal, cfg)["elements"][0]["text"]["content"]
            for kind, day in (("reminder", date(2026, 8, 20)), ("deadline", date(2026, 8, 25)))
        )

    def test_mention_default_is_deadline_only(self):
        self.assertEqual(self._mentions(), (False, True))

    def test_mention_both(self):
        self.assertEqual(self._mentions({"LARK_MENTION_ALL": "both"}), (True, True))

    def test_mention_none(self):
        self.assertEqual(self._mentions({"LARK_MENTION_ALL": "none"}), (False, False))

    def test_mention_legacy_boolean(self):
        # 旧来の true/false も受け付ける
        self.assertEqual(self._mentions({"LARK_MENTION_ALL": "true"}), (True, True))
        self.assertEqual(self._mentions({"LARK_MENTION_ALL": "false"}), (False, False))

    def test_mention_invalid_falls_back_to_deadline(self):
        self.assertEqual(self._mentions({"LARK_MENTION_ALL": "yes-please"}), (False, True))

    def test_markdown_matches_card_content(self):
        cal, cfg = make_cal()
        md = bot.render_markdown("deadline", date(2026, 1, 23), cal, cfg)
        card_body = bot.build_card("deadline", date(2026, 1, 23), cal, cfg)["elements"][0][
            "text"
        ]["content"]
        self.assertIn("⏰ 本日締切：1月分 経費精算", md)  # タイトルが本文に含まれる
        self.assertIn("本日 1月23日(金)", md)
        self.assertIn("1月25日(日)", md)  # 前倒しの理由
        self.assertIn("毎月25日", md)  # 注記
        # 本文の箇条書きはカードと同一
        for line in card_body.splitlines():
            if line.startswith("- "):
                self.assertIn(line, md)

    def test_markdown_mention_syntax(self):
        # markdown はテキストメッセージ用の記法を使い、カード用の記法は混ぜない
        cal, cfg = make_cal()
        deadline = bot.render_markdown("deadline", date(2026, 8, 25), cal, cfg)
        reminder = bot.render_markdown("reminder", date(2026, 8, 20), cal, cfg)
        self.assertIn('<at user_id="all">全員</at>', deadline)
        self.assertNotIn("<at id=all>", deadline)
        self.assertNotIn("<at", reminder)  # 既定ではリマインドに@全員は付かない

    def test_markdown_mention_policy(self):
        for policy, expected in (("both", (True, True)), ("none", (False, False))):
            cal, cfg = make_cal({"LARK_MENTION_ALL": policy})
            got = (
                '<at user_id="all">全員</at>'
                in bot.render_markdown("reminder", date(2026, 8, 20), cal, cfg),
                '<at user_id="all">全員</at>'
                in bot.render_markdown("deadline", date(2026, 8, 25), cal, cfg),
            )
            self.assertEqual(got, expected, policy)

    def test_markdown_expense_url_link(self):
        cal, cfg = make_cal({"EXPENSE_URL": "https://example.com/e"})
        md = bot.render_markdown("deadline", date(2026, 8, 25), cal, cfg)
        self.assertIn("[経費精算システムを開く](https://example.com/e)", md)

    def test_sign(self):
        # Lark の署名仕様: HMAC-SHA256(key="{timestamp}\n{secret}", msg="") を base64
        import base64
        import hashlib
        import hmac

        expected = base64.b64encode(
            hmac.new("1700000000\ns3cr3t".encode("utf-8"), b"", hashlib.sha256).digest()
        ).decode()
        self.assertEqual(bot.sign_payload("s3cr3t", "1700000000"), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
