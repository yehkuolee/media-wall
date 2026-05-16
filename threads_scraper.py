import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytz
from playwright.async_api import async_playwright

USERNAME = os.environ.get("THREADS_USERNAME", "")
PASSWORD = os.environ.get("THREADS_PASSWORD", "")
TW_TZ = pytz.timezone("Asia/Taipei")
OUTPUT_DIR = Path(__file__).parent / "data"
OUTPUT_DIR.mkdir(exist_ok=True)


async def dismiss_popups(page):
    for selector in [
        'button:has-text("現在不要")',
        'button:has-text("Not Now")',
        'button:has-text("稍後")',
        'button:has-text("跳過")',
        'div[role="button"]:has-text("現在不要")',
    ]:
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=1500):
                await el.click()
                await page.wait_for_timeout(500)
        except Exception:
            pass


async def capture():
    if not USERNAME or not PASSWORD:
        print("❌ THREADS_USERNAME / THREADS_PASSWORD 未設定", file=sys.stderr)
        sys.exit(1)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 390, "height": 844},
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
                "Mobile/15E148 Safari/604.1"
            ),
            locale="zh-TW",
            timezone_id="Asia/Taipei",
        )
        page = await context.new_page()

        # ── 登入 ──
        print("🔐 登入 Threads...")
        await page.goto("https://www.threads.com/login", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        await page.locator('input[name="username"], input[autocomplete="username"]').first.fill(USERNAME)
        await page.locator('input[name="password"], input[type="password"]').first.fill(PASSWORD)
        await page.locator('button[type="submit"]').first.click()

        try:
            await page.wait_for_url(lambda url: "login" not in url, timeout=15000)
        except Exception:
            print("⚠️ 登入可能失敗，繼續嘗試...", file=sys.stderr)

        await page.wait_for_timeout(3000)
        await dismiss_popups(page)

        # ── 前往趨勢搜尋頁 ──
        print("📱 前往 threads.com/search ...")
        await page.goto("https://www.threads.com/search", wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        await dismiss_popups(page)

        # ── 截圖 ──
        screenshot_path = OUTPUT_DIR / "threads_trending.png"
        await page.screenshot(
            path=str(screenshot_path),
            clip={"x": 0, "y": 0, "width": 390, "height": 750},
        )
        print(f"📸 截圖已存：{screenshot_path}")

        # ── 抽取趨勢話題 ──
        topics = await page.evaluate("""
            () => {
                const results = [];
                const seen = new Set();
                const countRe = /[\d,.]+\s*[萬千百]?\s*則/;

                const links = Array.from(document.querySelectorAll('a[href*="search"]'));
                for (const a of links) {
                    const href = a.href;
                    if (!href || seen.has(href)) continue;
                    if (href.includes('/login') || href.includes('context=')) continue;
                    seen.add(href);

                    let container = a;
                    for (let i = 0; i < 5; i++) {
                        if (!container.parentElement) break;
                        container = container.parentElement;
                        if (countRe.test(container.innerText)) break;
                    }

                    const lines = container.innerText.trim()
                        .split('\\n').map(l => l.trim()).filter(Boolean);
                    if (!lines.length) continue;

                    const title = lines[0];
                    if (title.length < 2 || title.length > 60) continue;

                    const countLine = lines.find(l => countRe.test(l)) || '';
                    const desc = lines.find(l => l !== title && l !== countLine && l.length > 8) || '';

                    results.push({ title, description: desc, count: countLine, link: href });
                    if (results.length >= 8) break;
                }
                return results;
            }
        """)

        # ── 存 JSON ──
        output = {
            "updated_at": datetime.now(TW_TZ).isoformat(),
            "topics": topics,
        }
        json_path = OUTPUT_DIR / "threads_trending.json"
        json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
        print(f"✅ 抓到 {len(topics)} 則趨勢話題")
        for t in topics:
            print(f"  [{t['count']}] {t['title']}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(capture())
