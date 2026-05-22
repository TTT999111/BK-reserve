#!/usr/bin/env python3
"""
docomo bike share 自動予約（毎分起動版）

launchd から毎分起動されることを前提に、1回の実行は数秒で終わる短時間プロセス。
状態はAPI側にあるためファイル保存不要。App Nap / スリープの影響を受けない。

動作:
- 平日 7:00 〜 9:00 の間のみ動作（時間外なら即終了）
- 現在の予約状況をAPIで確認
- 予約なし → 予約する
- 予約あり → 残り時間が3分以下ならキャンセル → 即再予約
- 8:55 以降は予約を残して終了
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")

BASE_URL = "https://csapi.docomo-cycle.jp/v12/api/restful"

# 残り時間がこの秒数以下になったらキャンセル→再予約
REFRESH_THRESHOLD_SEC = 180  # 3分
# 終了時刻の何分前から新規予約を行わないか（最後の予約を残す）
FINAL_HOLD_MINUTES = 5

os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, f"tick_{datetime.now():%Y%m%d}.log")),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


class DocomoBikeClient:
    def __init__(self, session_id: str, api_key: str):
        self.session = requests.Session()
        self.session.headers.update({
            "content-type": "application/json;charset=UTF-8",
            "x-api-key": api_key,
            "x-bks-sessionid": session_id,
            "user-agent": "bike%20share/25080601 CFNetwork/3860.500.112 Darwin/25.4.0",
            "accept": "*/*",
        })

    def _request(self, method: str, path: str, **kwargs):
        url = f"{BASE_URL}/{path}"
        for attempt in range(3):
            try:
                resp = self.session.request(method, url, timeout=10, **kwargs)
                return resp.json()
            except (requests.ConnectionError, requests.Timeout) as e:
                log.warning("通信エラー (試行%d/3): %s", attempt + 1, e)
            except Exception as e:
                log.error("予期しないエラー: %s", e)
        return {"result": -1}

    def get_status(self) -> dict:
        """予約状況を取得。result200, user_status (0=予約なし, 1=予約中), cyc_name, reserve_limitなど"""
        return self._request("GET", "reservecyclestatus")

    def get_available_bikes(self, park_id: str) -> list:
        data = self._request("GET", f"parkcycleinfo/{park_id}/100/1")
        if data.get("result") != 200:
            return []
        return data.get("cycle_info", [])

    def reserve_bike(self, cyc_name: str) -> Optional[dict]:
        data = self._request("POST", "reservecycle", json={"cyc_name": cyc_name})
        if data.get("result") != 200:
            log.error("予約失敗 (%s): %s", cyc_name, data)
            return None
        log.info("予約成功: %s (期限: %s)", cyc_name, data.get("reserve_limit"))
        return data

    def cancel_reservation(self):
        data = self._request("DELETE", "reservecycle")
        result = data.get("result")
        if result == 200:
            log.info("予約キャンセル成功")
            return True
        if result == 302:
            log.warning("キャンセル対象の予約なし(302)")
            return "expired"
        log.error("キャンセル失敗: %s", data)
        return False


def select_best_bike(bikes: list, min_battery: int = 2, prefer_csa: int = 1) -> Optional[str]:
    candidates = [b for b in bikes if b["battery_level"] >= min_battery]
    if not candidates:
        candidates = [b for b in bikes if b["battery_level"] > 0]
    if not candidates:
        return None
    candidates.sort(key=lambda b: (b["csa_type"] == prefer_csa, b["battery_level"]), reverse=True)
    return candidates[0]["cyc_name"]


def find_bike_across_parks(client: DocomoBikeClient, parks: list, min_battery: int, prefer_csa: int):
    for park in parks:
        bikes = client.get_available_bikes(park["park_id"])
        if bikes:
            cyc_name = select_best_bike(bikes, min_battery, prefer_csa)
            if cyc_name:
                log.info("ポート %s でバイク %s を選択", park["park_name"], cyc_name)
                return cyc_name, park["park_name"]
    return None, None


def parse_reserve_limit(s: str) -> Optional[datetime]:
    """'2026/05/15 07:27:16' → datetime"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y/%m/%d %H:%M:%S")
    except Exception:
        return None


def main():
    now = datetime.now()

    # 土日は何もしない
    if now.weekday() >= 5:
        return

    config = load_config()

    start_hour = config.get("start_hour", 7)
    start_minute = config.get("start_minute", 0)
    end_hour = config.get("end_hour", 9)
    end_minute = config.get("end_minute", 0)

    start_dt = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    end_dt = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    final_hold_dt = end_dt - timedelta(minutes=FINAL_HOLD_MINUTES)

    # 時間外
    if now < start_dt or now >= end_dt:
        return

    log.info("=== tick開始 %s ===", now.strftime("%H:%M:%S"))

    client = DocomoBikeClient(config["session_id"], config["api_key"])
    parks = config["parks"]
    min_battery = config.get("prefer_battery_level_min", 2)
    prefer_csa = config.get("prefer_csa_type", 1)

    status = client.get_status()
    if status.get("result") != 200:
        log.error("ステータス取得失敗: %s", status)
        sys.exit(1)

    user_info = status.get("user_info", {})
    user_status = user_info.get("user_status", -1)

    if user_status == 1:
        # 予約中
        cyc_name = user_info.get("cyc_name", "")
        limit_str = user_info.get("reserve_limit", "")
        limit_dt = parse_reserve_limit(limit_str)

        if limit_dt is None:
            log.warning("reserve_limit パース失敗: %s", limit_str)
            return

        remaining = (limit_dt - now).total_seconds()
        log.info("予約中: %s (期限: %s, 残り %d秒)", cyc_name, limit_str, remaining)

        # 終了時刻が近い場合は予約を残してそのまま
        if now >= final_hold_dt:
            log.info("終了時刻が近いため予約を維持します")
            return

        # まだ残り時間がある場合は何もしない
        if remaining > REFRESH_THRESHOLD_SEC:
            return

        # 残り時間が少ない → キャンセルして再予約
        log.info("残り%d秒のためキャンセル＆再予約", remaining)
        cancel_result = client.cancel_reservation()
        if cancel_result is False:
            return  # 通常のキャンセル失敗。次のtickで再試行
        # 成功 or expired どちらの場合も新規予約へ進む

    else:
        # 予約なし
        log.info("予約なし。新規予約を試みます")

        # 終了時刻が近い場合は新規予約しない
        if now >= final_hold_dt:
            log.info("終了時刻が近いため新規予約はしません")
            return

    # 新規予約
    cyc_name, park_name = find_bike_across_parks(client, parks, min_battery, prefer_csa)
    if not cyc_name:
        log.warning("全ポートでバイクが見つかりません。次のtickで再試行")
        return

    result = client.reserve_bike(cyc_name)
    if result:
        log.info("予約完了: %s (%s)", cyc_name, park_name)


if __name__ == "__main__":
    main()
