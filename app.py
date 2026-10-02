import json
import streamlit as st
import streamlit.components.v1 as components
import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from pathlib import Path
import pytz
import re
import html as html_mod

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False

try:
    from pytrends.request import TrendReq
    HAS_PYTRENDS = True
except ImportError:
    HAS_PYTRENDS = False

import xml.etree.ElementTree as ET

TW_TZ = pytz.timezone("Asia/Taipei")

RSS_FEEDS = {
    "Google 新聞": "https://news.google.com/rss?hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
}

# 爬熱門排行頁（依頁面順序＝熱門排名）；selector 指定條目、title_sel / title_attr 指定標題位置
# rss_fallback：爬不到 5 則（網站改版）時改用 RSS 最新新聞頂上
SCRAPED_SOURCES = {
    "CNA 中央社": {   # 舊 RSS（/rss/aall.aspx）已 404，改爬即時列表（中央社無熱門榜）
        "url": "https://www.cna.com.tw/list/aall.aspx",
        "selector": "#jsMainList li a",
        "title_sel": "h2",
        "pattern": "/news/",
        "min_len": 8,
        "limit": 20,
        "strip_time": False,
        "base_url": "https://www.cna.com.tw",
    },
    "自由時報": {
        "url": "https://news.ltn.com.tw/list/breakingnews/popular",
        "selector": "ul.list a[title]",
        "pattern": "ltn.com.tw/news/",
        "title_attr": True,
        "min_len": 8,
        "limit": 20,
        "strip_time": False,
        "rss_fallback": "https://news.ltn.com.tw/rss/all.xml",
    },
    "Yahoo 新聞": {
        "url": "https://tw.news.yahoo.com/most-popular",
        "selector": "a.mega-item-header-link",
        "pattern": ".html",
        "min_len": 8,
        "limit": 20,
        "strip_time": False,
        "base_url": "https://tw.news.yahoo.com",
        "rss_fallback": "https://tw.news.yahoo.com/rss",
    },
    "東森新聞": {
        "url": "https://news.ebc.net.tw/hot",
        "selector": "a.row_box",
        "pattern": "/news/",
        "title_attr": True,
        "min_len": 8,
        "limit": 20,
        "strip_time": False,
        "base_url": "https://news.ebc.net.tw",
    },
    "壹蘋新聞網": {
        "url": "https://news.nextapple.com/realtime/hit",
        "pattern": "news.nextapple.com/",
        "url_must_contain": "",
        "min_len": 10,
        "limit": 30,
        "strip_time": False,
    },
    "中國時報": {
        "url": "https://www.chinatimes.com/hotnews?chdtv",
        "pattern": "chinatimes.com/",
        "url_must_contain": "/202",
        "min_len": 8,
        "limit": 30,
        "strip_time": False,
    },
    "三立新聞": {
        "url": "https://www.setn.com/viewall/0",   # 三立「熱門」頁
        "pattern": "setn.com/news/",
        "url_must_contain": "",
        "min_len": 8,
        "limit": 30,
        "strip_time": True,   # 三立標題末尾夾有 HH:MM，需清除
        "base_url": "https://www.setn.com",
    },
    "聯合報熱門": {
        "url": "https://udn.com/rank/pv/2",
        "pattern": "news/story",
        "url_must_contain": "",
        "min_len": 8,
        "limit": 20,
        "strip_time": False,
    },
}

FALLBACK_KEYWORDS = [
    "台積電", "AI", "美股", "台幣", "颱風", "iPhone",
    "NVIDIA", "比特幣", "ETF", "聯準會", "通膨", "選舉",
    "房價", "電動車", "鴻海", "半導體", "台股", "美中",
]

WEEKDAY = {0: "一", 1: "二", 2: "三", 3: "四", 4: "五", 5: "六", 6: "日"}

# ── Page config ────────────────────────────────────────────────────
st.set_page_config(
    page_title="現在紅什麼",
    layout="wide",
    initial_sidebar_state="collapsed",
)

if HAS_AUTOREFRESH:
    st_autorefresh(interval=300_000, key="wall_refresh")

