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
import io
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

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
import topics as tp  # noqa: E402  找哏話題邏輯（跟媒體牆分類分頁共用）

import requests  # noqa: E402

# ── 設定 ───────────────────────────────────────────────────────────
PORT = int(os.environ.get("TIPBOT_PORT", "8790"))
LOOP_EVERY = 600                 # 每 10 分鐘算一次
ACTIVE_HOURS = (5, 24)           # 05:00～24:00 才收集與推播
PUSH_MIN_SCORE = int(os.environ.get("TIPBOT_PUSH_MIN_SCORE", "12"))   # 超強訊號門檻
PUSH_PER_DAY = 3                 # 主動推播每天上限
PUSH_PER_MONTH = 60              # 每月上限（LINE 輕用量免費 200 則，留安全空間）
LIST_N = 8                       # 回覆清單最多幾則
WALL_URL = os.environ.get("TIPBOT_WALL_URL", "https://media-wall-kyupypbavjip4jw2tsgida.streamlit.app/")  # 媒體牆網頁版
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


# ── 收訊號、串話題、算熱度、分類：在 ../topics.py（媒體牆分類分頁共用） ─────────────

tp.use(mw)
from topics import CATS, CAT_ICON, CAT_ALIAS, _threads_count  # noqa: E402


def build_tips() -> dict:
    t0 = time.time()
    memo = _read("categories.json", {})
    topics, pairs, n_sig, changed = tp.build_topics(memo)
    if changed:
        _write("categories.json", memo)
    # Threads 熱搜：串進話題的直接沿用該話題的分類與 ET 比對，沒排進前 50 的另外一起比對
    by_threads = {m["title"]: t for t, g in pairs for m in g if m["kind"] == "threads"}
    th = mw.fetch_threads_trending()
    th_items = [x for x in th.get("topics", [])[:15] if x.get("title")]
    et = mw.ettoday_matches([{"title": t["title"], "link": t["link"]} for t in topics]
                            + [{"title": x["title"], "link": x.get("link", "")} for x in th_items
                               if x["title"] not in by_threads])
    for t in topics:
        m = et.get(t["title"])
        t["et"] = {"title": m["title"], "link": m["link"]} if m else None
    threads = []
    for i, x in enumerate(th_items, 1):
        t = by_threads.get(x["title"])
        m = None if t else et.get(x["title"])
        threads.append({"rank": i, "title": x["title"], "desc": x.get("description", ""),
                        "count": _threads_count(x.get("count", "")), "link": x.get("link", ""),
                        "cat": t.get("cat", "") if t else "",
                        "et": t["et"] if t else ({"title": m["title"], "link": m["link"]} if m else None)})
    data = {"updated_at": now().isoformat(), "topics": topics,
            "threads": threads, "threads_at": th.get("updated_at"),
            "surges": [{"title": s["news"]["title"], "link": s["news"].get("link"), "reasons": s["reasons"],
                        "et": (lambda m: {"title": m["title"], "link": m["link"]} if m else None)(
                            et.get(s["news"]["title"]))}
                       for s in mw.detect_surges()]}
    _write("latest.json", data)
    log.info("算完 %d 個話題（訊號 %d 筆，%.1f 秒）", len(topics), n_sig, time.time() - t0)
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
            if l.get("gnews"):  # 熱搜字搜到的新聞，附標題才知道在講什麼
                lines.append(f"   {l['source']}｜{l['title'][:40]}")
            lines.append(f"   {l['source']}：{l['link']}")
        if not t["links"] and t["link"]:
            lines.append(f"   {t['link']}")
    return "\n".join(lines)


def fmt_threads(i, x):
    lines = [f"{i}. {x['title']}",
             f"   {'✅ ET 已寫' if x.get('et') else '❌ ET 未寫'}｜Threads #{x['rank']}"
             + (f"｜{x['count']:,} 則貼文" if x.get("count") else "")]
    if x.get("desc"):
        lines.append("   " + x["desc"][:60])
    if x.get("et"):
        lines.append(f"   ET：{x['et']['link']}")
    if x.get("link"):
        lines.append(f"   Threads：{x['link']}")
    return "\n".join(lines)


