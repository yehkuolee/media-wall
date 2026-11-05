#!/bin/bash
# 找哏小幫手（LINE Bot）：由 LaunchAgent com.lifeos.tipbot 開機自動啟動、掛掉自動重開
cd "$(dirname "$0")/.." || exit 1
git pull --rebase --autostash -q 2>/dev/null  # 先拿最新程式（Threads 排程也在推這個 repo）
exec /usr/bin/python3 tipbot/agent.py