# ── CSS ────────────────────────────────────────────────────────────
st.markdown("""
<style>
#MainMenu, footer, header { visibility: hidden; }
[data-testid="stAppViewContainer"] {
    background: #f5f2ed;
}
[data-testid="stHeader"] { background: transparent; }
.block-container { padding: 0.6rem 1rem !important; max-width: 100% !important; }

/* ── KPI card ── */
.kpi-card {
    background: #ffffff;
    border: 1px solid #d8d5d0;
    border-top: 3px solid #121212;
    border-radius: 0;
    padding: 14px 16px;
    text-align: center;
    position: relative;
    overflow: hidden;
    box-shadow: 0 3px 10px rgba(0,0,0,0.08), 0 1px 3px rgba(0,0,0,0.05);
    transition: transform .2s, box-shadow .2s;
    height: 100%;
}
.kpi-card::before { display: none; }
.kpi-icon  { font-size: 1.2rem; margin-bottom: 3px; }
.kpi-label { color: #999; font-size: .63rem; margin-bottom: 5px; letter-spacing: 1.5px; text-transform: uppercase; }
.kpi-value { color: #121212; font-size: 1.5rem; font-weight: 800; font-family: 'Courier New', monospace; line-height: 1.2; }
.kpi-sub   { color: #2a7a4a; font-size: .7rem; margin-top: 5px; font-weight: 600; }
.kpi-sub.warn { color: #c41230; }

/* ── Section title ── */
.sec-title {
    color: #121212;
    font-size: .70rem;
    font-weight: 700;
    letter-spacing: 2px;
    text-transform: uppercase;
    border-top: 2px solid #121212;
    border-bottom: 1px solid #d8d5d0;
    border-left: none;
    padding: 5px 0;
    margin-bottom: 10px;
}

/* ── Keyword cloud ── */
.kw-cloud {
    background: #ffffff;
    border: 1px solid #d8d5d0;
    border-top: 2px solid #121212;
    border-radius: 0;
    padding: 12px 10px;
    box-shadow: 0 3px 10px rgba(0,0,0,0.08), 0 1px 3px rgba(0,0,0,0.05);
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    min-height: 130px;
    align-content: flex-start;
}
.kw-tag {
    border-radius: 2px;
    padding: 3px 10px;
    cursor: default;
    transition: all .15s;
    white-space: nowrap;
    line-height: 1.4;
}
.kw-hot  { background: rgba(196,18,48,.07);  border:1px solid rgba(196,18,48,.28);  color:#c41230; }
.kw-warm { background: rgba(160,100,0,.06);  border:1px solid rgba(160,100,0,.25);  color:#a06400; }
.kw-cool { background: rgba(30,70,150,.06);  border:1px solid rgba(30,70,150,.22);  color:#1e4696; }

/* ── News card ── */
.news-card {
    background: #ffffff;
    border: 1px solid #e8e5e0;
    border-left: 3px solid #d8d5d0;
    border-radius: 0;
    padding: 9px 12px;
    margin-bottom: 5px;
    display: flex;
    align-items: flex-start;
    gap: 10px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
    transition: transform .2s, box-shadow .2s, border-left-color .2s;
}
.news-card:hover { background: #ffffff; transform: translateY(-2px); box-shadow: 0 6px 18px rgba(0,0,0,0.11), 0 2px 5px rgba(0,0,0,0.06); border-left-color: #c41230; }
.news-rank       { color: #c41230; font-weight: 800; font-size: .92rem; min-width: 22px; line-height: 1.6; font-family: 'Courier New', monospace; }
.news-rank.gold  { color: #b8860b; }
.news-rank.silv  { color: #888; }
.news-rank.brnz  { color: #a0522d; }
.news-body       {}
.news-title-link {
    display: block;
    color: #121212;
    font-size: .84rem;
    line-height: 1.55;
    text-decoration: none;
    font-family: Georgia, serif;
}
.news-title-link:hover { color: #c41230; }
.news-meta       { color: #aaa; font-size: .64rem; margin-top: 2px; }
.news-src        { color: #888; font-weight: 600; }

/* ── Threads card ── */
.threads-card {
    background: #ffffff;
    border: 1px solid #e8e5e0;
    border-radius: 0;
    padding: 8px 10px;
    margin-bottom: 4px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
    transition: transform .2s, box-shadow .2s;
}
.threads-card:hover { transform: translateY(-2px); box-shadow: 0 6px 16px rgba(0,0,0,0.10), 0 2px 4px rgba(0,0,0,0.05); }
.threads-title {
    color: #1a1a1a; font-size: .78rem;
    text-decoration: none; display: block; line-height: 1.4;
}
.threads-title:hover { color: #c41230; }
.threads-desc { color: #888; font-size: .67rem; margin-top: 2px; line-height: 1.4; }
.threads-count { font-size: .66rem; font-weight: 700; color: #aaa; margin-top: 3px; white-space: nowrap; }

/* ── PTT card ── */
.ptt-card {
    background: #ffffff;
    border: 1px solid #e8e5e0;
    border-radius: 0;
    padding: 8px 10px;
    margin-bottom: 4px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
    transition: transform .2s, box-shadow .2s;
}
.ptt-card:hover { transform: translateY(-2px); box-shadow: 0 6px 16px rgba(0,0,0,0.10), 0 2px 4px rgba(0,0,0,0.05); }
.ptt-title {
    color: #1a1a1a; font-size: .78rem; flex:1;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
    text-decoration: none;
}
.ptt-title:hover { color: #c41230; }
.ptt-push  { font-size: .73rem; font-weight: 800; min-width: 30px; text-align: right; color: #c41230; }
.ptt-push.boom  { color: #8b0000; }
.ptt-push.green { color: #2a7a4a; }

/* ── Source stat ── */
.src-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: #ffffff;
    border: 1px solid #e8e5e0;
    border-radius: 0;
    padding: 6px 10px;
    margin-bottom: 4px;
    box-shadow: 0 2px 5px rgba(0,0,0,0.05), 0 1px 2px rgba(0,0,0,0.03);
    transition: transform .2s, box-shadow .2s;
}
.src-row:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(0,0,0,0.09), 0 1px 3px rgba(0,0,0,0.05); }
.src-name  { color: #444; font-size: .76rem; font-weight: 600; }
.src-count { color: #2a7a4a; font-size: .76rem; font-weight: 700; }
.src-bar   {
    height: 2px;
    background: linear-gradient(90deg, #c41230, #880018);
    border-radius: 0;
    margin-top: 3px;
}

/* ── Ticker ── */
.ticker-wrap {
    background: #121212;
    border: none;
    border-radius: 0;
    padding: 7px 0;
    overflow: hidden;
    margin-top: 12px;
    white-space: nowrap;
}
.ticker-label { color: #ffffff; font-size: .68rem; font-weight: 700; padding: 0 12px; letter-spacing: 2px; text-transform: uppercase; }
.ticker-text  {
    color: #cccccc;
    font-size: .74rem;
    display: inline-block;
    animation: scroll-left 60s linear infinite;
}
@keyframes scroll-left {
    from { transform: translateX(80vw); }
    to   { transform: translateX(-100%); }
}

/* ── Footer ── */
.mw-footer { text-align:center; color:#bbb; font-size:.62rem; margin-top:8px; }

/* scrollbar */
::-webkit-scrollbar       { width: 4px; }
::-webkit-scrollbar-track { background: #f5f2ed; }
::-webkit-scrollbar-thumb { background: #d8d5d0; border-radius: 2px; }

/* ── 深色模式（純黑看盤風）：標題列按鈕在 <html> 加上 mw-dark ── */
html.mw-dark, html.mw-dark body, html.mw-dark .stApp,
html.mw-dark [data-testid="stAppViewContainer"] { background: #000000; color: #e6e6e6; }
html.mw-dark .kpi-card, html.mw-dark .kw-cloud, html.mw-dark .news-card, html.mw-dark .news-card:hover,
html.mw-dark .threads-card, html.mw-dark .ptt-card, html.mw-dark .src-row {
    background: #0b0b0b; border-color: #222222; box-shadow: none;
}
html.mw-dark .kw-cloud, html.mw-dark .kpi-card { border-top-color: #e6e6e6; }
html.mw-dark .news-card       { border-left-color: #333333; }
html.mw-dark .news-card:hover { border-left-color: #ff3b4e; box-shadow: 0 0 0 1px #ff3b4e33; }
html.mw-dark .threads-card:hover, html.mw-dark .ptt-card:hover, html.mw-dark .src-row:hover { box-shadow: 0 0 0 1px #333333; }
html.mw-dark .sec-title { color: #e6e6e6; border-top-color: #e6e6e6; border-bottom-color: #222222; }
html.mw-dark .news-title-link, html.mw-dark .threads-title, html.mw-dark .ptt-title { color: #e6e6e6; }
html.mw-dark .news-title-link:hover, html.mw-dark .threads-title:hover, html.mw-dark .ptt-title:hover { color: #ff3b4e; }
html.mw-dark .news-rank       { color: #ff3b4e; }
html.mw-dark .news-rank.gold  { color: #f5c542; }
html.mw-dark .news-rank.silv  { color: #b0b0b0; }
html.mw-dark .news-rank.brnz  { color: #d98a4e; }
html.mw-dark .news-meta, html.mw-dark .threads-count { color: #6f6f6f; }
html.mw-dark .news-src, html.mw-dark .threads-desc { color: #8a8a8a; }
html.mw-dark .kw-hot  { background: rgba(255,59,78,.12);  border-color: rgba(255,59,78,.45);  color: #ff3b4e; }
html.mw-dark .kw-warm { background: rgba(255,176,0,.10);  border-color: rgba(255,176,0,.40);  color: #ffb000; }
html.mw-dark .kw-cool { background: rgba(56,189,248,.10); border-color: rgba(56,189,248,.35); color: #38bdf8; }
html.mw-dark .ptt-push        { color: #ff3b4e; }
html.mw-dark .ptt-push.boom   { color: #ff6b78; }
html.mw-dark .ptt-push.green, html.mw-dark .src-count, html.mw-dark .kpi-sub { color: #22c55e; }
html.mw-dark .src-name  { color: #c8c8c8; }
html.mw-dark .src-bar   { background: linear-gradient(90deg, #ff3b4e, #8b0000); }
html.mw-dark .ticker-wrap { background: #000000; border-top: 1px solid #222222; border-bottom: 1px solid #222222; }
html.mw-dark .ticker-label { color: #ff3b4e; }
html.mw-dark .ticker-text  { color: #f5c542; }
html.mw-dark .mw-footer { color: #555555; }
html.mw-dark ::-webkit-scrollbar-track { background: #000000; }
html.mw-dark ::-webkit-scrollbar-thumb { background: #333333; }
</style>
""", unsafe_allow_html=True)


