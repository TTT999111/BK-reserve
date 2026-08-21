#!/usr/bin/env python3
"""
Lark 経費精算アラート bot

毎日1回起動されることを前提に、その日が配信日かどうかだけを判定して終了する
短時間プロセス。配信日でなければ何もせず正常終了する。

配信ルール:
- 毎月20日: 「経費処理を進めてください」のリマインド
- 毎月25日: 「本日締切」のアラート
  25日が土日祝（＋会社休業日）の場合は、その前の営業日に前倒しして配信する

リマインド日(20日)を前倒し/後ろ倒しするかは REMINDER_HOLIDAY_POLICY で切り替える
（既定は keep = 20日固定）。

祝日データ:
- 内閣府の「国民の祝日」CSV を取得できればそれを正とする（最新の法改正が反映される）
- 取得できない場合は組み込みの計算ロジックにフォールバックする（2007年〜2099年が対象）
"""

import argparse
import base64
import calendar
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import requests

JST = ZoneInfo("Asia/Tokyo")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
# 祝日CSVのローカルキャッシュ（GitHub Actions では logs/ ごとキャッシュされる）
HOLIDAY_CACHE_PATH = os.path.join(LOG_DIR, "syukujitsu.csv")
# 同日の二重配信を防ぐためのマーカー（--once-per-day 指定時のみ使用）
SENT_MARKER_PATH = os.path.join(LOG_DIR, "lark_expense_sent.json")

# 内閣府「国民の祝日について」の公開CSV（Shift_JIS）
HOLIDAY_CSV_URL = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv"

# 一時的とみなすHTTPステータス（リトライ対象）
TRANSIENT_STATUS = {429, 500, 502, 503, 504}

WEEKDAY_JA = ("月", "火", "水", "木", "金", "土", "日")

