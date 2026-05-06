import streamlit as st
import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime
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

TW_TZ = pytz.timezone("Asia/Taipei")

RSS_FEEDS = {
    "Google 新聞": "https://news.google.com/rss?hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
    "CNA 中央社": "https://www.cna.com.tw/rss/aall.aspx",
    "UDN 聯合": "https://udn.com/rssfeed/news/2/0?ch=news",
    "ETtoday": "https://feeds.feedburner.com/ettoday/roadnews",
    "Yahoo 新聞": "https://tw.news.yahoo.com/rss",
    "自由時報": "https://news.ltn.com.tw/rss/all.xml",
}

# 沒有標題的 RSS 或無 RSS，改爬頁面
SCRAPED_SOURCES = {
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
        "url": "https://www.setn.com/",
        "pattern": "NewsID=",
        "url_must_contain": "",
        "min_len": 8,
        "limit": 30,
        "strip_time": True,   # 三立標題末尾夾有 HH:MM，需清除
        "base_url": "https://www.setn.com",
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
    page_title="媒體熱度監測牆",
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
    background: radial-gradient(ellipse at top, #0a1628 0%, #030912 60%);
}
[data-testid="stHeader"] { background: transparent; }
.block-container { padding: 0.6rem 1rem !important; max-width: 100% !important; }

/* ── Header ── */
.mw-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: linear-gradient(90deg, #041428 0%, #0b2545 50%, #041428 100%);
    border: 1px solid #1a4080;
    border-radius: 10px;
    padding: 12px 24px;
    margin-bottom: 12px;
    box-shadow: 0 0 40px rgba(0,150,255,.12), inset 0 1px 0 rgba(255,255,255,.05);
}
.mw-title { font-size: 1.4rem; font-weight: 800; color: #fff; letter-spacing: 3px; }
.mw-subtitle { font-size: .65rem; color: #4a6fa5; letter-spacing: 1px; margin-top: 2px; }
.mw-live { display: flex; align-items: center; gap: 8px; }
.live-dot {
    width: 10px; height: 10px; background: #00ff88;
    border-radius: 50%; box-shadow: 0 0 8px #00ff88;
    animation: blink 1.6s ease-in-out infinite;
}
@keyframes blink {
    0%,100% { opacity:1; box-shadow: 0 0 8px #00ff88; }
    50%      { opacity:.4; box-shadow: 0 0 14px #00ff88; }
}
.live-text { color: #00ff88; font-size: .8rem; font-weight: 700; letter-spacing: 1px; }
.mw-time { color: #64b5f6; font-size: 1.15rem; font-weight: 700; font-family: monospace; text-align: right; }
.mw-date { color: #3a6a9a; font-size: .7rem; text-align: right; }

/* ── KPI card ── */
.kpi-card {
    background: linear-gradient(135deg, #0d1f3c 0%, #162d52 100%);
    border: 1px solid #1e3a6e;
    border-radius: 10px;
    padding: 14px 16px;
    text-align: center;
    position: relative;
    overflow: hidden;
    box-shadow: 0 4px 20px rgba(0,100,200,.08);
    height: 100%;
}
.kpi-card::before {
    content: '';
    position: absolute;
    top: 0; left: 10%; right: 10%;
    height: 2px;
    background: linear-gradient(90deg, transparent, #00aaff, transparent);
    border-radius: 2px;
}
.kpi-icon  { font-size: 1.3rem; margin-bottom: 3px; }
.kpi-label { color: #5a7fa8; font-size: .68rem; margin-bottom: 5px; letter-spacing: .5px; }
.kpi-value { color: #fff; font-size: 1.55rem; font-weight: 800; font-family: 'Courier New', monospace; line-height: 1.2; }
.kpi-sub   { color: #00c87a; font-size: .7rem; margin-top: 5px; }
.kpi-sub.warn { color: #ffd700; }

/* ── Section title ── */
.sec-title {
    color: #00aaff;
    font-size: .78rem;
    font-weight: 700;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    border-left: 3px solid #00aaff;
    padding-left: 9px;
    margin-bottom: 9px;
}

/* ── Keyword cloud ── */
.kw-cloud {
    background: linear-gradient(135deg, #0d1f3c, #0f1e38);
    border: 1px solid #1a3060;
    border-radius: 9px;
    padding: 14px 12px;
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
    min-height: 140px;
    align-content: flex-start;
}
.kw-tag {
    border-radius: 20px;
    padding: 4px 11px;
    cursor: default;
    transition: all .18s;
    white-space: nowrap;
    line-height: 1.4;
}
.kw-hot  { background: rgba(255,80,80,.12);  border:1px solid rgba(255,80,80,.35);  color:#ff7070; }
.kw-warm { background: rgba(255,190,0,.09);  border:1px solid rgba(255,190,0,.35);  color:#ffd060; }
.kw-cool { background: rgba(0,150,255,.09);  border:1px solid rgba(0,150,255,.28);  color:#64b5f6; }

/* ── News card ── */
.news-card {
    background: linear-gradient(135deg, #0d1f3c, #0f1e38);
    border: 1px solid #1a3060;
    border-radius: 8px;
    padding: 9px 13px;
    margin-bottom: 7px;
    display: flex;
    align-items: flex-start;
    gap: 10px;
    transition: border-color .18s, transform .18s;
}
.news-card:hover { border-color: #00aaff; transform: translateX(3px); }
.news-rank       { color: #00aaff; font-weight: 800; font-size: 1rem; min-width: 22px; line-height: 1.5; }
.news-rank.gold  { color: #ffd700; }
.news-rank.silv  { color: #c0c0c0; }
.news-rank.brnz  { color: #cd7f32; }
.news-body       {}
.news-title-link {
    display: block;
    color: #d8e8f8;
    font-size: .83rem;
    line-height: 1.5;
    text-decoration: none;
}
.news-title-link:hover { color: #00c8ff; text-decoration: underline; }
.news-meta       { color: #3a5a85; font-size: .67rem; margin-top: 3px; }
.news-src        { color: #4a7aa8; font-weight: 600; }

/* ── PTT card ── */
.ptt-card {
    background: #0b1c36;
    border: 1px solid #192f58;
    border-radius: 6px;
    padding: 7px 11px;
    margin-bottom: 5px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
}
.ptt-title {
    color: #b8cee8; font-size: .78rem; flex:1;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
    text-decoration: none;
}
.ptt-title:hover { color: #00c8ff; text-decoration: underline; }
.ptt-push  { font-size: .75rem; font-weight: 800; min-width: 30px; text-align: right; color: #ff6b6b; }
.ptt-push.boom  { color: #ff3838; }
.ptt-push.green { color: #00cc70; }

/* ── Source stat ── */
.src-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: #0b1c36;
    border: 1px solid #192f58;
    border-radius: 6px;
    padding: 7px 12px;
    margin-bottom: 5px;
}
.src-name  { color: #64b5f6; font-size: .78rem; font-weight: 600; }
.src-count { color: #00cc70; font-size: .8rem; font-weight: 700; }
.src-bar   {
    height: 3px;
    background: linear-gradient(90deg, #00aaff, #0044aa);
    border-radius: 2px;
    margin-top: 4px;
}

/* ── Ticker ── */
.ticker-wrap {
    background: #041020;
    border: 1px solid #1a3060;
    border-radius: 7px;
    padding: 7px 0;
    overflow: hidden;
    margin-top: 10px;
    white-space: nowrap;
}
.ticker-label { color: #00aaff; font-size: .72rem; font-weight: 700; padding: 0 12px; }
.ticker-text  {
    color: #90b8d8;
    font-size: .75rem;
    display: inline-block;
    animation: scroll-left 60s linear infinite;
}
@keyframes scroll-left {
    from { transform: translateX(80vw); }
    to   { transform: translateX(-100%); }
}

/* ── Footer ── */
.mw-footer { text-align:center; color:#1e3555; font-size:.62rem; margin-top:8px; }

/* scrollbar */
::-webkit-scrollbar       { width: 4px; }
::-webkit-scrollbar-track { background: #041020; }
::-webkit-scrollbar-thumb { background: #1a4080; border-radius: 2px; }
</style>
""", unsafe_allow_html=True)


# ── Data fetchers ──────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def fetch_keywords() -> list[str]:
    if not HAS_PYTRENDS:
        return FALLBACK_KEYWORDS
    try:
        pt = TrendReq(hl="zh-TW", tz=480, timeout=(10, 30))
        df = pt.trending_searches(pn="taiwan")
        return df[0].tolist()[:25]
    except Exception:
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
            for a in soup.select("a[href]"):
                raw_title = a.get_text(strip=True)
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
            continue
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
        return '<div style="color:#3a5a85;padding:30px;text-align:center">資料取得中...</div>'
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


# ── Main ───────────────────────────────────────────────────────────

def main():
    now = datetime.now(TW_TZ)

    # ── Header ──────────────────────────────────────────────────
    st.markdown(f"""
    <div class="mw-header">
        <div>
            <div class="mw-title">📡 媒體熱度監測牆</div>
            <div class="mw-subtitle">Public Media Heat Monitor · LifeOS</div>
        </div>
        <div class="mw-live">
            <div class="live-dot"></div>
            <span class="live-text">LIVE 即時更新中</span>
        </div>
        <div>
            <div class="mw-time">{now.strftime('%H:%M:%S')}</div>
            <div class="mw-date">{now.strftime('%Y/%m/%d')} (週{WEEKDAY[now.weekday()]})</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Fetch all data ───────────────────────────────────────────
    with st.spinner("載入資料中..."):
        keywords  = fetch_keywords()
        all_news  = fetch_news()
        ptt_goss  = fetch_ptt("Gossiping", 10)
        ptt_stock = fetch_ptt("Stock", 6)

    today = now.date()
    today_news = [n for n in all_news if n["pub"] and n["pub"].date() == today]
    source_counts: dict[str, int] = {}
    for n in all_news:
        source_counts[n["source"]] = source_counts.get(n["source"], 0) + 1
    max_push = max((p["push_num"] for p in ptt_goss), default=0)

    # ── KPI Row ─────────────────────────────────────────────────
    k1, k2, k3, k4, k5 = st.columns(5)
    kpis = [
        (k1, "🔥", "Google 熱搜關鍵字", str(len(keywords)), "▲ 即時趨勢", ""),
        (k2, "📰", "今日新聞篇數",      f"{len(today_news):,}", f"▲ {len(source_counts)} 個媒體", ""),
        (k3, "📡", "RSS 文章總數",      f"{len(all_news):,}",  f"▲ 近期 {len(RSS_FEEDS)} 來源", ""),
        (k4, "💬", "PTT 八卦最高推文",  "爆" if max_push >= 100 else str(max_push), "▲ 板上人氣", "warn" if max_push < 30 else ""),
        (k5, "🕐", "最後更新",          now.strftime("%H:%M"), "▲ 每 5 分鐘刷新", ""),
    ]
    for col, icon, label, value, sub, sub_cls in kpis:
        with col:
            st.markdown(f"""
            <div class="kpi-card">
                <div class="kpi-icon">{icon}</div>
                <div class="kpi-label">{label}</div>
                <div class="kpi-value">{value}</div>
                <div class="kpi-sub {sub_cls}">{sub}</div>
            </div>
            """, unsafe_allow_html=True)

    st.markdown("<div style='margin:10px 0'></div>", unsafe_allow_html=True)

    # ── Main 3 columns ───────────────────────────────────────────
    left, center, right = st.columns([1.1, 2.1, 1.1])

    # ── LEFT ──
    with left:
        st.markdown('<div class="sec-title">🔥 Google 熱搜關鍵字雲</div>', unsafe_allow_html=True)
        st.markdown(keyword_cloud_html(keywords), unsafe_allow_html=True)

        st.markdown("<div style='margin:10px 0'></div>", unsafe_allow_html=True)

        st.markdown('<div class="sec-title">💬 PTT 八卦板 熱門文章</div>', unsafe_allow_html=True)
        if ptt_goss:
            for p in ptt_goss[:7]:
                t = html_mod.escape(p['title'])
                u = html_mod.escape(p['url'])
                st.markdown(f"""
                <div class="ptt-card">
                    <a class="ptt-title" href="{u}" target="_blank" rel="noopener" title="{t}">{t}</a>
                    <div class="{ptt_push_class(p['push_num'])}">{p['push']}</div>
                </div>""", unsafe_allow_html=True)
        else:
            st.markdown('<div style="color:#3a5a85;padding:16px;text-align:center">PTT 資料暫時無法取得</div>', unsafe_allow_html=True)

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

        st.markdown("<div style='margin:10px 0'></div>", unsafe_allow_html=True)

        st.markdown('<div class="sec-title">📈 PTT 股票板 熱門</div>', unsafe_allow_html=True)
        if ptt_stock:
            for p in ptt_stock[:6]:
                t = html_mod.escape(p['title'])
                u = html_mod.escape(p['url'])
                st.markdown(f"""
                <div class="ptt-card">
                    <a class="ptt-title" href="{u}" target="_blank" rel="noopener" title="{t}">{t}</a>
                    <div class="{ptt_push_class(p['push_num'])}">{p['push']}</div>
                </div>""", unsafe_allow_html=True)
        else:
            st.markdown('<div style="color:#3a5a85;padding:16px;text-align:center">暫時無法取得</div>', unsafe_allow_html=True)

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
        資料來源：Google Trends · RSS (Google 新聞 / CNA / UDN / ETtoday / Yahoo) · PTT &nbsp;｜&nbsp;
        更新時間：{now.strftime('%Y-%m-%d %H:%M:%S')} (台北 UTC+8) &nbsp;｜&nbsp;
        每 5 分鐘自動刷新
    </div>
    """, unsafe_allow_html=True)


main()