def _threads_head(data, base, cat):
    at = data.get("threads_at")
    at = datetime.fromisoformat(at).strftime("%H:%M") if at else "?"
    tag = f"{CAT_ICON.get(cat, '')} {cat}類" if cat else ""
    what = "ETtoday 還沒寫的 Threads 熱搜" if base.endswith("未寫") else "Threads 熱搜"
    return f"🧵 {tag}{what}｜{at} 抓取（每小時更新）"


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
    if base.startswith("threads"):
        items = _filter(data.get("threads", []), base, cat)
        head = _threads_head(data, base, cat)
        if not items:
            return f"{head}\n目前沒有{'還沒寫的' if base.endswith('未寫') else ''}話題 👍{rest}"
        return "\n\n".join([head + rest] + [fmt_threads(i, x) for i, x in enumerate(items, 1)])[:4900]
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


def threads_bubble(i, x):
    why = [f"Threads 熱搜 #{x['rank']}" + (f"｜{x['count']:,} 則貼文" if x.get("count") else "")]
    if x.get("desc"):
        why.append(x["desc"][:80])
    links = ([("看 ETtoday", x["et"]["link"])] if x.get("et") else []) + [("看 Threads", x.get("link", ""))]
    return _bubble(i, x["title"], x.get("et"), why, links, x.get("cat", ""))


def topic_bubble(i, t):
    why = [f"熱度 {t['score']}｜" + "｜".join(t["why"][:1])] + t["why"][1:]
    if t.get("threads_desc") and t["sources"] == 0:
        why.append(t["threads_desc"][:60])
    why += [f"📰 {l['title'][:40]}（{l['source']}）" for l in t["links"] if l.get("gnews")]
    links = []
    if t.get("et"):
        links.append(("看 ETtoday", t["et"]["link"]))
    links += [(f"看{_short(l['source'])}", l["link"]) for l in t["links"]]
    if not t["links"] and t["link"]:
        links.append(("看來源", t["link"]))
    return _bubble(i, t["title"], t.get("et"), why, links, t.get("cat", ""))


LOOSE_BASE = [("暴衝", ("暴衝",)), ("未寫", ("未寫", "沒寫", "漏")), ("哏", ("哏", "最新"))]  # 前面的優先
THREADS_WORDS = ("thread", "脆")


def _loose_cmd(t: str):
    """私訊用：一句話裡只要有指令字就照做，例如「今天政治有什麼沒寫的」→「未寫:政治」"""
    if "skill" in t or "技能" in t:  # 「skill怎麼裝」（t 已轉小寫）
        return "skill"
    if "指令" in t:  # 「給我改寫指令」
        return "指令"
    text_mode = "清單" in t or "文字" in t
    cat = next((CAT_ALIAS[a] for a in sorted(CAT_ALIAS, key=len, reverse=True) if a in t), "")
    base = next((b for b, words in LOOSE_BASE if any(w in t for w in words)), "")
    if base == "暴衝":
        cmd = "暴衝"
    elif any(w in t for w in THREADS_WORDS):  # 「脆上有什麼沒寫的」→「threads未寫」
        cmd = "threads" + ("未寫" if base == "未寫" else "") + (f":{cat}" if cat else "")
    elif base or cat or text_mode:
        cmd = (base or "哏") + (f":{cat}" if cat else "")
    else:
        return "help" if any(w in t for w in ("說明", "help", "怎麼用")) else None
    return f"清單/{cmd}" if text_mode else cmd


def parse_cmd(text: str, loose: bool = False):
    """「哏」「未寫」「暴衝」「說明」，或加分類：「政治」「哏 政治」「未寫 政治」「政治未寫」→「未寫:政治」
    loose（私訊）：內容包含指令字就算；群組維持嚴格比對，免得大家聊天提到「政治」「最新」就被插嘴"""
    t = re.sub(r"\s+", "", text.strip().lower()).replace("梗", "哏").replace("哽", "哏")  # 常見錯字也認
    if t in COMMANDS:
        return COMMANDS[t]
    if loose:
        return _loose_cmd(t)
    for w in ("清單", "文字"):  # 「清單 政治」「清單 未寫」→「清單/哏:政治」：同樣內容的純文字版
        if w in t:
            inner = parse_cmd(t.replace(w, "", 1))
            return f"清單/{inner}" if inner and _split(inner)[0] in ("哏", "未寫", "暴衝", "threads") else None
    for alias in sorted(CAT_ALIAS, key=len, reverse=True):
        if alias in t:
            rest = t.replace(alias, "", 1)
            base = COMMANDS.get(rest) if rest else "哏"
            if base in ("哏", "未寫", "threads"):
                return f"{base}:{CAT_ALIAS[alias]}"
            return None
    return None


