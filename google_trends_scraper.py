"""爬 Google Trends「搜尋趨勢」網頁（trends.google.com.tw/trending?geo=TW）

網頁為 JS 動態載入，RSS 內容跟網頁排序不同，所以用 Playwright 在本機（台灣 IP）抓，
輸出 data/google_trending.json 給 app.py 讀取。
"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import pytz
from playwright.async_api import async_playwright

TW_TZ = pytz.timezone("Asia/Taipei")
OUTPUT_DIR = Path(__file__).parent / "data"
OUTPUT_DIR.mkdir(exist_ok=True)
URL = "https://trends.google.com.tw/trending?geo=TW&hl=zh-TW"


async def capture():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1400, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
            locale="zh-TW",
            timezone_id="Asia/Taipei",
        )
        page = await context.new_page()
        print("📈 前往 Google Trends 搜尋趨勢 ...")
        await page.goto(URL, wait_until="networkidle")
        await page.wait_for_timeout(4000)

        # 每列 td：[勾選框, 關鍵字, 搜尋量, 開始時間, 相關搜尋, ...]
        rows = await page.evaluate(r"""
            () => Array.from(document.querySelectorAll('table tbody tr')).map(r =>
                Array.from(r.querySelectorAll('td')).map(td => (td.innerText || '').trim()))
        """)
        await browser.close()

    keywords = []
    for cells in rows:
        if len(cells) < 3 or not cells[1]:
            continue
        keywords.append({
            "title":  cells[1].split("\n")[0].strip(),
            "volume": cells[2].split("\n")[0].strip(),
        })

    if not keywords:
        print("❌ Google Trends 沒抓到任何關鍵字", file=sys.stderr)
        sys.exit(1)

    output = {"updated_at": datetime.now(TW_TZ).isoformat(), "keywords": keywords}
    (OUTPUT_DIR / "google_trending.json").write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"✅ 抓到 {len(keywords)} 個 Google 熱搜字")
    for k in keywords:
        print(f"  [{k['volume']}] {k['title']}")


if __name__ == "__main__":
    asyncio.run(capture())
