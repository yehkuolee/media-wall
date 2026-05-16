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

echo "$(date '+%Y-%m-%d %H:%M:%S') 🚀 開始抓取 Threads 趨勢..."
"$PYTHON3" threads_scraper.py

# 推回 GitHub
git pull --rebase --quiet 2>&1 || true
git add data/
if git diff --staged --quiet; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ℹ️  資料無變動，跳過 commit"
else
    git commit -m "chore: threads trending $(TZ=Asia/Taipei date +'%Y-%m-%dT%H:%M')"
    git push
    echo "$(date '+%Y-%m-%d %H:%M:%S') ✅ 已推送更新"
fi