def _split(cmd: str):
    base, _, cat = cmd.partition(":")
    return base, cat


def _filter(topics, base, cat):
    if base.endswith("未寫"):  # 「未寫」或「threads未寫」
        topics = [t for t in topics if not t.get("et")]
    if cat:
        topics = [t for t in topics if t.get("cat") == cat]
    return topics


def _title(base, cat):
    tag = f"{CAT_ICON.get(cat, '')} {cat}類" if cat else ""
    if base == "未寫":
        return f"🔥 {tag}ETtoday 還沒寫的哏" if cat else "🔥 ETtoday 還沒寫的哏"
    return f"🔥 {tag}熱門哏" if cat else "🔥 最新哏"


PROMPT_FILE = HERE / "指令.md"  # 社群貼文改寫新聞稿的 AI 指令（不進 git，repo 是公開的）
# 同一份指令也包成 Claude Skill：https://tip.etwriter-ai.com/skill（每次下載都從 指令.md 現做，兩邊永遠一致）
SKILL_NAME = "ettoday-social-rewrite"
SKILL_URL = os.environ.get("TIPBOT_PUBLIC_URL", "https://tip.etwriter-ai.com") + "/skill"  # LINE 按鈕不收 .zip 結尾的網址；下載下來檔名還是 .zip
SKILL_HEAD = """---
name: ettoday-social-rewrite
description: 把臉書社團、Threads、PTT、Instagram、Dcard等社群貼文改寫成ETtoday網路即時新聞稿（標題＋6段＋2個小標，事實紀律、用語規則）。使用者貼上社群貼文素材（發文者、內文、留言、圖片說明），或要求「改寫成新聞」「轉成新聞稿」「寫成即時新聞」時使用。
---

"""


def skill_md() -> str:
    return SKILL_HEAD + PROMPT_FILE.read_text().strip() + "\n"


def skill_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{SKILL_NAME}/SKILL.md", skill_md())
    return buf.getvalue()


def prompt_messages() -> list:
    """「指令」：說明一則＋指令全文一則（單獨一則，長按就能整則複製，不夾別的字）"""
    try:
        text = PROMPT_FILE.read_text().strip()
    except FileNotFoundError:
        return [{"type": "text", "text": "指令還沒設定好，請找管理者"}]
    guide = ("📋社群貼文改寫新聞稿的AI指令（給Gemini、ChatGPT用）\n"
             "長按下一則訊息→複製，貼給AI，再貼上貼文素材\n"
             "用Claude的話，傳「SKILL」裝成技能更方便")
    return [{"type": "text", "text": guide}, {"type": "text", "text": text[:5000]}]


SKILL_STEPS = ["1.用電腦下載SKILL（zip檔）",
               "2.claude.ai→設定（Settings）→功能（Capabilities），先打開「程式碼執行」",
               "3.同一頁的技能（Skills）按上傳，選剛下載的zip（不用解壓縮）",
               "4.開新對話，貼上貼文素材，說「改寫成新聞稿」就會照規則寫"]


def skill_text() -> str:
    return ("🧩社群貼文改寫新聞稿SKILL（Claude技能）\n裝一次，之後貼素材就自動照ETtoday規則改寫\n\n"
            + "\n".join(SKILL_STEPS) + f"\n\n下載：{SKILL_URL}")