# ── Data fetchers ──────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def fetch_keywords() -> list[str]:
    # 優先讀本機排程爬的 Google Trends 網頁資料（與 trends.google.com.tw/trending 一致）
    json_path = Path(__file__).parent / "data" / "google_trending.json"
    try:
        data = json.loads(json_path.read_text())
        updated = datetime.fromisoformat(data["updated_at"])
        if datetime.now(updated.tzinfo) - updated < timedelta(hours=3):
            titles = [k["title"] for k in data.get("keywords", []) if k.get("title")]
            if titles:
                return titles[:25]
    except Exception:
        pass
    # 備援：Google Trends RSS（公開，不需要登入）
    try:
        r = requests.get(
            "https://trends.google.com/trending/rss?geo=TW",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        root = ET.fromstring(r.content)
        ns = {"ht": "https://trends.google.com/trending/rss"}
        titles = [item.find("title").text for item in root.iter("item") if item.find("title") is not None]
        if titles:
            return titles[:25]
    except Exception:
        pass
    # 備援：pytrends
    if HAS_PYTRENDS:
        try:
            pt = TrendReq(hl="zh-TW", tz=480, timeout=(10, 30))
            df = pt.trending_searches(pn="taiwan")
            return df[0].tolist()[:25]
        except Exception:
            pass
    return FALLBACK_KEYWORDS


@st.cache_data(ttl=300, show_spinner=False)
def fetch_scraped_news() -> list[dict]:
    """爬取沒有 RSS 的媒體首頁"""
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    items: list[dict] = []
    for source, cfg in SCRAPED_SOURCES.items():
        try:
            r = requests.get(cfg["url"], headers=headers, timeout=10)
            soup = BeautifulSoup(r.text, "html.parser")
            seen: set[str] = set()
            count = 0
            must = cfg.get("url_must_contain", "")
            base = cfg.get("base_url", "")
            for a in soup.select(cfg.get("selector", "a[href]")):
                title_el = a.select_one(cfg["title_sel"]) if cfg.get("title_sel") else None
                raw_title = ((title_el.get_text(strip=True) if title_el else None)
                             or (a.get("title") if cfg.get("title_attr") else None)
                             or a.get_text(strip=True))
                href = a.get("href", "")
                if base and href.startswith("/"):
                    href = base + href
                title = re.sub(r'\d{1,2}:\d{2}$', '', raw_title).strip() if cfg.get("strip_time") else raw_title
                if (len(title) >= cfg["min_len"]
                        and cfg["pattern"] in href
                        and (not must or must in href)
                        and title not in seen):
                    seen.add(title)
                    items.append({
                        "title":  title,
                        "link":   href,
                        "source": source,
                        "pub":    None,
                    })
                    count += 1
                    if count >= cfg["limit"]:
                        break
        except Exception:
            count = 0
        if count < 5 and cfg.get("rss_fallback"):
            items = [n for n in items if n["source"] != source]
            try:
                feed = feedparser.parse(requests.get(cfg["rss_fallback"], headers=headers, timeout=8).content)
                for e in feed.entries[:cfg["limit"]]:
                    items.append({"title": e.get("title", "").strip(), "link": e.get("link", "#"),
                                  "source": source, "pub": None})
            except Exception:
                pass
    return items


@st.cache_data(ttl=300, show_spinner=False)
def fetch_news() -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MediaWallBot/1.0)"}
    all_items: list[dict] = []
    for source, url in RSS_FEEDS.items():
        try:
            r = requests.get(url, headers=headers, timeout=8)
            feed = feedparser.parse(r.content)
            for e in feed.entries[:20]:
                pub = None
                if getattr(e, "published_parsed", None):
                    _dt = datetime(*e.published_parsed[:6], tzinfo=pytz.utc).astimezone(TW_TZ)
                    pub = _dt if _dt.year >= 2020 else None  # 過濾掉 RSS 日期異常（如 1970）
                all_items.append({
                    "title":  e.get("title", "").strip(),
                    "link":   e.get("link", "#"),
                    "source": source,
                    "pub":    pub,
                })
        except Exception:
            continue
    # RSS 文章依時間排序，爬蟲文章（無時間）附在後面
    rss_items = [n for n in all_items if n["pub"]]
    rss_items.sort(key=lambda x: x["pub"], reverse=True)
    scraped = fetch_scraped_news()
    return rss_items + scraped


