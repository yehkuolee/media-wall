#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

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

echo "$(date '+%Y-%m-%d %H:%M:%S') 🚀 開始抓取 Google 熱搜..."
"$PYTHON3" google_trends_scraper.py

# 推回 GitHub（與 run_threads.sh 共用鎖，避免同時操作 git）
LOCK="/tmp/media-wall-git.lock"
for _ in $(seq 1 60); do mkdir "$LOCK" 2>/dev/null && break; sleep 2; done
trap 'rmdir "$LOCK" 2>/dev/null' EXIT
git stash --quiet 2>/dev/null || true
git pull --rebase --quiet 2>&1 || true
git stash pop --quiet 2>/dev/null || true
git add data/google_trending.json
if git diff --staged --quiet; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ℹ️  資料無變動，跳過 commit"
else
    git commit -m "chore: google trending $(TZ=Asia/Taipei date +'%Y-%m-%dT%H:%M')"
    git push
    echo "$(date '+%Y-%m-%d %H:%M:%S') ✅ 已推送更新"
fi
