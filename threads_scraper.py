import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytz
from playwright.async_api import async_playwright

COOKIES_JSON = os.environ.get("THREADS_COOKIES", "")
TW_TZ = pytz.timezone("Asia/Taipei")
OUTPUT_DIR = Path(__file__).parent / "data"
OUTPUT_DIR.mkdir(exist_ok=True)


async def capture():
    if not COOKIES_JSON:
        print("❌ THREADS_COOKIES 未設定", file=sys.stderr)
        sys.exit(1)

    try:
        cookies = json.loads(COOKIES_JSON)
    except json.JSONDecodeError:
        print("❌ THREADS_COOKIES 不是合法 JSON", file=sys.stderr)
        sys.exit(1)

    # Cookie-Editor 匯出的格式需轉成 Playwright 格式
    pw_cookies = []
    for c in cookies:
        cookie = {
            "name":   c["name"],
            "value":  c["value"],
            "domain": c.get("domain", ".threads.com"),
            "path":   c.get("path", "/"),
        }
        if "sameSite" in c and c["sameSite"] in ("Strict", "Lax", "None"):
            cookie["sameSite"] = c["sameSite"]
        pw_cookies.append(cookie)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-TW",
            timezone_id="Asia/Taipei",
        )

        # 注入 cookie，跳過登入
        await context.add_cookies(pw_cookies)
        print(f"🍪 注入 {len(pw_cookies)} 個 cookie")

        page = await context.new_page()

        # 直接前往搜尋頁
        print("📱 前往 threads.com/search ...")
        await page.goto("https://www.threads.com/search", wait_until="networkidle")
        await page.wait_for_timeout(4000)

        # 驗證登入狀態
        page_text = await page.evaluate("document.body.innerText")
        if "登入或註冊" in page_text or "Log in or sign up" in page_text:
            await page.screenshot(path=str(OUTPUT_DIR / "debug_cookie_fail.png"))
            print("❌ Cookie 無效或已過期，請重新從瀏覽器匯出並更新 THREADS_COOKIES Secret", file=sys.stderr)
            await browser.close()
            sys.exit(1)
        print("  ✅ 登入狀態驗證通過")

        # 關閉可能出現的 app 推廣 modal
        for modal_sel in [
            '[aria-label="關閉"]',
            '[aria-label="Close"]',
            'div[role="dialog"] button',
        ]:
            try:
                btn = page.locator(modal_sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    print(f"  ✅ 關閉 modal（{modal_sel}）")
                    await page.wait_for_timeout(1000)
                    break
            except Exception:
                pass

        await page.wait_for_timeout(2000)

        # Debug 截圖
        await page.screenshot(path=str(OUTPUT_DIR / "debug_search.png"))

        # ── 截圖（取左側趨勢欄）──
        screenshot_path = OUTPUT_DIR / "threads_trending.png"
        await page.screenshot(
            path=str(screenshot_path),
            clip={"x": 0, "y": 0, "width": 680, "height": 900},
        )
        print(f"📸 截圖已存：{screenshot_path}")

        # ── 抽取趨勢話題 ──
        topics = await page.evaluate("""
            () => {
                const results = [];
                const seen = new Set();
                const countRe = /[\d,.]+\s*[萬千百]?\s*則/;

                const allEls = Array.from(document.querySelectorAll('*'));
                const countEls = allEls.filter(el =>
                    el.children.length === 0 &&
                    countRe.test(el.innerText || '') &&
                    (el.innerText || '').length < 30
                );

                for (const countEl of countEls) {
                    let container = countEl;
                    let link = null;
                    for (let i = 0; i < 8; i++) {
                        if (!container.parentElement) break;
                        container = container.parentElement;
                        const a = container.querySelector(
                            'a[href*="serp_type"], a[href*="search?q"], a[href*="/search"]'
                        );
                        if (a) { link = a; break; }
                    }
                    if (!link) continue;

                    const href = link.href;
                    if (seen.has(href) || href.includes('/login')) continue;
                    seen.add(href);

                    const lines = container.innerText.trim()
                        .split('\\n').map(l => l.trim()).filter(Boolean);
                    if (!lines.length) continue;

                    const title = lines[0];
                    if (title.length < 2 || title.length > 60) continue;

                    const countLine = lines.find(l => countRe.test(l)) || '';
                    const desc = lines.find(
                        l => l !== title && l !== countLine && l.length > 8
                    ) || '';

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
