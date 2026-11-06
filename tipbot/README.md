# 找哏小幫手（LINE Bot）

LINE 官方帳號「ETtoday競媒監測器」（@164jhlis，輕用量免費方案）。

- 每 10 分鐘（05:00～24:00）收集訊號：競媒熱門榜、🚀 瀏覽暴衝、Google 熱搜、Threads 趨勢、PTT 八卦板
  → 同一話題串成一群 → 算熱度 → 比對 ETtoday 寫了沒 → 存成 `state/latest.json`
- LINE 傳「哏」「未寫」「暴衝」「說明」→ 回覆卡片清單（回覆不扣免費則數）；「清單」→ 純文字直列版（電腦版 LINE 卡片不好滑），可加分類或未寫：「清單 政治」「清單 未寫 政治」
- 分類查詢：傳「政治」「哏 政治」「未寫 政治」只看該類（政治、社會、生活〔含健康〕、娛樂、財經、國際〔含兩岸〕、體育、科技、寵物）。
  分類方式：先看各家網址的分類（自由、東森、壹蘋、中央社、三立娛樂），看不出來的（Yahoo、聯合、中時、Google 熱搜、Threads、PTT）每輪整批問 Gemini，問過的存 `state/categories.json`；標題有毛孩、寵物、動保等字一律歸寵物
- 超強訊號（熱度 ≥ 12、ETtoday 沒寫、沒推過）才主動推播：每天 ≤ 3 則、每月 ≤ 60 則
- 第一個私訊 Bot 的人自動成為推播對象（`state/owner.json`，刪掉就能重綁）
- 資料抓取沿用 `../app.py`（所以 app.py 最後是 `if __name__ == "__main__": main()`）

## 舊機安裝（只要做一次）

在舊機開 Claude Code，照順序做：

1. `cd ~/LifeOS/media-wall && git pull`
2. 金鑰寫進 `tipbot/.env`（權限 600，不進 git）：
   ```
   LINE_CHANNEL_SECRET=...
   LINE_CHANNEL_ACCESS_TOKEN=...
   GEMINI_API_KEY=...      # 媒體牆那把（ETtoday 比對用）；舊機有 .streamlit/secrets.toml 也會自動讀
   ```
3. 試跑一次：`/usr/bin/python3 tipbot/agent.py --once`（印出哏清單就對了）
4. Cloudflare 通道多開一個網址（跟工作站 news.etwriter-ai.com 共用同一條 tunnel）：
   - `~/.cloudflared/config.yml` 的 `ingress:` 在 news 那條**下面**、最後的 `http_status:404` **上面**加：
     ```
     - hostname: tip.etwriter-ai.com
       service: http://127.0.0.1:8790
     ```
   - `~/LifeOS/bin/cloudflared tunnel route dns fb-news-tool tip.etwriter-ai.com`
   - `launchctl kickstart -k gui/$(id -u)/com.lifeos.fb-news-tunnel`
   - **不要**把 tip 加進 Cloudflare Access（LINE 伺服器要能直接連；程式自己驗 LINE 簽章）
5. 開機自動啟動：
   ```
   cp launchd/com.lifeos.tipbot.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.lifeos.tipbot.plist
   ```
6. 確認：`curl https://tip.etwriter-ai.com` 回 `tipbot ok`
7. LINE Official Account Manager → 設定 → Messaging API → Webhook 網址填
   `https://tip.etwriter-ai.com/callback` → 儲存；回應設定 → Webhook 打開
   （LINE Developers Console 也可以按 Verify 測試）
8. 手機 LINE 傳「哏」給 Bot

## 日常

- log：`~/Library/Logs/tipbot.log`
- 重開：`launchctl kickstart -k gui/$(id -u)/com.lifeos.tipbot`
- 改門檻：`.env` 加 `TIPBOT_PUSH_MIN_SCORE=10`（越小推越多），重開
- 本月推了幾則：`cat tipbot/state/pushed.json`
- 誰加了好友、問過幾次：管理者（推播對象）私訊 Bot 傳「名單」；完整資料在 `tipbot/state/members.json`（LINE 名稱一週更新一次，封鎖的會標 🚫；別人傳「名單」只會看到說明）
