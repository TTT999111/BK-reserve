# BK-reserve

GitHub Actions で動かしている個人用の自動化スクリプト置き場。

| スクリプト | 内容 | ワークフロー |
| --- | --- | --- |
| `docomo_bike_tick.py` | ドコモ・バイクシェアの自動予約（毎分tick） | `.github/workflows/bike_reserve.yml` |
| `lark_expense_alert.py` | Lark への経費精算アラート（毎日1回） | `.github/workflows/lark_expense_alert.yml` |

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

### セットアップ

**1. Lark 側でカスタムボットを作る**

投稿先のグループ → 設定 → ボット → ボットを追加 → **Custom Bot（カスタムボット）** を選び、Webhook URL を控える。セキュリティ設定で「署名検証」を有効にした場合は、表示される Secret も控える。

**2. GitHub の Secrets / Variables を設定する**

リポジトリの Settings → Secrets and variables → Actions で登録する。

Secrets（必須）:

| 名前 | 内容 |
| --- | --- |
| `LARK_WEBHOOK_URL` | カスタムボットの Webhook URL |
| `LARK_WEBHOOK_SECRET` | 署名検証を有効にした場合のみ設定 |

Variables（任意・未設定なら既定値）:

| 名前 | 既定値 | 内容 |
| --- | --- | --- |
| `EXPENSE_SYSTEM_NAME` | `経費精算システム` | 本文に出す精算システムの名前（例: `freee 経費精算`） |
| `EXPENSE_URL` | なし | 設定するとカードに「◯◯を開く」ボタンが付く |
| `EXTRA_HOLIDAYS` | なし | 会社休業日。`12-29,12-30,12-31,01-02,01-03`（毎年）や `2026-08-14`（その年だけ）をカンマ区切りで指定 |
| `REMINDER_DAY` | `20` | リマインドの日 |
| `DEADLINE_DAY` | `25` | 締切の日 |
| `REMINDER_HOLIDAY_POLICY` | `keep` | 20日が休業日のときの扱い。`keep`=そのまま / `before`=前営業日 / `after`=翌営業日 |
| `LARK_MENTION_ALL` | `deadline` | `@全員` メンションの範囲。`deadline`=締切アラートのみ / `both`=20日のリマインドにも付ける / `none`=付けない |

**3. 動作確認**

Actions → `lark expense alert` → Run workflow で手動起動できる。入力欄で

- `判定日の上書き` に `2026-01-23` などを入れると、その日として判定する
- `強制配信する種別` に `reminder` / `deadline` を選ぶと、配信日でなくても送る
- `送信せず内容だけ確認する` を on にすると、Lark には送らずログに JSON を出す

まず dry run で文面を確認し、そのあと `強制配信する種別=deadline` で実際に届くか確かめるとよい。

### 起動タイミング

`.github/workflows/lark_expense_alert.yml` の `schedule` で毎日 **10:00 JST**（`0 1 * * *` UTC）に起動し、その日が配信日かどうかはスクリプト側で判定する。配信日でなければ何もせず終了する。

GitHub Actions の `schedule` は数分〜数十分遅れることがある。時刻の確実性が要る場合は、`docomo_bike_tick.py` と同様に外部cron（cron-job.org など）から `workflow_dispatch` API を叩いてもよい。ワークフローは `--once-per-day` 付きで実行するので、schedule と外部cronが二重に起動しても同じ日に2回配信されることはない（配信済みマーカーを Actions キャッシュで引き継ぐ）。

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
