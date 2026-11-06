"""找哏監測 Agent（LINE Bot）

一支常駐程式，做兩件事：
1. 背景每 10 分鐘（05:00～24:00）收集訊號 → 把同一話題串成一群 → 算熱度 → 比對 ETtoday 寫了沒
   → 存成「最新哏清單」（state/latest.json）；超強訊號且 ETtoday 還沒寫才主動推播
2. 收 LINE webhook：有人傳「哏」「未寫」「暴衝」就回覆現成清單（回覆不扣 LINE 免費則數）

資料抓取全部沿用媒體熱度監測牆 app.py 的函式（import app 不會跑畫面）。
金鑰放 tipbot/.env（見 README.md）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE = HERE / "state"
STATE.mkdir(exist_ok=True)


def _load_env():
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
sys.path.insert(0, str(ROOT))
import contextlib  # noqa: E402

_NULL = open(os.devnull, "w")  # 不在 streamlit 裡跑會一直印「No runtime found」等警告，導到黑洞（不能關檔）
with contextlib.redirect_stderr(_NULL):
    import app as mw  # noqa: E402  媒體牆的抓資料函式

import requests  # noqa: E402

# ── 設定 ───────────────────────────────────────────────────────────
PORT = int(os.environ.get("TIPBOT_PORT", "8790"))
LOOP_EVERY = 600                 # 每 10 分鐘算一次
ACTIVE_HOURS = (5, 24)           # 05:00～24:00 才收集與推播
PUSH_MIN_SCORE = int(os.environ.get("TIPBOT_PUSH_MIN_SCORE", "12"))   # 超強訊號門檻
PUSH_PER_DAY = 3                 # 主動推播每天上限
PUSH_PER_MONTH = 60              # 每月上限（LINE 輕用量免費 200 則，留安全空間）
LIST_N = 8                       # 回覆清單最多幾則
SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
TZ = mw.TW_TZ

log = logging.getLogger("tipbot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%m-%d %H:%M:%S")


def now():
    return datetime.now(TZ)


def _read(name, default):
    try:
        return json.loads((STATE / name).read_text())
    except Exception:
        return default


def _write(name, data):
    tmp = STATE / (name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(STATE / name)


# ── 收集訊號、串成話題 ─────────────────────────────────────────────

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


def categorize(topics: list, groups: dict):
    """每個話題：成員網址分類投票，最多票的贏；都看不出來就用 Gemini 判斷代表標題"""
    memo = _read("categories.json", {})
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
    if todo:
        ans = _ask_cats([t["title"] for t in todo])
        for t in todo:
            t["cat"] = ans.get(t["title"], "")
        if ans:
            if len(memo) > 3000:
                memo = {}
            memo.update(ans)
            _write("categories.json", memo)


def build_tips() -> dict:
    t0 = time.time()
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
    categorize(topics, {id(t): g for t, g in pairs})
    et = mw.ettoday_matches([{"title": t["title"], "link": t["link"]} for t in topics])
    for t in topics:
        m = et.get(t["title"])
        t["et"] = {"title": m["title"], "link": m["link"]} if m else None
    data = {"updated_at": now().isoformat(), "topics": topics,
            "surges": [{"title": s["news"]["title"], "link": s["news"].get("link"), "reasons": s["reasons"],
                        "et": (lambda m: {"title": m["title"], "link": m["link"]} if m else None)(
                            et.get(s["news"]["title"]))}
                       for s in mw.detect_surges()]}
    _write("latest.json", data)
    log.info("算完 %d 個話題（訊號 %d 筆，%.1f 秒）", len(topics), len(sig), time.time() - t0)
    return data


# ── 文字格式 ───────────────────────────────────────────────────────

def _ago(iso):
    try:
        mins = int((now() - datetime.fromisoformat(iso)).total_seconds() // 60)
    except Exception:
        return ""
    return "剛剛" if mins < 1 else f"{mins} 分鐘前" if mins < 60 else f"{mins // 60} 小時前"


def fmt_topic(i, t, with_links=True):
    lines = [f"{i}. {t['title']}",
             f"   {'✅ ET 已寫' if t.get('et') else '❌ ET 未寫'}｜熱度 {t['score']}",
             "   " + "｜".join(t["why"])]
    if t.get("threads_desc") and t["sources"] == 0:
        lines.append("   " + t["threads_desc"][:60])
    if with_links:
        if t.get("et"):
            lines.append(f"   ET：{t['et']['link']}")
        for l in t["links"][:2]:
            lines.append(f"   {l['source']}：{l['link']}")
        if not t["links"] and t["link"]:
            lines.append(f"   {t['link']}")
    return "\n".join(lines)


def reply_text(cmd: str) -> str:
    data = _read("latest.json", None)
    if not data:
        return "還在暖機，第一份清單大約 1 分鐘後好，等一下再傳「哏」🙏"
    head_time = datetime.fromisoformat(data["updated_at"]).strftime("%H:%M")
    rest = "" if ACTIVE_HOURS[0] <= now().hour < ACTIVE_HOURS[1] else "\n（00:00～05:00 休息中，這是最後一次的清單）"
    if cmd == "暴衝":
        s = data.get("surges", [])
        if not s:
            return f"🚀 暴衝｜{head_time} 更新\n目前沒有排名暴衝的新聞（剛重開要約 30 分鐘才有名次紀錄）{rest}"
        out = [f"🚀 暴衝｜{head_time} 更新（{_ago(data['updated_at'])}）{rest}"]
        for i, x in enumerate(s, 1):
            out.append(f"{i}. {x['title']}\n   {'✅ ET 已寫' if x.get('et') else '❌ ET 未寫'}｜"
                       + "；".join(x["reasons"]) + f"\n   {x['link']}")
        return "\n\n".join(out)
    base, cat = _split(cmd)
    topics = _filter(data["topics"], base, cat)
    title = _title(base, cat)
    if not topics:
        return f"{title}｜{head_time} 更新\n目前沒有{'還沒寫的' if base == '未寫' else ''}哏 👍{rest}"
    out = [f"{title}｜{head_time} 更新（{_ago(data['updated_at'])}）{rest}"]
    out += [(f"[{t['cat']}] " if t.get("cat") else "") + fmt_topic(i, t) for i, t in enumerate(topics[:LIST_N], 1)]
    text = "\n\n".join(out)
    return text[:4900]


# ── LINE 卡片（Flex Message）：網址藏在按鈕後面，比一長串網址好讀 ─────────

def _short(src: str) -> str:
    return (src.replace(" 新聞", "").replace("新聞網", "").replace("熱門", "").replace("CNA ", "")
            .replace("時報", "").replace("新聞", ""))[:8]


def _bubble(i, title, et, why_lines, links, cat=""):
    body = [
        {"type": "text", "text": f"#{i}" + (f"｜{CAT_ICON.get(cat, '')} {cat}" if cat else ""),
         "size": "xs", "color": "#999999"},
        {"type": "text", "text": title, "weight": "bold", "size": "md", "wrap": True, "maxLines": 4},
        {"type": "text", "text": "✅ ETtoday 已寫" if et else "❌ ETtoday 未寫", "size": "sm",
         "weight": "bold", "color": "#1a7f37" if et else "#c41230", "margin": "md"},
    ]
    for w in why_lines:
        body.append({"type": "text", "text": w, "size": "xs", "color": "#555555", "wrap": True})
    if et:
        body.append({"type": "text", "text": f"ET：{et['title']}", "size": "xs", "color": "#1a7f37",
                     "wrap": True, "maxLines": 2, "margin": "sm"})
    buttons = []
    for label, url in links[:3]:
        if url and url.startswith("http") and len(url) < 1000:
            buttons.append({"type": "button", "style": "link", "height": "sm",
                            "action": {"type": "uri", "label": label[:20], "uri": url}})
    bubble = {"type": "bubble", "size": "kilo",
              "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body}}
    if buttons:
        bubble["footer"] = {"type": "box", "layout": "vertical", "spacing": "none", "contents": buttons}
    return bubble


def topic_bubble(i, t):
    why = [f"熱度 {t['score']}｜" + "｜".join(t["why"][:1])] + t["why"][1:]
    if t.get("threads_desc") and t["sources"] == 0:
        why.append(t["threads_desc"][:60])
    links = []
    if t.get("et"):
        links.append(("看 ETtoday", t["et"]["link"]))
    links += [(f"看{_short(l['source'])}", l["link"]) for l in t["links"]]
    if not t["links"] and t["link"]:
        links.append(("看來源", t["link"]))
    return _bubble(i, t["title"], t.get("et"), why, links, t.get("cat", ""))


def parse_cmd(text: str):
    """「哏」「未寫」「暴衝」「說明」，或加分類：「政治」「哏 政治」「未寫 政治」「政治未寫」→「未寫:政治」"""
    t = re.sub(r"\s+", "", text.strip().lower())
    if t in COMMANDS:
        return COMMANDS[t]
    for alias in sorted(CAT_ALIAS, key=len, reverse=True):
        if alias in t:
            rest = t.replace(alias, "", 1)
            base = COMMANDS.get(rest) if rest else "哏"
            if base in ("哏", "未寫"):
                return f"{base}:{CAT_ALIAS[alias]}"
            return None
    return None


def _split(cmd: str):
    base, _, cat = cmd.partition(":")
    return base, cat


def _filter(topics, base, cat):
    if base == "未寫":
        topics = [t for t in topics if not t.get("et")]
    if cat:
        topics = [t for t in topics if t.get("cat") == cat]
    return topics


def _title(base, cat):
    tag = f"{CAT_ICON.get(cat, '')} {cat}類" if cat else ""
    if base == "未寫":
        return f"🔥 {tag}ETtoday 還沒寫的哏" if cat else "🔥 ETtoday 還沒寫的哏"
    return f"🔥 {tag}熱門哏" if cat else "🔥 最新哏"


def reply_messages(cmd: str) -> list:
    """回覆用：一則標題文字＋一組卡片（最多 10 張）"""
    data = _read("latest.json", None)
    base, cat = _split(cmd)
    if cmd == "help":
        return [{"type": "text", "text": HELP}]
    if not data:
        return [{"type": "text", "text": reply_text(cmd)}]
    head_time = datetime.fromisoformat(data["updated_at"]).strftime("%H:%M")
    rest = "" if ACTIVE_HOURS[0] <= now().hour < ACTIVE_HOURS[1] else "\n（00:00～05:00 休息中，這是最後一次的清單）"
    if cmd == "暴衝":
        items = data.get("surges", [])
        head = f"🚀 暴衝｜{head_time} 更新（{_ago(data['updated_at'])}）{rest}"
        bubbles = [_bubble(i, x["title"], x.get("et"), x["reasons"],
                           ([("看 ETtoday", x["et"]["link"])] if x.get("et") else []) + [("看原文", x["link"])])
                   for i, x in enumerate(items[:10], 1)]
    else:
        items = _filter(data["topics"], base, cat)
        head = f"{_title(base, cat)}｜{head_time} 更新（{_ago(data['updated_at'])}）{rest}"
        bubbles = [topic_bubble(i, t) for i, t in enumerate(items[:10], 1)]
    if not bubbles:
        return [{"type": "text", "text": reply_text(cmd)}]
    return [{"type": "text", "text": head + "\n👉 左右滑動看更多"},
            {"type": "flex", "altText": head.split("\n")[0], "contents": {"type": "carousel", "contents": bubbles}}]


HELP = ("🔥 找哏小幫手\n"
        "傳以下關鍵字：\n"
        "・哏 → 最新哏清單（依熱度排）\n"
        "・未寫 → 只看 ETtoday 還沒寫的\n"
        "・暴衝 → 競媒熱門榜排名衝最快的\n"
        "・分類 → 傳「政治」「未寫 政治」只看該類\n"
        "　（政治、社會、生活、娛樂、財經、國際、體育、科技、寵物）\n"
        "清單每 10 分鐘更新（05:00～24:00）")

COMMANDS = {"哏": "哏", "梗": "哏", "最新": "哏", "未寫": "未寫", "沒寫": "未寫", "漏": "未寫",
            "暴衝": "暴衝", "說明": "help", "help": "help", "?": "help", "？": "help"}


# ── LINE ───────────────────────────────────────────────────────────

def line_api(path, body):
    if not TOKEN:
        log.warning("沒有 LINE_CHANNEL_ACCESS_TOKEN，略過 %s", path)
        return None
    r = requests.post(f"https://api.line.me/v2/bot/message/{path}", json=body, timeout=10,
                      headers={"Authorization": f"Bearer {TOKEN}"})
    if r.status_code != 200:
        log.warning("LINE %s 失敗 %s %s", path, r.status_code, r.text[:200])
    return r


# ── 使用者名單（誰加好友、誰常問）：存 state/members.json，管理者傳「名單」查看 ──────

_members_lock = threading.Lock()
WEEKDAY = "一二三四五六日"


def line_get(path):
    if not TOKEN:
        return None
    try:
        r = requests.get(f"https://api.line.me/v2/bot/{path}", timeout=5,
                         headers={"Authorization": f"Bearer {TOKEN}"})
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _profile_name(src: dict, uid: str) -> str:
    t = src.get("type")
    if t == "group":
        p = line_get(f"group/{src.get('groupId')}/member/{uid}")
    elif t == "room":
        p = line_get(f"room/{src.get('roomId')}/member/{uid}")
    else:
        p = line_get(f"profile/{uid}")
    return (p or {}).get("displayName", "")


def track_member(ev: dict, cmd: str | None):
    """記錄加好友／封鎖／發問；LINE 名稱一週更新一次，不扣推播額度"""
    src = ev.get("source", {})
    uid = src.get("userId", "")
    if not uid:
        return
    typ, ts = ev.get("type"), now().isoformat(timespec="seconds")
    with _members_lock:
        mem = _read("members.json", {})
        m = mem.setdefault(uid, {"name": "", "first_seen": ts, "asks": 0})
        m["last_seen"] = ts
        if typ == "follow":
            m["followed_at"], m["status"] = ts, "好友"
            m.pop("unfollowed_at", None)
        elif typ == "unfollow":
            m["unfollowed_at"], m["status"] = ts, "封鎖"
        elif typ == "message" and cmd:
            m["asks"] = m.get("asks", 0) + 1
            m.setdefault("cmds", {})
            m["cmds"][cmd] = m["cmds"].get(cmd, 0) + 1
        if src.get("type") in ("group", "room"):
            m["in_group"] = True
        need_name = typ != "unfollow" and (not m.get("name") or
                    (now() - datetime.fromisoformat(m.get("name_at", "2000-01-01T00:00:00+08:00"))).days >= 7)
        _write("members.json", mem)
    if need_name:
        name = _profile_name(src, uid)  # 網路請求放在鎖外，不卡其他人
        if name:
            with _members_lock:
                mem = _read("members.json", {})
                if uid in mem:
                    mem[uid].update(name=name, name_at=ts)
                    _write("members.json", mem)
            log.info("使用者 %s：%s", typ, name)


def _md(iso):
    d = datetime.fromisoformat(iso)
    return f"{d.month}/{d.day}({WEEKDAY[d.weekday()]}) {d:%H:%M}"


def members_text() -> str:
    mem = _read("members.json", {})
    if not mem:
        return "還沒有人加入或發問"
    rows = sorted(mem.values(), key=lambda m: m.get("followed_at") or m.get("first_seen", ""), reverse=True)
    friends = [m for m in rows if m.get("status") != "封鎖"]
    out = [f"👥 使用者名單：{len(friends)} 人（封鎖 {len(rows) - len(friends)} 人）"]
    for m in rows[:40]:
        name = m.get("name") or "（未知名稱）"
        tag = "🚫" if m.get("status") == "封鎖" else "・"
        out.append(f"{tag}{name}｜加入 {_md(m.get('followed_at') or m['first_seen'])}"
                   f"｜問 {m.get('asks', 0)} 次｜最近 {_ago(m.get('last_seen', m['first_seen']))}")
    if len(rows) > 40:
        out.append(f"……還有 {len(rows) - 40} 人，完整名單在 tipbot/state/members.json")
    return "\n".join(out)[:4900]


def handle_event(ev: dict):
    text = ev.get("message", {}).get("text", "").strip() if ev.get("type") == "message" else ""
    try:
        _handle_event(ev)
    finally:
        try:
            track_member(ev, parse_cmd(text) if text else None)
        except Exception:
            log.warning("記錄使用者失敗\n%s", traceback.format_exc())


def _handle_event(ev: dict):
    src = ev.get("source", {})
    uid = src.get("userId", "")
    owner = _read("owner.json", {})
    if uid and not owner.get("userId") and src.get("type") == "user":
        _write("owner.json", {"userId": uid, "bound_at": now().isoformat()})
        log.info("綁定推播對象 %s", uid)
    if ev.get("type") == "follow":
        return line_api("reply", {"replyToken": ev["replyToken"], "messages": [{"type": "text", "text": HELP}]})
    if ev.get("type") != "message" or ev.get("message", {}).get("type") != "text":
        return
    text = ev["message"]["text"].strip()
    if text == "名單" and src.get("type") == "user" and uid == _read("owner.json", {}).get("userId"):
        return line_api("reply", {"replyToken": ev["replyToken"], "messages": [{"type": "text", "text": members_text()}]})
    cmd = parse_cmd(text)
    if not cmd:
        if src.get("type") != "user":
            return  # 群組裡別人聊天不插嘴
        cmd = "help"
    log.info("回覆 %s：%s", src.get("type"), text)
    r = line_api("reply", {"replyToken": ev["replyToken"], "messages": reply_messages(cmd)})
    if r is not None and r.status_code == 400:  # 卡片格式被 LINE 退回時，改回純文字
        line_api("reply", {"replyToken": ev["replyToken"],
                           "messages": [{"type": "text", "text": HELP if cmd == "help" else reply_text(cmd)}]})


def verify(body: bytes, sig: str) -> bool:
    if not SECRET:
        return False
    mac = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(mac, sig or "")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, text="OK"):
        b = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send(200, "tipbot ok")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        if not verify(body, self.headers.get("X-Line-Signature")):
            log.warning("簽章不符，拒絕")
            return self._send(403, "bad signature")
        self._send(200)
        try:
            for ev in json.loads(body).get("events", []):
                handle_event(ev)
        except Exception:
            log.error(traceback.format_exc())


# ── 主動推播（超強訊號才推）──────────────────────────────────────────

def maybe_push(data: dict):
    owner = _read("owner.json", {}).get("userId") or os.environ.get("LINE_PUSH_TO", "")
    if not owner:
        return
    st = _read("pushed.json", {"items": [], "month": "", "month_count": 0})
    today, month = now().strftime("%Y-%m-%d"), now().strftime("%Y-%m")
    if st["month"] != month:
        st["month"], st["month_count"] = month, 0
    st["items"] = [x for x in st["items"] if time.time() - x["t"] < 86400 * 2]
    today_count = sum(1 for x in st["items"] if x.get("day") == today)
    pushed_grams = [mw._grams(x["title"]) for x in st["items"]]
    new = []
    for t in data["topics"]:
        if t["score"] < PUSH_MIN_SCORE or t.get("et"):
            continue
        g = mw._grams(t["title"])
        if any(mw._overlap(g, pg) >= 0.3 for pg in pushed_grams):
            continue
        new.append(t)
    room = min(PUSH_PER_DAY - today_count, PUSH_PER_MONTH - st["month_count"])
    if not new or room <= 0:
        return
    new = new[:3]  # 一次推播最多帶 3 個哏，算 1 則
    head = f"🚨 超強訊號｜{now():%H:%M}　ETtoday 還沒寫、熱度很高"
    r = line_api("push", {"to": owner, "messages": [
        {"type": "text", "text": head + "\n（傳「哏」看完整清單）"},
        {"type": "flex", "altText": head, "contents": {"type": "carousel", "contents": [
            topic_bubble(i, t) for i, t in enumerate(new, 1)]}}]})
    if r is not None and r.status_code == 200:
        st["month_count"] += 1
        st["items"] += [{"title": t["title"], "t": time.time(), "day": today} for t in new]
        _write("pushed.json", st)
        log.info("主動推播 %d 個哏（本月第 %d 則）", len(new), st["month_count"])


def loop():
    while True:
        try:
            if ACTIVE_HOURS[0] <= now().hour < ACTIVE_HOURS[1] or not (STATE / "latest.json").exists():
                data = build_tips()
                if ACTIVE_HOURS[0] <= now().hour < ACTIVE_HOURS[1]:
                    maybe_push(data)
        except Exception:
            log.error(traceback.format_exc())
        time.sleep(LOOP_EVERY - (time.time() % LOOP_EVERY))  # 對齊整 10 分鐘


def main():
    if "--once" in sys.argv:  # 測試用：算一次、印出清單
        build_tips()
        print(reply_text("哏"))
        print("\n" + "=" * 40 + "\n")
        print(reply_text("暴衝"))
        return
    if not SECRET or not TOKEN:
        log.warning("tipbot/.env 缺 LINE_CHANNEL_SECRET 或 LINE_CHANNEL_ACCESS_TOKEN")
    threading.Thread(target=loop, daemon=True).start()
    log.info("找哏小幫手啟動，webhook 在 127.0.0.1:%d", PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
