#!/usr/bin/env python3
"""
docomo bike share 自動予約（毎分起動版）

launchd から毎分起動されることを前提に、1回の実行は数秒で終わる短時間プロセス。
状態はAPI側にあるためファイル保存不要。App Nap / スリープの影響を受けない。

動作:
- 平日 朝7:00〜9:00 / 夜19:00〜21:00 の間のみ動作（時間外なら即終了）
- 時間帯ごとに対象ポートが異なる（朝=skyz他, 夜=豊洲駅/シビックセンター）
- 現在の予約状況をAPIで確認
- 予約なし → 予約する
- 予約あり → 残り時間が3分以下ならキャンセル → 即再予約
- 終了5分前以降は予約を残して終了
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

# 時間帯ごとの動作定義（平日のみ）。parksは優先順（先頭ほど優先）。
# 朝と夜で予約を取り違えないよう state ファイルも時間帯別に分ける。
WINDOWS = [
    {
        "name": "morning",
        "start": (7, 0),
        "end": (9, 0),
        "parks": [
            {"park_id": "00010147", "park_name": "H1-29.skyz tower&garden"},
            {"park_id": "00010406", "park_name": "H1-68.芝浦工業大学附属中学高等学校"},
            {"park_id": "00010223", "park_name": "H1-44.テプコ豊洲ビル"},
        ],
    },
    {
        "name": "evening",
        "start": (19, 0),
        "end": (21, 0),
        "parks": [
            {"park_id": "00010093", "park_name": "H1-02.豊洲駅"},
            {"park_id": "00010278", "park_name": "H1-53.豊洲シビックセンター"},
        ],
    },
]

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


def state_path(window_name: str) -> str:
    return os.path.join(LOG_DIR, f"state_{window_name}.json")


def save_state(window_name: str, cyc_name: str, reserve_limit: str):
    try:
        with open(state_path(window_name), "w") as f:
            json.dump({"cyc_name": cyc_name, "reserve_limit": reserve_limit}, f)
    except Exception as e:
        log.warning("state保存失敗: %s", e)


def load_state(window_name: str) -> dict:
    try:
        with open(state_path(window_name)) as f:
            return json.load(f)
    except Exception:
        return {}


def clear_state(window_name: str):
    try:
        p = state_path(window_name)
        if os.path.exists(p):
            os.remove(p)
    except Exception as e:
        log.warning("state削除失敗: %s", e)


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

    def reserve_bike(self, cyc_name: str, window_name: str) -> Optional[dict]:
        data = self._request("POST", "reservecycle", json={"cyc_name": cyc_name})
        if data.get("result") != 200:
            log.error("予約失敗 (%s): %s", cyc_name, data)
            return None
        log.info("予約成功: %s (期限: %s)", cyc_name, data.get("reserve_limit"))
        save_state(window_name, cyc_name, data.get("reserve_limit", ""))
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


def get_active_window(now: datetime):
    """現在時刻が属する時間帯を返す。無ければ (None, None, None)。"""
    for w in WINDOWS:
        start_dt = now.replace(hour=w["start"][0], minute=w["start"][1], second=0, microsecond=0)
        end_dt = now.replace(hour=w["end"][0], minute=w["end"][1], second=0, microsecond=0)
        if start_dt <= now < end_dt:
            return w, start_dt, end_dt
    return None, None, None


def main():
    now = datetime.now()

    # 土日は何もしない
    if now.weekday() >= 5:
        return

    # 現在時刻が属する時間帯を判定（朝 or 夜）。時間外なら即終了。
    window, start_dt, end_dt = get_active_window(now)
    if window is None:
        return

    final_hold_dt = end_dt - timedelta(minutes=FINAL_HOLD_MINUTES)

    config = load_config()

    window_name = window["name"]
    parks = window["parks"]

    log.info("=== tick開始 %s [%s] ===", now.strftime("%H:%M:%S"), window_name)

    client = DocomoBikeClient(config["session_id"], config["api_key"])
    min_battery = config.get("prefer_battery_level_min", 2)
    prefer_csa = config.get("prefer_csa_type", 1)

    status = client.get_status()
    if status.get("result") != 200:
        log.error("ステータス取得失敗: %s", status)
        sys.exit(1)

    user_info = status.get("user_info", {})
    user_status = user_info.get("user_status", -1)

    if user_status == 1:
        # 予約中。status APIは reserve_limit を返さないため state ファイルから取得する。
        cyc_name = user_info.get("cyc_name", "")
        state = load_state(window_name)
        limit_str = state.get("reserve_limit", "")
        # cyc_name の整合性チェック（state が古い別予約の可能性）
        if state.get("cyc_name") and cyc_name and state.get("cyc_name") != cyc_name:
            log.warning("state不一致 (state=%s, api=%s): stateクリア", state.get("cyc_name"), cyc_name)
            limit_str = ""
        limit_dt = parse_reserve_limit(limit_str)

        if limit_dt is None:
            # state が無い/古い → 残り時間不明。終了時刻近くなければ安全側でキャンセル＆即再予約。
            log.warning("state不明のためキャンセル＆再予約を試行 (cyc=%s)", cyc_name)
            if now >= final_hold_dt:
                log.info("終了時刻が近いため予約を維持します")
                return
            cancel_result = client.cancel_reservation()
            if cancel_result is False:
                return
            clear_state(window_name)
            # 新規予約フェーズへ
        else:
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
            clear_state(window_name)
            # 成功 or expired どちらの場合も新規予約へ進む

    else:
        # 予約なし。残っている state は無効。
        clear_state(window_name)
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

    result = client.reserve_bike(cyc_name, window_name)
    if result:
        log.info("予約完了: %s (%s)", cyc_name, park_name)


if __name__ == "__main__":
    main()
