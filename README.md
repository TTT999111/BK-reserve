# BK-reserve

個人用の自動化スクリプト置き場。

| スクリプト | 内容 | 実行方法 |
| --- | --- | --- |
| `docomo_bike_tick.py` | ドコモ・バイクシェアの自動予約（毎分tick） | GitHub Actions（`bike_reserve.yml`） |
| `lark_expense_alert.py` | Lark への経費精算アラート（毎日1回） | Claude の定期タスク（Routine）。`lark_expense_alert.yml` は手動フォールバック |

---

## Lark 経費精算アラート bot

Lark（飛書）のグループに、経費精算のリマインドと締切アラートを自動投稿する。

### 配信ルール

| 種別 | 配信日 | 内容 |
| --- | --- | --- |
| リマインド | 毎月 **20日** | 「今月の締切は◯月◯日です／残りN営業日」 |
| 締切アラート | 毎月 **25日**（土日祝・会社休業日なら**その前の営業日**） | 「本日が締切です」 |

- 「営業日」＝ 土日でなく、国民の祝日（振替休日・国民の休日を含む）でなく、`EXTRA_HOLIDAYS` で指定した会社休業日でもない日。
- 例）25日が日曜なら金曜に前倒し。25日が月曜の祝日なら金曜に前倒し。
- 20日は既定では前倒し・後ろ倒しをしない（`REMINDER_HOLIDAY_POLICY` で変更可）。20日が休みでも、リマインドに書かれる締切日は前倒し後の日付になる。
- 前倒しで20日と締切日が同じ日になった場合は、締切アラートだけを配信する。

任意の年の配信予定はコマンドで確認できる。

```console
$ python3 lark_expense_alert.py --calendar 2026
2026年の配信予定（祝日データ: 内閣府CSV / リマインド方針: keep）
  月  リマインド             締切アラート            備考
  1月  1月20日(火)          1月23日(金)          締切25日(日曜日)→23日
  2月  2月20日(金)          2月25日(水)
 ...
```

### 祝日データ

1. 内閣府が公開している[「国民の祝日」CSV](https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv) を毎回取得し、これを正とする（法改正・特例が自動で反映される）。取得できた内容は `logs/` にキャッシュする。
2. 取得に失敗した場合は、スクリプト内蔵の計算ロジック（振替休日・国民の休日まで算出）にフォールバックする。2007〜2099年について `jpholiday` と全件一致することを確認済み。

### 配信の仕組み

日々の配信は **Claude の定期タスク（Routine）** が担う。Lark のカスタムボットを作る必要も、GitHub に Secret を登録する必要もない。既存の Lark 連携（lark-cli）をそのまま使う。

```
毎日 10:00 JST に Routine が起動
  → lark-restore で lark-cli を復元
  → このリポジトリを clone/pull して python3 lark_expense_alert.py --dry-run を実行
  → 標準出力が空 = 配信日ではない → 何もせず終了（ログは stderr）
  → JSON が出た = 配信日 → card を「Thinking Japan Team」グループに投稿
  → lark_sync_check.sh でトークンを書き戻す
```

**日付判定はすべてスクリプト側にある**ので、Routine 側に「20日かどうか」「25日が祝日かどうか」といったロジックを持たせない。毎日そのまま実行してよい。

### セットアップ

接続フォルダのある Cowork セッションで、以下をそのまま貼る（1回だけ）。

```
経費精算アラートの定期タスクを作って。

1. lark-restore を実行して lark-cli を復元する
2. Lark の「Thinking Japan Team」グループの chat_id を調べる
3. 動作テスト:
   git clone https://github.com/TTT999111/BK-reserve && cd BK-reserve
   pip install requests
   python3 lark_expense_alert.py --force deadline --dry-run
   → 出力された card JSON を「Thinking Japan Team」に1通投稿して見え方を確認する
     （@全員が正しくレンダリングされるかもここで確認する）
4. 問題なければ毎日 10:00 JST の Routine を作成する。中身:
   - lark-restore を実行
   - 上記リポジトリを clone/pull して python3 lark_expense_alert.py --dry-run を実行
   - 標準出力が空なら配信日ではないので何もせず終了（ログは stderr に出る）
   - JSON が出たら card を「Thinking Japan Team」に投稿
   - 最後に lark_sync_check.sh でトークンを書き戻す
```

GitHub のリポジトリセッションからは接続フォルダが見えず Lark に触れないため、この作業は Cowork 側で行う必要がある。

### 設定項目

すべて環境変数。未設定なら既定値で動く。Routine 方式ではプロンプト側で `EXPENSE_URL=... python3 lark_expense_alert.py ...` のように渡す。

| 名前 | 既定値 | 内容 |
| --- | --- | --- |
| `EXPENSE_SYSTEM_NAME` | `経費精算システム` | 本文に出す精算システムの名前（例: `freee 経費精算`） |
| `EXPENSE_URL` | なし | 設定するとカードに「◯◯を開く」ボタンが付く |
| `EXTRA_HOLIDAYS` | なし | 会社休業日。`12-29,12-30,12-31,01-02,01-03`（毎年）や `2026-08-14`（その年だけ）をカンマ区切りで指定 |
| `REMINDER_DAY` | `20` | リマインドの日 |
| `DEADLINE_DAY` | `25` | 締切の日 |
| `REMINDER_HOLIDAY_POLICY` | `keep` | 20日が休業日のときの扱い。`keep`=そのまま / `before`=前営業日 / `after`=翌営業日 |
| `LARK_MENTION_ALL` | `deadline` | `@全員` メンションの範囲。`deadline`=締切アラートのみ / `both`=20日のリマインドにも付ける / `none`=付けない |
| `USE_HOLIDAY_CSV` | `true` | `false` にすると内閣府CSVを取りに行かず組み込み計算だけで判定する |

### GitHub Actions（手動フォールバック）

`.github/workflows/lark_expense_alert.yml` は、Lark カスタムボットの webhook 方式に戻したくなった場合のフォールバックとして残してある。**`schedule` は持たせていない**ので、放置しても勝手に動かない。

使う場合:

1. 投稿先グループ → 設定 → ボット → ボットを追加 → **Custom Bot** を作り、Webhook URL を控える
2. Settings → Secrets and variables → Actions に `LARK_WEBHOOK_URL` を登録（Lark 側で署名検証を有効にしたなら `LARK_WEBHOOK_SECRET` も）
3. Actions → `lark expense alert` → Run workflow で手動起動。`判定日の上書き` / `強制配信する種別` / `送信せず内容だけ確認する` を指定できる
4. 日次で回したければ `on:` に `schedule: - cron: '0 1 * * *'`（=10:00 JST）を足す

「設定項目」の各値は、この方式では GitHub の Variables として登録するとワークフローが読み込む。

### ローカルでの実行

```bash
pip install requests

# 配信予定の確認（送信しない）
python3 lark_expense_alert.py --calendar 2026

# 文面の確認（送信しない）
EXPENSE_SYSTEM_NAME='freee 経費精算' \
  python3 lark_expense_alert.py --date 2026-01-23 --dry-run

# 実際に送る
export LARK_WEBHOOK_URL='https://open.larksuite.com/open-apis/bot/v2/hook/xxxx'
python3 lark_expense_alert.py --force deadline

# テスト
python3 test_lark_expense_alert.py
```