os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(
            os.path.join(LOG_DIR, f"lark_expense_{datetime.now(JST):%Y%m}.log")
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------- 設定


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# LARK_MENTION_ALL の値 → @全員 の方針。旧来の真偽値も受け付ける
MENTION_POLICIES = {
    "": "deadline",
    "deadline": "deadline",
    "both": "both",
    "true": "both",
    "1": "both",
    "yes": "both",
    "on": "both",
    "none": "none",
    "false": "none",
    "0": "none",
    "no": "none",
    "off": "none",
}


def parse_mention_policy(raw: Optional[str]) -> str:
    """@全員 メンションの方針を返す。既定は締切アラートのみ。"""
    key = (raw or "").strip().lower()
    if key in MENTION_POLICIES:
        return MENTION_POLICIES[key]
    log.warning("LARK_MENTION_ALL=%r は不正。deadline として扱う", raw)
    return "deadline"


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        log.warning("%s の値 %r が数値ではないため既定値 %d を使う", name, raw, default)
        return default


class Config:
    def __init__(self) -> None:
        self.webhook_url = os.environ.get("LARK_WEBHOOK_URL", "").strip()
        self.webhook_secret = os.environ.get("LARK_WEBHOOK_SECRET", "").strip()
        self.reminder_day = env_int("REMINDER_DAY", 20)
        self.deadline_day = env_int("DEADLINE_DAY", 25)
        # keep: 20日固定 / before: 前営業日 / after: 翌営業日
        self.reminder_policy = os.environ.get("REMINDER_HOLIDAY_POLICY", "keep").strip().lower()
        self.extra_holidays_raw = os.environ.get("EXTRA_HOLIDAYS", "")
        self.system_name = os.environ.get("EXPENSE_SYSTEM_NAME", "経費精算システム").strip()
        self.expense_url = os.environ.get("EXPENSE_URL", "").strip()
        # deadline(既定): 締切アラートのみ @全員 / both: 両方 / none: しない
        self.mention_policy = parse_mention_policy(os.environ.get("LARK_MENTION_ALL"))
        self.use_holiday_api = env_bool("USE_HOLIDAY_CSV", True)

        if self.reminder_policy not in ("keep", "before", "after"):
            log.warning(
                "REMINDER_HOLIDAY_POLICY=%r は不正。keep として扱う", self.reminder_policy
            )
            self.reminder_policy = "keep"
        for label, day in (("REMINDER_DAY", self.reminder_day), ("DEADLINE_DAY", self.deadline_day)):
            if not 1 <= day <= 31:
                raise SystemExit(f"{label}={day} が不正（1〜31で指定してください）")


# ---------------------------------------------------------------- 祝日


def _nth_monday(year: int, month: int, nth: int) -> date:
    """その月の第nth月曜日"""
    d = date(year, month, 1)
    offset = (0 - d.weekday()) % 7  # 最初の月曜まで
    return d + timedelta(days=offset + 7 * (nth - 1))


def _shunbun_day(year: int) -> int:
    """春分の日（1980〜2099年で有効な近似式）"""
    return int(20.8431 + 0.242194 * (year - 1980) - (year - 1980) // 4)


def _shubun_day(year: int) -> int:
    """秋分の日（1980〜2099年で有効な近似式）"""
    return int(23.2488 + 0.242194 * (year - 1980) - (year - 1980) // 4)


def _base_holidays(year: int) -> Dict[date, str]:
    """振替休日・国民の休日を除く「国民の祝日」本体（2007年以降の法令に準拠）"""
    h: Dict[date, str] = {
        date(year, 1, 1): "元日",
        _nth_monday(year, 1, 2): "成人の日",
        date(year, 2, 11): "建国記念の日",
        date(year, 3, _shunbun_day(year)): "春分の日",
        date(year, 4, 29): "昭和の日",
        date(year, 5, 3): "憲法記念日",
        date(year, 5, 4): "みどりの日",
        date(year, 5, 5): "こどもの日",
        date(year, 9, _shubun_day(year)): "秋分の日",
        date(year, 11, 3): "文化の日",
        date(year, 11, 23): "勤労感謝の日",
    }

    # 天皇誕生日（2019年は該当日なし、2020年から2月23日）
    if year >= 2020:
        h[date(year, 2, 23)] = "天皇誕生日"
    elif year <= 2018:
        h[date(year, 12, 23)] = "天皇誕生日"

    # 海の日 / 山の日 / 敬老の日 / スポーツの日（2020・2021年は五輪特例で移動）
    if year == 2020:
        h[date(2020, 7, 23)] = "海の日"
        h[date(2020, 7, 24)] = "スポーツの日"
        h[date(2020, 8, 10)] = "山の日"
    elif year == 2021:
        h[date(2021, 7, 22)] = "海の日"
        h[date(2021, 7, 23)] = "スポーツの日"
        h[date(2021, 8, 8)] = "山の日"
    else:
        h[_nth_monday(year, 7, 3)] = "海の日"
        h[_nth_monday(year, 10, 2)] = "スポーツの日" if year >= 2020 else "体育の日"
        if year >= 2016:
            h[date(year, 8, 11)] = "山の日"
    h[_nth_monday(year, 9, 3)] = "敬老の日"

    # 2019年の即位関連の特例
    if year == 2019:
        h[date(2019, 5, 1)] = "天皇の即位の日"
        h[date(2019, 10, 22)] = "即位礼正殿の儀の行われる日"

    return h


def calc_japanese_holidays(year: int) -> Dict[date, str]:
    """振替休日・国民の休日まで含めた、その年の祝日一覧を計算する。

    内閣府CSVが取れないときのフォールバック。1月・12月の振替が年をまたぐため
    前後の年もあわせて計算し、対象年だけを返す。
    """
    base: Dict[date, str] = {}
    for y in (year - 1, year, year + 1):
        base.update(_base_holidays(y))

    result = dict(base)

    # 振替休日: 日曜と重なった祝日は、その後の直近の「祝日でない日」が休日になる
    for d in sorted(base):
        if d.weekday() != 6:  # 日曜以外
            continue
        cand = d + timedelta(days=1)
        while cand in result:
            cand += timedelta(days=1)
        result[cand] = "振替休日"

    # 国民の休日: 前後を祝日に挟まれた平日（日曜・振替休日を除く）は休日になる
    for d in sorted(base):
        cand = d + timedelta(days=1)
        if cand in result or cand.weekday() == 6:
            continue
        if cand + timedelta(days=1) in base:
            result[cand] = "国民の休日"

    return {d: name for d, name in result.items() if d.year == year}


def fetch_holiday_csv(timeout: int = 15) -> Optional[Dict[date, str]]:
    """内閣府CSVから祝日一覧を取得する。取れなければローカルキャッシュを読む。"""
    body: Optional[bytes] = None
    try:
        resp = requests.get(HOLIDAY_CSV_URL, timeout=timeout)
        if resp.status_code == 200 and resp.content:
            body = resp.content
            try:
                with open(HOLIDAY_CACHE_PATH, "wb") as f:
                    f.write(body)
            except OSError as e:  # キャッシュ書き込み失敗は致命的ではない
                log.warning("祝日CSVのキャッシュ保存に失敗: %s", e)
        else:
            log.warning("祝日CSVの取得に失敗: status=%s", resp.status_code)
    except requests.RequestException as e:
        log.warning("祝日CSVの取得に失敗: %s", e)

    if body is None and os.path.exists(HOLIDAY_CACHE_PATH):
        log.info("祝日CSVのローカルキャッシュを使う: %s", HOLIDAY_CACHE_PATH)
        try:
            with open(HOLIDAY_CACHE_PATH, "rb") as f:
                body = f.read()
        except OSError as e:
            log.warning("祝日CSVのキャッシュ読み込みに失敗: %s", e)

    if not body:
        return None

    try:
        text = body.decode("cp932")
    except UnicodeDecodeError:
        text = body.decode("utf-8", errors="replace")

    holidays: Dict[date, str] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        try:
            y, m, d = (int(x) for x in row[0].strip().split("/"))
        except ValueError:
            continue  # ヘッダ行など
        holidays[date(y, m, d)] = row[1].strip()

    if not holidays:
        log.warning("祝日CSVを解釈できなかった")
        return None
    log.info("祝日CSVを読み込んだ: %d件 (%s〜%s)", len(holidays), min(holidays), max(holidays))
    return holidays


def parse_extra_holidays(raw: str, years: Iterable[int]) -> Dict[date, str]:
    """会社独自の休業日を解釈する。

    "2026-12-29" のような特定日と、"12-29" のような毎年の休業日の両方を受け付ける
    （カンマまたは空白区切り）。
    """
    result: Dict[date, str] = {}
    for token in raw.replace("\n", ",").replace(" ", ",").split(","):
        token = token.strip()
        if not token:
            continue
        parts = token.replace("/", "-").split("-")
        try:
            if len(parts) == 3:
                result[date(int(parts[0]), int(parts[1]), int(parts[2]))] = "会社休業日"
            elif len(parts) == 2:
                for y in years:
                    result[date(y, int(parts[0]), int(parts[1]))] = "会社休業日"
            else:
                raise ValueError(token)
        except ValueError:
            log.warning("EXTRA_HOLIDAYS の %r を解釈できないため無視する", token)
    return result


class HolidayCalendar:
    """土日＋祝日＋会社休業日をまとめて扱うカレンダー"""

    def __init__(self, years: Iterable[int], cfg: Config) -> None:
        years = sorted(set(years))
        self.holidays: Dict[date, str] = {}

        csv_holidays = fetch_holiday_csv() if cfg.use_holiday_api else None
        for y in years:
            self.holidays.update(calc_japanese_holidays(y))
        if csv_holidays:
            covered = {d.year for d in csv_holidays}
            # CSVが対象としている年は、CSVの内容を正とする
            for y in years:
                if y in covered:
                    self.holidays = {
                        d: n for d, n in self.holidays.items() if d.year != y
                    }
            self.holidays.update({d: n for d, n in csv_holidays.items() if d.year in years})
            self.source = "内閣府CSV"
        else:
            self.source = "組み込み計算"

        self.holidays.update(parse_extra_holidays(cfg.extra_holidays_raw, years))

    def holiday_name(self, d: date) -> Optional[str]:
        return self.holidays.get(d)

    def is_business_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def previous_business_day(self, d: date) -> date:
        cand = d
        for _ in range(60):
            cand -= timedelta(days=1)
            if self.is_business_day(cand):
                return cand
        raise RuntimeError(f"{d} の前営業日が見つからない")

    def next_business_day(self, d: date) -> date:
        cand = d
        for _ in range(60):
            cand += timedelta(days=1)
            if self.is_business_day(cand):
                return cand
        raise RuntimeError(f"{d} の翌営業日が見つからない")

    def business_days_between(self, start: date, end: date) -> int:
        """start〜end（両端含む）の営業日数"""
        if end < start:
            return 0
        count = 0
        cand = start
        while cand <= end:
            if self.is_business_day(cand):
                count += 1
            cand += timedelta(days=1)
        return count


# ---------------------------------------------------------------- 配信日の判定


def clamp_day(year: int, month: int, day: int) -> date:
    """月末を超える指定（2月30日など）はその月の末日に丸める"""
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last))


def deadline_date(year: int, month: int, cal: HolidayCalendar, cfg: Config) -> Tuple[date, date]:
    """(実際の締切配信日, 本来の締切日) を返す。休業日なら前営業日に前倒しする。"""
    nominal = clamp_day(year, month, cfg.deadline_day)
    actual = nominal if cal.is_business_day(nominal) else cal.previous_business_day(nominal)
    return actual, nominal


def reminder_date(year: int, month: int, cal: HolidayCalendar, cfg: Config) -> Tuple[date, date]:
    """(実際のリマインド配信日, 本来のリマインド日) を返す。"""
    nominal = clamp_day(year, month, cfg.reminder_day)
    if cfg.reminder_policy == "keep" or cal.is_business_day(nominal):
        return nominal, nominal
    if cfg.reminder_policy == "before":
        return cal.previous_business_day(nominal), nominal
    return cal.next_business_day(nominal), nominal


def fmt_date(d: date) -> str:
    return f"{d.month}月{d.day}日({WEEKDAY_JA[d.weekday()]})"


def off_day_reason(d: date, cal: "HolidayCalendar") -> str:
    """休業日である理由（祝日名 / 土日 / 会社休業日）を返す"""
    name = cal.holiday_name(d)
    if name:
        return name
    if d.weekday() >= 5:
        return f"{WEEKDAY_JA[d.weekday()]}曜日"
    return "休業日"


# ---------------------------------------------------------------- メッセージ


# @全員 メンションの記法は、カード内(lark_md)とテキストメッセージで異なる
MENTION_TAG = {
    "card": "<at id=all></at> ",
    "markdown": '<at user_id="all">全員</at> ',
}


def mention_prefix(kind: str, cfg: Config, mode: str) -> str:
    """その種別で @全員 を付けるなら、モードに応じたタグを返す"""
    on = cfg.mention_policy == "both" or (
        cfg.mention_policy == "deadline" and kind == "deadline"
    )
    return MENTION_TAG[mode] if on else ""


def build_message(
    kind: str, today: date, cal: HolidayCalendar, cfg: Config, mode: str = "card"
) -> Tuple[str, str, List[str], str]:
    """(タイトル, カードの色, 本文行, 注記) を組み立てる。card / markdown で共用する。"""
    deadline_actual, deadline_nominal = deadline_date(today.year, today.month, cal, cfg)
    moved = deadline_actual != deadline_nominal
    mention = mention_prefix(kind, cfg, mode)
    system = cfg.system_name

    if kind == "reminder":
        remaining = cal.business_days_between(today, deadline_actual)
        title = f"💴 {today.month}月分 経費精算のお願い"
        template = "orange"
        lines = [
            f"{mention}**今月の経費精算の締切は {fmt_date(deadline_actual)} です。**",
            f"締切まで残り **{remaining}営業日**（本日含む）です。",
            "",
            f"- 交通費・出張費・接待費などの領収書を{system}に登録してください",
            "- 承認まで完了して締切です。承認者の時間も見込んで早めにご申請ください",
            "- 締切を過ぎた分は翌月精算になります",
        ]
        if moved:
            lines.append(
                f"- ※ 本来の締切 {fmt_date(deadline_nominal)} が"
                f"{off_day_reason(deadline_nominal, cal)}のため前倒しの締切です"
            )
        note = "毎月20日 / 締切日に自動配信しています"
    else:
        title = f"⏰ 本日締切：{today.month}月分 経費精算"
        template = "red"
        lines = [
            f"{mention}**本日 {fmt_date(today)} は今月の経費精算の締切日です。**",
            "",
            f"- 未申請の方は本日中に{system}での申請を完了してください",
            "- すでに申請済みの方は、承認状況（差し戻しがないか）をご確認ください",
            "- 本日を過ぎた分は翌月精算になります",
        ]
        if moved:
            lines.append(
                f"- ※ 本来の締切 {fmt_date(deadline_nominal)} が"
                f"{off_day_reason(deadline_nominal, cal)}のため、本日が今月の締切です"
            )
        note = "毎月25日（休業日の場合は前営業日）に自動配信しています"

    return title, template, lines, note


def build_card(kind: str, today: date, cal: HolidayCalendar, cfg: Config) -> dict:
    title, template, lines, note = build_message(kind, today, cal, cfg, mode="card")

    elements: List[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
    ]
    if cfg.expense_url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": f"{cfg.system_name}を開く",
                        },
                        "url": cfg.expense_url,
                        "type": "primary",
                    }
                ],
            }
        )
    elements.append({"tag": "hr"})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note}]})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def render_markdown(kind: str, today: date, cal: HolidayCalendar, cfg: Config) -> str:
    """lark-cli の --markdown にそのまま渡せるテキストを組み立てる"""
    title, _, lines, note = build_message(kind, today, cal, cfg, mode="markdown")

    out = [f"**{title}**", ""] + lines
    if cfg.expense_url:
        out += ["", f"[{cfg.system_name}を開く]({cfg.expense_url})"]
    out += ["", note]
    return "\n".join(out)


