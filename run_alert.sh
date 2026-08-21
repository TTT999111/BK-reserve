#!/usr/bin/env bash
#
# 経費精算アラートを1コマンドで評価する。Claude の定期タスク(Routine)から呼ぶ想定。
#
#   bash run_alert.sh --markdown
#
# 配信日でなければ何も出力せず exit 0 で終わる。配信日なら本文を標準出力に出すので、
# 呼び出し側は「出力が空でなければ Lark に投稿する」だけでよい。
# 日付・祝日の判定はすべて lark_expense_alert.py 側にあるので、呼び出し側は持たない。
#
# オプション:
#   --markdown              本文を markdown テキストで出す（既定はカードJSON）
#   --no-pull               git pull を行わない（テスト用）
#   --force reminder|deadline   配信日でなくても強制的に出力する
#   --date YYYY-MM-DD       判定日を上書きする
#
set -euo pipefail

cd "$(dirname "$0")"

emit=card
pull=1
extra=()

while [ $# -gt 0 ]; do
  case "$1" in
    --markdown)  emit=markdown; shift ;;
    --no-pull)   pull=0; shift ;;
    --force)     extra+=(--force "$2"); shift 2 ;;
    --date)      extra+=(--date "$2"); shift 2 ;;
    -h|--help)   sed -n '2,18p' "$0"; exit 0 ;;
    *)           echo "不明なオプション: $1" >&2; exit 2 ;;
  esac
done

# 最新のコードで判定する（ネットワーク不通でも止めない）
if [ "$pull" = "1" ] && [ -d .git ]; then
  git pull --ff-only --quiet >&2 || echo "警告: git pull に失敗。手元のコードで続行する" >&2
fi

# requests が無いときだけ入れる
if ! python3 -c "import requests" >/dev/null 2>&1; then
  echo "requests を導入中..." >&2
  pip install --quiet requests >&2
fi

# ログは stderr、本文だけが stdout に出る
exec python3 lark_expense_alert.py --dry-run --emit "$emit" ${extra[@]+"${extra[@]}"}