@st.cache_data(ttl=180, show_spinner=False)
def fetch_ptt(board: str = "Gossiping", limit: int = 10) -> list[dict]:
    try:
        headers = {
            "Cookie": "over18=1",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
        r = requests.get(f"https://www.ptt.cc/bbs/{board}/index.html", headers=headers, timeout=8)
        soup = BeautifulSoup(r.text, "html.parser")
        posts: list[dict] = []
        for item in soup.select(".r-ent"):
            a = item.select_one(".title a")
            push = item.select_one(".nrec span")
            if not a:
                continue
            push_text = push.text.strip() if push else "0"
            if push_text == "爆":
                push_num = 100
            elif push_text.startswith("X"):
                push_num = -int(push_text[1:]) if push_text[1:].isdigit() else -1
            elif push_text.isdigit():
                push_num = int(push_text)
            else:
                push_num = 0
            posts.append({
                "title":    a.text.strip(),
                "url":      "https://www.ptt.cc" + a["href"],
                "push":     push_text,
                "push_num": push_num,
            })
        posts.sort(key=lambda x: x["push_num"], reverse=True)
        return posts[:limit]
    except Exception:
        return []


@st.cache_data(ttl=300, show_spinner=False)
def fetch_threads_trending() -> dict:
    json_path = Path(__file__).parent / "data" / "threads_trending.json"
    if not json_path.exists():
        return {"updated_at": None, "topics": []}
    try:
        return json.loads(json_path.read_text())
    except Exception:
        return {"updated_at": None, "topics": []}


# ── Helpers ────────────────────────────────────────────────────────

def round_robin_top(all_news: list, n: int = 15) -> list:
    """各來源輪流各取一篇，確保 Top N 不被單一媒體佔據"""
    by_source: dict[str, list] = {}
    for item in all_news:
        by_source.setdefault(item["source"], []).append(item)
    result: list = []
    sources = list(by_source.keys())
    ptrs = {s: 0 for s in sources}
    while len(result) < n:
        added = False
        for s in sources:
            if ptrs[s] < len(by_source[s]):
                result.append(by_source[s][ptrs[s]])
                ptrs[s] += 1
                added = True
                if len(result) >= n:
                    break
        if not added:
            break
    return result


def time_ago(dt) -> str:
    if not dt:
        return ""
    diff = int((datetime.now(TW_TZ) - dt).total_seconds() / 60)
    if diff < 1:
        return "剛剛"
    if diff < 60:
        return f"{diff} 分鐘前"
    if diff < 1440:
        return f"{diff // 60} 小時前"
    return f"{diff // 1440} 天前"


def rank_class(i: int) -> str:
    return {0: "gold", 1: "silv", 2: "brnz"}.get(i, "")


def ptt_push_class(num: int) -> str:
    if num >= 100:
        return "ptt-push boom"
    if num >= 30:
        return "ptt-push green"
    return "ptt-push"


# ── Keyword cloud HTML ──────────────────────────────────────────────

def keyword_cloud_html(kws: list[str]) -> str:
    if not kws:
        return '<div style="color:#aaa;padding:30px;text-align:center">資料取得中...</div>'
    sizes = ["1.15rem", "1.0rem", "0.9rem", "0.82rem", "0.76rem"]
    out = '<div class="kw-cloud">'
    for i, kw in enumerate(kws):
        if i < 5:
            cls, sz = "kw-tag kw-hot",  sizes[0]
        elif i < 10:
            cls, sz = "kw-tag kw-warm", sizes[1]
        elif i < 16:
            cls, sz = "kw-tag kw-cool", sizes[2]
        else:
            cls, sz = "kw-tag kw-cool", sizes[3]
        kw_safe  = html_mod.escape(kw)
        kw_query = html_mod.escape(requests.utils.quote(kw))
        href = f"https://news.google.com/search?q={kw_query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        out += f'<a class="{cls}" style="font-size:{sz};text-decoration:none;" href="{href}" target="_blank" rel="noopener">{kw_safe}</a>'
    out += "</div>"
    return out


MASTHEAD_CSS = """
.mw-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: #ffffff;
    border-top: 5px solid #121212;
    border-bottom: 2px solid #121212;
    border-left: none; border-right: none;
    border-radius: 0;
    padding: 14px 24px;
    margin-bottom: 14px;
    box-shadow: 0 4px 16px rgba(0,0,0,0.10), 0 1px 4px rgba(0,0,0,0.06);
}
.mw-title { font-family: Georgia, 'Times New Roman', serif; font-size: 1.6rem; font-weight: 700; color: #121212; letter-spacing: 2px; }
.mw-subtitle { font-size: .63rem; color: #999; letter-spacing: 2px; text-transform: uppercase; margin-top: 3px; }
.mw-live { display: flex; align-items: center; gap: 8px; }
.live-dot {
    width: 8px; height: 8px; background: #c41230;
    border-radius: 50%;
    animation: blink 1.6s ease-in-out infinite;
}
@keyframes blink {
    0%,100% { opacity:1; }
    50%      { opacity:.2; }
}
.live-text { color: #c41230; font-size: .72rem; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; }
.mw-time { color: #121212; font-size: 1.1rem; font-weight: 700; font-family: 'Courier New', monospace; text-align: right; }
.mw-date { color: #999; font-size: .67rem; text-align: right; letter-spacing: 1px; }
.mw-updated { color: #999; font-size: .67rem; text-align: right; letter-spacing: 1px; margin-top: 2px; }
.mw-right { display: flex; align-items: center; gap: 14px; }
.mw-theme-btn {
    width: 32px; height: 32px; border: 1px solid #d8d5d0; background: transparent;
    border-radius: 50%; cursor: pointer; font-size: .95rem; line-height: 1; padding: 0;
}
.mw-theme-btn:hover { border-color: #c41230; }

/* 深色模式 */
html.mw-dark, html.mw-dark body { background: #000000 !important; }
.mw-dark .mw-header { background: #0b0b0b; border-top-color: #e6e6e6; border-bottom-color: #222222; box-shadow: none; }
.mw-dark .mw-title, .mw-dark .mw-time { color: #e6e6e6; }
.mw-dark .mw-time { color: #22c55e; }
.mw-dark .mw-subtitle, .mw-dark .mw-date, .mw-dark .mw-updated { color: #6f6f6f; }
.mw-dark .live-dot { background: #ff3b4e; }
.mw-dark .live-text { color: #ff3b4e; }
.mw-dark .mw-theme-btn { border-color: #333333; }
.mw-dark .mw-theme-btn:hover { border-color: #ff3b4e; }
"""


# ── Main ───────────────────────────────────────────────────────────

def main():
    now = datetime.now(TW_TZ)

    # ── Header ──────────────────────────────────────────────────
    # 用 components.html（iframe）渲染，時鐘才能用 JS 每秒跳動；st.markdown 不執行 script
    components.html(f"""
    <style>
    html, body {{ margin: 0; padding: 0 1px; background: #f5f2ed;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang TC", "Noto Sans TC", sans-serif; }}
{MASTHEAD_CSS}
    .mw-header {{ margin-bottom: 0; }}
    </style>
    <div class="mw-header">
        <div>
            <div class="mw-title">現在紅什麼</div>
            <div class="mw-subtitle">即時媒體熱度監測 · LifeOS</div>
        </div>
        <div class="mw-live">
            <div class="live-dot"></div>
            <span class="live-text">LIVE 即時更新中</span>
        </div>
        <div class="mw-right">
            <button class="mw-theme-btn" id="mw-theme-btn" title="切換深色／淺色">🌙</button>
            <div>
                <div class="mw-time" id="mw-clock">{now.strftime('%H:%M:%S')}</div>
                <div class="mw-date" id="mw-date">{now.strftime('%Y/%m/%d')} (週{WEEKDAY[now.weekday()]})</div>
                <div class="mw-updated">最後更新 {now.strftime('%H:%M:%S')}</div>
            </div>
        </div>
    </div>
    <script>
    // 深色模式：選擇存在父頁面 localStorage，沒選過就跟系統設定；
    // 把 mw-dark 加在父頁面 <html>（主頁 CSS 用）和自己的 <html>（標題列用）
    const THEME_KEY = "mw-theme";
    function loadTheme() {{
        try {{ const t = window.parent.localStorage.getItem(THEME_KEY); if (t) return t; }} catch (e) {{}}
        return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }}
    function applyTheme(t) {{
        const dark = t === "dark";
        document.documentElement.classList.toggle("mw-dark", dark);
        try {{ window.parent.document.documentElement.classList.toggle("mw-dark", dark); }} catch (e) {{}}
        document.getElementById("mw-theme-btn").textContent = dark ? "☀️" : "🌙";
    }}
    let theme = loadTheme();
    applyTheme(theme);
    document.getElementById("mw-theme-btn").addEventListener("click", () => {{
        theme = theme === "dark" ? "light" : "dark";
        try {{ window.parent.localStorage.setItem(THEME_KEY, theme); }} catch (e) {{}}
        applyTheme(theme);
    }});

    const WD = "一二三四五六日";
    function tick() {{
        // 以台北時區取時間，不受觀看者電腦時區影響
        const p = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {{
            timeZone: "Asia/Taipei", hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit", second: "2-digit", weekday: "short"
        }}).formatToParts(new Date()).map(x => [x.type, x.value]));
        const wd = WD["MonTueWedThuFriSatSun".indexOf(p.weekday) / 3];
        document.getElementById("mw-clock").textContent = `${{p.hour}}:${{p.minute}}:${{p.second}}`;
        document.getElementById("mw-date").textContent = `${{p.year}}/${{p.month}}/${{p.day}} (週${{wd}})`;
    }}
    tick();
    setInterval(tick, 1000);

    // 看門狗：每次刷新這個 iframe 都會重建，LOADED 跟著重設。
    // 若超過 11 分鐘沒刷新（睡眠喚醒、背景分頁、連線斷掉導致 st_autorefresh 失效），整頁重載。
    // iframe 沒有 allow-top-navigation，所以把 reload 腳本塞進父頁面執行。
    const LOADED = Date.now();
    setInterval(() => {{
        if (Date.now() - LOADED < 11 * 60 * 1000) return;
        try {{
            const s = window.parent.document.createElement("script");
            s.textContent = "window.location.reload()";
            window.parent.document.head.appendChild(s);
        }} catch (e) {{ window.parent.location.reload(); }}
    }}, 30000);
    </script>
    """, height=96)

    # ── Fetch all data ───────────────────────────────────────────
    with st.spinner("載入資料中..."):
        keywords      = fetch_keywords()
        all_news      = fetch_news()
        ptt_goss      = fetch_ptt("Gossiping", 10)
        threads_data  = fetch_threads_trending()

    today = now.date()
    today_news = [n for n in all_news if n["pub"] and n["pub"].date() == today]
    source_counts: dict[str, int] = {}
    for n in all_news:
        source_counts[n["source"]] = source_counts.get(n["source"], 0) + 1
    threads_topics = threads_data.get("topics", [])
    threads_updated = threads_data.get("updated_at")

    # ── Main 3 columns ───────────────────────────────────────────
    left, center, right = st.columns([1.1, 2.1, 1.1])

    # ── LEFT ──
    with left:
        st.markdown('<div class="sec-title">🧵 Threads 最新趨勢話題</div>', unsafe_allow_html=True)
        if threads_topics:
            for topic in threads_topics[:15]:
                t    = html_mod.escape(topic["title"])
                u    = html_mod.escape(topic.get("link", "#"))
                desc = html_mod.escape(topic.get("description", ""))
                count = html_mod.escape(topic.get("count", ""))
                desc_html = f'<div class="threads-desc">{desc}</div>' if desc else ""
                st.markdown(f"""
                <div class="threads-card">
                    <a class="threads-title" href="{u}" target="_blank" rel="noopener">{t}</a>
                    {desc_html}
                    <div class="threads-count">{count}</div>
                </div>""", unsafe_allow_html=True)
            if threads_updated:
                try:
                    upd = datetime.fromisoformat(threads_updated).strftime('%H:%M')
                    st.markdown(
                        f'<div style="color:#aaa;font-size:.62rem;text-align:right;margin-top:4px">更新：{upd}</div>',
                        unsafe_allow_html=True,
                    )
                except Exception:
                    pass
        else:
            st.markdown('<div style="color:#aaa;padding:16px;text-align:center">Threads 資料尚未抓取</div>', unsafe_allow_html=True)

    # ── CENTER ──
    with center:
        st.markdown('<div class="sec-title">📰 即時熱門新聞 Top 15（各媒體均攤）</div>', unsafe_allow_html=True)
        top15 = round_robin_top(all_news, 15)
        for i, news in enumerate(top15):
            rc = rank_class(i)
            rank_num = i + 1
            raw_title = news["title"] or "（標題載入中）"
            display_title = html_mod.escape(raw_title[:50] + "…" if len(raw_title) > 50 else raw_title)
            link = html_mod.escape(news.get("link") or "#")
            source = html_mod.escape(news["source"])
            ago = time_ago(news["pub"])
            ago_html = f' · {html_mod.escape(ago)}' if ago else ''
            st.markdown(f"""
            <div class="news-card">
                <div class="news-rank {rc}">{rank_num}</div>
                <div class="news-body">
                    <a class="news-title-link" href="{link}" target="_blank" rel="noopener">
                        {display_title}
                    </a>
                    <div class="news-meta">
                        <span class="news-src">📌 {source}</span>{ago_html}
                    </div>
                </div>
            </div>""", unsafe_allow_html=True)

    # ── RIGHT ──
    with right:
        st.markdown('<div class="sec-title">🔍 Google 熱搜字</div>', unsafe_allow_html=True)
        st.markdown(keyword_cloud_html(keywords), unsafe_allow_html=True)

        st.markdown("<div style='margin:10px 0'></div>", unsafe_allow_html=True)

        st.markdown('<div class="sec-title">💬 PTT 八卦板 熱門</div>', unsafe_allow_html=True)
        if ptt_goss:
            for p in ptt_goss[:8]:
                t = html_mod.escape(p['title'])
                u = html_mod.escape(p['url'])
                st.markdown(f"""
                <div class="ptt-card">
                    <a class="ptt-title" href="{u}" target="_blank" rel="noopener" title="{t}">{t}</a>
                    <div class="{ptt_push_class(p['push_num'])}">{p['push']}</div>
                </div>""", unsafe_allow_html=True)
        else:
            st.markdown('<div style="color:#aaa;padding:16px;text-align:center">暫時無法取得</div>', unsafe_allow_html=True)

        st.markdown("<div style='margin:10px 0'></div>", unsafe_allow_html=True)

        st.markdown('<div class="sec-title">📊 媒體來源統計</div>', unsafe_allow_html=True)
        total = sum(source_counts.values()) or 1
        for src, cnt in sorted(source_counts.items(), key=lambda x: x[1], reverse=True):
            pct = int(cnt / total * 100)
            st.markdown(f"""
            <div class="src-row">
                <div class="src-name">{src}</div>
                <div class="src-count">{cnt} 則</div>
            </div>
            <div style="padding:0 4px;margin-bottom:6px;">
                <div class="src-bar" style="width:{pct}%"></div>
            </div>""", unsafe_allow_html=True)

    # ── Ticker ──────────────────────────────────────────────────
    if all_news:
        ticker = "  ｜  ".join(
            f"📌 {n['source']}：{n['title'][:28]}" for n in all_news[:20]
        )
        st.markdown(f"""
        <div class="ticker-wrap">
            <span class="ticker-label">📡 最新</span>
            <span class="ticker-text">{ticker}</span>
        </div>""", unsafe_allow_html=True)

    # ── Footer ──────────────────────────────────────────────────
    st.markdown(f"""
    <div class="mw-footer">
        資料來源：Google Trends · RSS (Google 新聞) · 中央社即時 · 熱門排行（自由 / Yahoo / 東森 / 壹蘋 / 中時 / 聯合報 / 三立） · PTT &nbsp;｜&nbsp;
        更新時間：{now.strftime('%Y-%m-%d %H:%M:%S')} (台北 UTC+8) &nbsp;｜&nbsp;
        每 5 分鐘自動刷新
    </div>
    """, unsafe_allow_html=True)


main()