def skill_messages() -> list:
    """「SKILL」：卡片＋下載、一鍵複製連結兩顆按鈕（電腦版 LINE 點下載不方便時，複製連結貼到瀏覽器）"""
    body = [{"type": "text", "text": "🧩社群貼文改寫新聞稿SKILL", "weight": "bold", "size": "md", "wrap": True},
            {"type": "text", "text": "Claude技能，裝一次，之後貼素材就自動照ETtoday規則改寫",
             "size": "xs", "color": "#555555", "wrap": True}]
    body += [{"type": "text", "text": x, "size": "sm", "wrap": True, "margin": "md" if k == 0 else "sm"}
             for k, x in enumerate(SKILL_STEPS)]
    bubble = {"type": "bubble", "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body},
              "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                  {"type": "button", "style": "primary", "color": "#c41230", "height": "sm",
                   "action": {"type": "uri", "label": "下載SKILL", "uri": SKILL_URL}},
                  {"type": "button", "style": "secondary", "height": "sm",
                   "action": {"type": "clipboard", "label": "一鍵複製下載連結", "clipboardText": SKILL_URL}}]}}
    return [{"type": "flex", "altText": "社群貼文改寫新聞稿SKILL", "contents": bubble}]


def web_url(cmd: str) -> str:
    """回覆對應的媒體牆網址：有分類就直接開那一類（列全部，不只未寫），其他開首頁"""
    base, cat = _split(cmd.removeprefix("清單/"))
    if cat in CATS and not base.startswith("threads"):
        return WALL_URL + "?cat=" + quote(cat)
    return WALL_URL


def with_web(messages: list, cmd: str) -> list:
    """每則回覆最後附網頁版連結：最後一則是文字就接在後面，是卡片就多一則文字（一次回覆最多 5 則）"""
    if cmd in ("指令", "skill"):  # 指令全文要能整則複製；SKILL 跟媒體牆無關，都不接網址
        return messages
    line = f"🌐 網頁版：{web_url(cmd)}"
    last = messages[-1]
    if last["type"] == "text" and len(last["text"]) + len(line) < 4990:
        return messages[:-1] + [{**last, "text": last["text"] + "\n\n" + line}]
    return (messages + [{"type": "text", "text": line}])[:5]


def reply_messages(cmd: str) -> list:
    """回覆用：一則標題文字＋一組卡片（最多 10 張）"""
    data = _read("latest.json", None)
    base, cat = _split(cmd)
    if cmd == "help":
        return [{"type": "text", "text": HELP}]
    if cmd == "指令":
        return prompt_messages()
    if cmd == "skill":
        return skill_messages()
    if cmd == "清單" or cmd.startswith("清單/"):  # 純文字直列版，電腦版 LINE 從上往下捲就好
        return [{"type": "text", "text": reply_text(cmd[3:] or "哏")}]
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
    elif base.startswith("threads"):
        items = _filter(data.get("threads", []), base, cat)
        head = _threads_head(data, base, cat) + rest
        bubbles = [threads_bubble(i, x) for i, x in enumerate(items[:10], 1)]
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
        "・分類 → 傳類別名稱，例如「政治」只看該類\n"
        "　（政治、社會、生活、娛樂、財經、國際、體育、科技、寵物）\n"
        "・Threads 或 脆 → Threads 熱搜話題，標出 ETtoday 寫了沒\n"
        "・清單 → 最新哏的純文字版（電腦版 LINE 卡片不好滑時用）\n"
        "　加分類也行，例如「清單 政治」\n"
        "・SKILL → 社群貼文改寫新聞稿的Claude技能，附下載和安裝步驟\n"
        "・指令 → 同一套改寫規則的全文，長按複製貼給Gemini、ChatGPT\n"
        "直接打一句話也可以，例如「政治有什麼還沒寫的」\n"
        "清單每 10 分鐘更新（05:00～24:00）")
OWNER_HELP = ("\n\n管理者限定（只有你看得到）：\n"
              "・名單 → 誰加了好友、各問過幾次\n"
              "・紀錄 → 最近 50 則發問原文\n"
              "　看某人：「紀錄 名字」")

