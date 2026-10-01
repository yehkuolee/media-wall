"""
Streamlit Cloud 防休眠：用真瀏覽器開網站（純 HTTP 請求不算流量），
遇到休眠頁就按「Yes, get this app back up!」喚醒。主力：舊 MacBook Pro LaunchAgent 每 10 分鐘（launchd/ 內 plist）；google-trends.yml 順便跑當備援（GitHub 排程實際常 4-6 小時才跑一次）。
"""
from playwright.sync_api import sync_playwright

APP_URL = "https://media-wall-kyupypbavjip4jw2tsgida.streamlit.app/"

from datetime import datetime
print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), end=" ", flush=True)

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto(APP_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(8_000)

    wake = page.get_by_role("button", name="get this app back up")
    if wake.count():
        print("App 在休眠，按下喚醒")
        wake.first.click()
        page.wait_for_timeout(60_000)
    else:
        print("App 醒著")
        page.wait_for_timeout(15_000)  # 停留一下，確保算成一次造訪
    browser.close()
