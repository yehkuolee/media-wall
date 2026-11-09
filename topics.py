"""找哏話題：收訊號 → 同一話題串成一群 → 算熱度 → 分類

LINE 找哏小幫手（tipbot/agent.py）和媒體牆的分類分頁（app.py）共用這一份，兩邊篩選邏輯才會一樣。
抓資料的函式在 app.py，由呼叫端用 use() 傳進來（Streamlit 跑 app.py 時模組名是 __main__，不能直接 import app）。
"""
from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

log = logging.getLogger("tipbot")
mw = None  # app.py 模組


def use(app_module):
    global mw
    mw = app_module


def _norm(t: str) -> str:
    return re.sub(r"[\s\W_]+", "", t or "")


def _threads_count(s: str) -> int:
    m = re.search(r"([\d.]+)\s*(萬)?", (s or "").replace("\xa0", " "))
    if not m:
        return 0
    return int(float(m.group(1)) * (10000 if m.group(2) else 1))


def collect() -> list[dict]:
    """回傳訊號清單，每筆 {kind, title, link, source, rank, extra}"""
    sig = []
    news = mw.fetch_news()
    mw.record_ranks(news)
    per_src: dict[str, int] = {}
    for n in news:
        src = n["source"]
        per_src[src] = per_src.get(src, 0) + 1
        hot = src in mw.HOT_SOURCES and not n.get("fallback")
        if hot and per_src[src] > 10:
            continue
        if not hot and per_src[src] > 15:
            continue
        sig.append({"kind": "news" if hot else "latest", "title": n["title"], "link": n["link"],
                    "source": src, "rank": per_src[src]})
    for i, kw in enumerate(mw.fetch_keywords()[:25], 1):
        if kw not in mw.FALLBACK_KEYWORDS:
            sig.append({"kind": "google", "title": kw, "link": "https://trends.google.com.tw/trending?geo=TW",
                        "source": "Google 熱搜", "rank": i})
    th = mw.fetch_threads_trending()
    for i, t in enumerate(th.get("topics", [])[:15], 1):
        sig.append({"kind": "threads", "title": t.get("title", ""), "link": t.get("link", ""),
                    "source": "Threads", "rank": i, "extra": _threads_count(t.get("count", "")),
                    "desc": t.get("description", "")})
    for i, p in enumerate(mw.fetch_ptt(limit=10), 1):
        title = re.sub(r"^(Re:\s*)?\[[^\]]+\]\s*", "", p["title"])
        if p["title"].startswith("[公告]"):
            continue
        sig.append({"kind": "ptt", "title": title, "link": p["url"], "source": "PTT 八卦板", "rank": i,
                    "extra": p["push_num"]})
    return [s for s in sig if s["title"]]


def _related(a: dict, b: dict) -> bool:
    """兩個訊號是不是同一話題"""
    short = {"google", "threads"}
    ga, gb = mw._grams(a["title"]), mw._grams(b["title"])
    if not ga or not gb:
        return False
    if a["kind"] in short or b["kind"] in short:
        s, l = (a, b) if len(_norm(a["title"])) <= len(_norm(b["title"])) else (b, a)
        ns = _norm(s["title"])
        if s["kind"] == "google" and len(ns) >= 2 and ns in _norm(l["title"]):
            return True  # 熱搜字整個出現在標題裡
        inter = ga & gb
        return len(inter) >= 3 and len(inter) / min(len(ga), len(gb)) >= 0.5
    inter = ga & gb
    ratio = len(inter) / min(len(ga), len(gb))
    if a["source"] == b["source"]:  # 同一家兩篇同一件事（例如營養午餐香鬆），門檻拉高
        return len(inter) >= 4 and ratio >= 0.3
    return len(inter) >= 4 and ratio >= 0.2


def cluster(sig: list[dict]) -> list[list[dict]]:
    parent = list(range(len(sig)))

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(sig)):
        for j in range(i + 1, len(sig)):
            if _related(sig[i], sig[j]):
                parent[root(i)] = root(j)
    groups: dict[int, list] = {}
    for i, s in enumerate(sig):
        groups.setdefault(root(i), []).append(s)
    return list(groups.values())