# ---------------------------------------------------------------- 送信


def sign_payload(secret: str, timestamp: str) -> str:
    """Lark カスタムボットの署名検証用トークン"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def post_to_lark(card: dict, cfg: Config, retries: int = 3) -> bool:
    payload: Dict[str, object] = {"msg_type": "interactive", "card": card}
    if cfg.webhook_secret:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = sign_payload(cfg.webhook_secret, ts)

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(cfg.webhook_url, json=payload, timeout=15)
        except requests.RequestException as e:
            log.warning("送信失敗 (%d/%d): %s", attempt, retries, e)
            if attempt == retries:
                return False
            time.sleep(2**attempt)
            continue

        if resp.status_code in TRANSIENT_STATUS:
            log.warning("送信失敗 (%d/%d): status=%s", attempt, retries, resp.status_code)
            if attempt == retries:
                return False
            time.sleep(2**attempt)
            continue

        try:
            data = resp.json()
        except ValueError:
            log.error("応答を解釈できない: status=%s body=%s", resp.status_code, resp.text[:300])
            return False

        # Lark は成功時に code=0（旧形式では StatusCode=0）を返す
        code = data.get("code", data.get("StatusCode"))
        if resp.status_code == 200 and code in (0, None):
            log.info("送信成功: %s", data)
            return True

        log.error("送信エラー: status=%s body=%s", resp.status_code, data)
        return False

    return False


# ---------------------------------------------------------------- 二重配信ガード


def load_sent_markers() -> Dict[str, str]:
    if not os.path.exists(SENT_MARKER_PATH):
        return {}
    try:
        with open(SENT_MARKER_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as e:
        log.warning("配信済みマーカーを読めない: %s", e)
        return {}


def save_sent_marker(key: str) -> None:
    markers = load_sent_markers()
    markers[key] = datetime.now(JST).isoformat()
    # 直近90日分だけ残す
    cutoff = (datetime.now(JST) - timedelta(days=90)).isoformat()
    markers = {k: v for k, v in markers.items() if v >= cutoff}
    try:
        with open(SENT_MARKER_PATH, "w", encoding="utf-8") as f:
            json.dump(markers, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.warning("配信済みマーカーを保存できない: %s", e)


# ---------------------------------------------------------------- 実行


def decide_kind(today: date, cal: HolidayCalendar, cfg: Config) -> Optional[str]:
    """その日に配信すべき種別を返す。配信不要なら None。"""
    deadline_actual, _ = deadline_date(today.year, today.month, cal, cfg)
    reminder_actual, _ = reminder_date(today.year, today.month, cal, cfg)

    if today == deadline_actual:
        if today == reminder_actual:
            # 前倒しでリマインド日と締切日が重なったら、締切アラートだけを出す
            log.info("リマインド日と締切日が重なったため、締切アラートのみ配信する")
        return "deadline"
    if today == reminder_actual:
        return "reminder"
    return None


def print_calendar(year: int, cal: HolidayCalendar, cfg: Config) -> None:
    print(f"{year}年の配信予定（祝日データ: {cal.source} / リマインド方針: {cfg.reminder_policy}）")
    print(f"{'月':>3}  {'リマインド':<16}  {'締切アラート':<16}  備考")
    for m in range(1, 13):
        r_actual, r_nominal = reminder_date(year, m, cal, cfg)
        d_actual, d_nominal = deadline_date(year, m, cal, cfg)
        notes = []
        if r_actual != r_nominal:
            notes.append(f"リマインド{r_nominal.day}日→{r_actual.day}日")
        if d_actual != d_nominal:
            notes.append(
                f"締切{d_nominal.day}日({off_day_reason(d_nominal, cal)})→{d_actual.day}日"
            )
        print(
            f"{m:>3}月  {fmt_date(r_actual):<16}  {fmt_date(d_actual):<16}  {' / '.join(notes)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Lark 経費精算アラート bot")
    parser.add_argument("--date", help="判定日をYYYY-MM-DDで上書きする（テスト用）")
    parser.add_argument("--force", choices=["reminder", "deadline"], help="配信日でなくても指定種別を送る")
    parser.add_argument("--dry-run", action="store_true", help="送信せず内容だけ表示する")
    parser.add_argument(
        "--emit",
        choices=["card", "markdown"],
        default="card",
        help="--dry-run 時の出力形式。card=カードJSON / markdown=本文テキスト",
    )
    parser.add_argument("--calendar", type=int, metavar="YEAR", help="指定年の配信予定を表示して終了")
    parser.add_argument("--once-per-day", action="store_true", help="同じ日に二重配信しない")
    args = parser.parse_args()

    cfg = Config()
    dry_run = args.dry_run or env_bool("DRY_RUN", False)

    if args.date:
        try:
            today = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            raise SystemExit(f"--date の形式が不正: {args.date}")
    else:
        today = datetime.now(JST).date()

    if args.calendar:
        year = args.calendar
        cal = HolidayCalendar((year - 1, year, year + 1), cfg)
        print_calendar(year, cal, cfg)
        return 0

    cal = HolidayCalendar((today.year - 1, today.year, today.year + 1), cfg)
    kind = args.force or decide_kind(today, cal, cfg)

    if kind is None:
        d_actual, _ = deadline_date(today.year, today.month, cal, cfg)
        r_actual, _ = reminder_date(today.year, today.month, cal, cfg)
        log.info(
            "本日 %s は配信日ではない（今月: リマインド %s / 締切 %s）",
            today, r_actual, d_actual,
        )
        return 0

    marker_key = f"{today.isoformat()}:{kind}"
    if args.once_per_day and marker_key in load_sent_markers():
        log.info("本日分 (%s) は配信済みのためスキップする", marker_key)
        return 0

    card = build_card(kind, today, cal, cfg)
    log.info("配信対象: %s (%s)", kind, today)

    if dry_run:
        if args.emit == "markdown":
            print(render_markdown(kind, today, cal, cfg))
        else:
            print(
                json.dumps(
                    {"msg_type": "interactive", "card": card},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        log.info("DRY_RUN のため送信しない (--emit %s)", args.emit)
        return 0

    if not cfg.webhook_url:
        log.error("LARK_WEBHOOK_URL が設定されていない")
        return 1

    if not post_to_lark(card, cfg):
        return 1

    if args.once_per_day:
        save_sent_marker(marker_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