COMMANDS = {"哏": "哏", "梗": "哏", "最新": "哏", "最新哏": "哏", "最新哏清單": "哏", "未寫": "未寫", "沒寫": "未寫", "漏": "未寫",
            "暴衝": "暴衝", "threads": "threads", "thread": "threads", "脆": "threads", "清單": "清單", "文字": "清單", "說明": "help", "help": "help", "指令": "指令", "改寫指令": "指令", "skill": "skill", "skills": "skill", "技能": "skill", "?": "help", "？": "help"}


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


# ── 發問紀錄：每則私訊、群組裡的指令存 state/asks.jsonl，管理者傳「紀錄」「紀錄 名字」查看 ──

def log_ask(ev: dict, text: str, cmd: str | None):
    src = ev.get("source", {})
    if src.get("type") != "user" and not cmd:
        return  # 群組閒聊不記
    rec = {"t": now().isoformat(timespec="seconds"), "uid": src.get("userId", ""),
           "src": src.get("type"), "text": text[:200], "cmd": cmd or "help"}
    with _members_lock, open(STATE / "asks.jsonl", "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def asks_text(who: str = "") -> str:
    try:
        lines = (STATE / "asks.jsonl").read_text().splitlines()
    except FileNotFoundError:
        return "還沒有發問紀錄"
    mem = _read("members.json", {})
    rows = []
    for ln in lines:
        r = json.loads(ln)
        name = mem.get(r["uid"], {}).get("name") or "（未知名稱）"
        if not who or who in name:
            rows.append((r, name))
    if not rows:
        return f"找不到「{who}」的發問紀錄"
    out = [f"📝 發問紀錄{'：' + who if who else ''}（共 {len(rows)} 則，列最近 50 則）"]
    for r, name in rows[-50:][::-1]:
        grp = "〔群組〕" if r["src"] != "user" else ""
        out.append(f"{_md(r['t'])} {name}{grp}：{r['text']}")
    if not who:
        out.append("看某人：傳「紀錄 名字」")
    return "\n".join(out)[:4900]


def handle_event(ev: dict):
    text = ev.get("message", {}).get("text", "").strip() if ev.get("type") == "message" else ""
    try:
        _handle_event(ev)
    finally:
        try:
            cmd = parse_cmd(text, ev.get("source", {}).get("type") == "user") if text else None
            track_member(ev, cmd)
            if text:
                log_ask(ev, text, cmd)
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
        return line_api("reply", {"replyToken": ev["replyToken"], "messages": with_web([{"type": "text", "text": HELP}], "help")})
    if ev.get("type") != "message" or ev.get("message", {}).get("type") != "text":
        return
    text = ev["message"]["text"].strip()
    if text == "名單" and src.get("type") == "user" and uid == _read("owner.json", {}).get("userId"):
        return line_api("reply", {"replyToken": ev["replyToken"], "messages": [{"type": "text", "text": members_text()}]})
    if text.split()[:1] == ["紀錄"] and src.get("type") == "user" and uid == _read("owner.json", {}).get("userId"):
        return line_api("reply", {"replyToken": ev["replyToken"],
                                  "messages": [{"type": "text", "text": asks_text(text[2:].strip())}]})
    cmd = parse_cmd(text, loose=src.get("type") == "user")
    if not cmd:
        if src.get("type") != "user":
            return  # 群組裡別人聊天不插嘴
        cmd = "help"
    log.info("回覆 %s：%s", src.get("type"), text)
    if cmd == "help" and src.get("type") == "user" and uid == _read("owner.json", {}).get("userId"):
        return line_api("reply", {"replyToken": ev["replyToken"], "messages": with_web([{"type": "text", "text": HELP + OWNER_HELP}], "help")})
    r = line_api("reply", {"replyToken": ev["replyToken"], "messages": with_web(reply_messages(cmd), cmd)})
    if r is not None and r.status_code == 400:  # 卡片格式被 LINE 退回時，改回純文字
        line_api("reply", {"replyToken": ev["replyToken"],
                           "messages": with_web([{"type": "text", "text": HELP if cmd == "help" else
                                                          skill_text() if cmd == "skill" else reply_text(cmd)}], cmd)})


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
        if self.path.split("?")[0] in ("/skill", "/skill.zip"):
            try:
                b = skill_zip()
            except FileNotFoundError:
                return self._send(404, "not ready")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{SKILL_NAME}.zip"')
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
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