def score_topic(members: list[dict], surge_links: dict) -> dict:
    hot = {m["source"] for m in members if m["kind"] == "news"}
    best_rank = {}
    for m in members:
        if m["kind"] == "news":
            best_rank[m["source"]] = min(best_rank.get(m["source"], 99), m["rank"])
    score, why = 0, []
    if hot:
        score += 2 * len(hot) + sum(1 for r in best_rank.values() if r <= 3)
        why.append("、".join(f"{s.replace(' 新聞', '').replace('新聞網', '').replace('熱門', '')}#{r}"
                             for s, r in sorted(best_rank.items(), key=lambda x: x[1])))
    surges = [r for m in members for r in surge_links.get(m["link"], [])]
    if surges:
        score += 3
        why.append("🚀 " + surges[0])
    g = [m for m in members if m["kind"] == "google"]
    if g:
        r = min(m["rank"] for m in g)
        score += 3 if r <= 10 else 2
        why.append(f"Google 熱搜 #{r}")
    t = [m for m in members if m["kind"] == "threads"]
    if t:
        c = max(m.get("extra", 0) for m in t)
        score += 3 if c >= 10000 else 2
        why.append(f"Threads 趨勢（{c:,} 則）" if c else "Threads 趨勢")
    p = [m for m in members if m["kind"] == "ptt"]
    if p:
        push = max(m.get("extra", 0) for m in p)
        score += 2 if push >= 50 else 1
        why.append("PTT " + ("爆" if push >= 100 else f"{push} 推"))
    if any(m["kind"] == "latest" for m in members) and not hot:
        why.append("中央社/三立即時")

    order = {"news": 0, "latest": 1, "ptt": 2, "threads": 3, "google": 4}
    rep = min(members, key=lambda m: (order[m["kind"]], m["rank"]))
    links, seen = [], set()
    for m in sorted(members, key=lambda m: (order[m["kind"]], m["rank"])):
        if m["kind"] in ("news", "latest") and m["source"] not in seen:
            seen.add(m["source"])
            links.append({"source": m["source"], "title": m["title"], "link": m["link"]})
    th = next((m for m in t), None)
    return {"title": rep["title"], "link": rep["link"], "score": score, "why": why,
            "sources": len(hot), "links": links[:3], "threads_desc": th.get("desc", "") if th else "",
            "kinds": sorted({m["kind"] for m in members})}


# ── 新聞分類：先看網址，看不出來再整批問 Gemini（問過的標題存 state/categories.json） ──────

CATS = ["政治", "社會", "生活", "娛樂", "財經", "國際", "體育", "科技", "寵物"]
CAT_ICON = {"政治": "🏛", "社會": "🚨", "生活": "🏠", "娛樂": "🎬", "財經": "💰", "國際": "🌏",
            "體育": "⚾", "科技": "💻", "寵物": "🐾"}
CAT_ALIAS = {**{c: c for c in CATS}, "健康": "生活", "醫療": "生活", "地方": "生活", "消費": "生活",
             "兩岸": "國際", "大陸": "國際", "中國": "國際", "全球": "國際", "影劇": "娛樂", "星光": "娛樂",
             "運動": "體育", "股市": "財經", "台股": "財經", "股票": "財經", "房產": "財經", "3c": "科技",
             "毛孩": "寵物", "動物": "寵物"}
# 網址路徑（或子網域）裡的分類字 → 類別；自由、東森、壹蘋、中央社、三立娛樂都靠這個
URL_CAT = {
    "politics": "政治", "def": "政治", "aipl": "政治",
    "society": "社會", "asoc": "社會",
    "life": "生活", "living": "生活", "local": "生活", "novelty": "生活", "health": "生活", "istyle": "生活",
    "ahel": "生活", "aloc": "生活", "acul": "生活", "fun": "生活", "car": "生活",
    "ent": "娛樂", "entertainment": "娛樂", "star": "娛樂", "amov": "娛樂",
    "ec": "財經", "business": "財經", "finance": "財經", "money": "財經", "afe": "財經", "asc": "財經", "aie": "財經",
    "world": "國際", "international": "國際", "china": "國際", "aopl": "國際", "acn": "國際",
    "sports": "體育", "sport": "體育", "aspt": "體育",
    "3c": "科技", "tech": "科技", "gadget": "科技", "ait": "科技",
    "pet": "寵物", "pets": "寵物",
}
PET_RE = re.compile(r"(毛孩|寵物|貓咪|狗狗|浪浪|浪貓|浪犬|柴犬|柯基|動保|認養|飼主|汪星人|喵星人|貓奴)")


def _url_cat(link: str) -> str:
    m = re.match(r"https?://([^/]+)(/[^?#]*)?", link or "")
    if not m:
        return ""
    host, path = m.group(1).lower(), (m.group(2) or "").lower()
    if "yahoo" in host or "udn.com" in host or "chinatimes" in host:
        return ""  # 網址沒有分類（或代碼還沒確認），交給 Gemini
    sub = host.split(".")[0]
    if sub in URL_CAT:
        return URL_CAT[sub]
    for seg in path.split("/"):
        if seg in URL_CAT:
            return URL_CAT[seg]
    return ""


