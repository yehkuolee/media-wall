#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

COOKIES_FILE="$HOME/.config/threads/cookies.json"
if [ ! -f "$COOKIES_FILE" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ❌ Cookie 檔案不存在：$COOKIES_FILE" >&2
    exit 1
fi

export THREADS_COOKIES="$(cat "$COOKIES_FILE")"

# 找 python3（支援 Homebrew arm/intel 與系統 Python）
PYTHON3=""
for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [ -x "$p" ]; then
        PYTHON3="$p"
        break
    fi
done

if [ -z "$PYTHON3" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ❌ 找不到 python3" >&2
    exit 1
fi

# 先同步遠端再抓，避免多台機器輪流推送時互相衝突
git rebase --abort 2>/dev/null || true
git pull --rebase --autostash --quiet 2>&1 || true

echo "$(date '+%Y-%m-%d %H:%M:%S') 🚀 開始抓取 Threads 趨勢..."
"$PYTHON3" threads_scraper.py

# 推回 GitHub
git add data/
if git diff --staged --quiet; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ℹ️  資料無變動，跳過 commit"
    exit 0
fi
git commit --quiet -m "chore: threads trending $(TZ=Asia/Taipei date +'%Y-%m-%dT%H:%M')"
# push 被拒（別台剛推過）就 rebase 重推；衝突時以本次抓到的資料為準（rebase 下 theirs = 本機 commit）
for i in 1 2 3; do
    if git push --quiet 2>&1; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') ✅ 已推送更新"
        exit 0
    fi
    git pull --rebase --autostash -X theirs --quiet 2>&1 || git rebase --abort 2>/dev/null || true
    sleep 5
done
echo "$(date '+%Y-%m-%d %H:%M:%S') ❌ 推送失敗 3 次" >&2
exit 1
