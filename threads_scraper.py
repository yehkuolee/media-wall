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
        print("🔐 前往登入頁...")
        await page.goto("https://www.threads.com/login", wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)

        # Debug：先截圖看登入頁長什麼樣
        await page.screenshot(path=str(OUTPUT_DIR / "debug_login.png"))
        print(f"  📸 debug_login.png 已存（目前 URL: {page.url}）")

        # 接受 cookie 同意彈窗（歐盟 GDPR 等）
        for cookie_sel in [
            'button:has-text("Allow all cookies")',
            'button:has-text("接受所有")',
            'button:has-text("Accept All")',
            '[data-testid="cookie-policy-manage-dialog-accept-button"]',
        ]:
            try:
                btn = page.locator(cookie_sel).first
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    await page.wait_for_timeout(1000)
                    print(f"  ✅ 接受 cookie: {cookie_sel}")
                    break
            except Exception:
                pass

        # 填帳號（多 selector fallback）
        username_sel = None
        for sel in [
            'input[name="username"]',
            'input[autocomplete="username"]',
            'input[aria-label*="username" i]',
            'input[aria-label*="使用者名稱"]',
            'input[aria-label*="手機號碼"]',
            'input[type="text"]',
        ]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    await el.fill(USERNAME)
                    username_sel = sel
                    print(f"  ✅ 帳號填入（selector: {sel}）")
                    break
            except Exception:
                continue

        if not username_sel:
            await page.screenshot(path=str(OUTPUT_DIR / "debug_no_input.png"))
            print("❌ 找不到帳號輸入框，已存 debug_no_input.png", file=sys.stderr)
            await browser.close()
            sys.exit(1)

        # 填密碼
        await page.locator('input[type="password"]').first.fill(PASSWORD)
        print("  ✅ 密碼填入")

        # 送出登入
        await page.locator('button[type="submit"]').first.click()
        print("  ✅ 點擊登入")

        try:
            await page.wait_for_url(lambda url: "login" not in url, timeout=15000)
            print(f"  ✅ 登入成功，跳轉至：{page.url}")
        except Exception:
            await page.screenshot(path=str(OUTPUT_DIR / "debug_after_login.png"))
            print("  ⚠️ 登入後未跳轉，已存 debug_after_login.png", file=sys.stderr)

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