def _ask_cats(titles: list) -> dict:
    key = mw._gemini_key()
    if not key or not titles:
        return {}
    prompt = ("把每則台灣新聞標題歸到一個類別：" + "、".join(CATS) + "。\n"
              "國際包含兩岸與中國大陸；生活包含健康、醫療、交通、天氣、消費、地方；跟貓狗或動物有關的歸寵物；"
              "股市、房市、產業歸財經；選舉、政策、政黨歸政治；犯罪、意外、司法歸社會；明星、網紅、影視歸娛樂。\n\n"
              + "\n".join(f"{i}. {t}" for i, t in enumerate(titles, 1)))
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                 "responseSchema": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                                     "n": {"type": "INTEGER"}, "cat": {"type": "STRING", "enum": CATS}},
                                     "required": ["n", "cat"]}}}}
    for model in mw.ET_MODELS:
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              headers={"x-goog-api-key": key}, json=body, timeout=40)
            if r.status_code != 200:
                continue
            arr = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
            return {titles[x["n"] - 1]: x["cat"] for x in arr if 1 <= x["n"] <= len(titles) and x["cat"] in CATS}
        except Exception:
            continue
    log.warning("Gemini 分類失敗，這輪沒分類的先跳過")
    return {}



def categorize(topics: list, groups: dict, memo: dict) -> bool:
    """每個話題：成員網址分類投票，最多票的贏；都看不出來就用 Gemini 判斷代表標題。
    memo 是問過 Gemini 的「標題→類別」，有新答案會加進去並回傳 True（呼叫端自己存）"""
    todo = []
    for t in topics:
        if PET_RE.search(t["title"]):
            t["cat"] = "寵物"
            continue
        votes = {}
        for m in groups[id(t)]:
            c = _url_cat(m["link"])
            if c:
                votes[c] = votes.get(c, 0) + 1
        if votes:
            t["cat"] = max(votes, key=votes.get)
        elif t["title"] in memo:
            t["cat"] = memo[t["title"]]
        else:
            t["cat"] = ""
            todo.append(t)
    if not todo:
        return False
    ans = _ask_cats([t["title"] for t in todo])
    for t in todo:
        t["cat"] = ans.get(t["title"], "")
    if ans:
        if len(memo) > 3000:
            memo.clear()
        memo.update(ans)
    return bool(ans)


# ── 只有 Google 熱搜字、沒有媒體新聞的話題：用熱搜字搜 Google 新聞（近一天），附前 3 則 ──────
GNEWS_URL = "https://news.google.com/rss/search?q={}+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
GNEWS_TTL = 1800
_gnews_cache: dict[str, tuple[float, list]] = {}


def google_news(word: str) -> list[dict]:
    """[{source, title, link, gnews}]；問過的 30 分鐘內不重問"""
    hit = _gnews_cache.get(word)
    if hit and time.time() - hit[0] < GNEWS_TTL:
        return hit[1]
    items = []
    try:
        r = requests.get(GNEWS_URL.format(quote(word)), headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        for it in list(ET.fromstring(r.content).iter("item"))[:3]:
            src = it.findtext("source") or "Google 新聞"
            title = re.sub(r"\s+-\s+" + re.escape(src) + r"$", "", it.findtext("title") or "")  # 標題尾巴的「 - 媒體名」
            if title and it.findtext("link"):
                items.append({"source": src, "title": title, "link": it.findtext("link"), "gnews": True})
    except Exception:
        log.warning("Google 新聞搜尋失敗：%s", word)
    if len(_gnews_cache) > 500:
        _gnews_cache.clear()
    _gnews_cache[word] = (time.time(), items)
    return items


def build_topics(memo: dict) -> tuple[list, list, int, bool]:
    """回傳 (話題前 50 名依熱度排, [(話題, 成員訊號)], 訊號筆數, memo 有沒有更新)"""
    sig = collect()
    surge_links: dict[str, list] = {}
    for s in mw.detect_surges():
        surge_links.setdefault(s["news"].get("link"), []).extend(s["reasons"])
    pairs = [(score_topic(g, surge_links), g) for g in cluster(sig)]
    # 只有單一來源、又只是「最新列表」的不算哏
    pairs = [p for p in pairs if p[0]["score"] >= 2]
    pairs.sort(key=lambda p: -p[0]["score"])
    pairs = pairs[:50]  # 多留一些，分類查詢時每類才有東西
    topics = [t for t, _ in pairs]
    for t in topics:
        if "google" in t["kinds"] and not t["links"]:
            t["links"] = google_news(t["title"])
    changed = categorize(topics, {id(t): g for t, g in pairs}, memo)
    return topics, pairs, len(sig), changed
