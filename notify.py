"""通知規則：全部是程式比對，不呼叫任何 AI（不花 Claude 額度）。

流程：Python 把「目前的暴衝／熱門話題」整理成事實清單（facts），
交給瀏覽器裡的 notify.js 依使用者設定的門檻比對、發系統通知。
門檻、開關存在使用者自己的瀏覽器（localStorage），每台裝置各自設定。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import pytz
import requests

TOPIC_FLOOR = 4      # 熱度低於這個的話題不送進前端（UI 門檻最低 4）
TOPIC_LIMIT = 15     # 只看熱度前 15 名，避免替一堆冷門話題比對 ETtoday


CATS = ["政治", "社會", "生活", "娛樂", "財經", "國際", "體育", "科技", "寵物"]
NO_CAT = "未分類"


def _surge_cat(link: str, cat_by_link: dict) -> str:
    """暴衝新聞沒有分類：先看它有沒有被串進某個話題（沿用話題的分類），再看網址，都不行就是未分類"""
    import topics as tp
    return cat_by_link.get(link) or tp._url_cat(link) or ""


def build_facts(surges: list, topics: list, all_topics: list | None = None) -> dict:
    """surges: detect_surges() 的結果；topics: 已經標好 et 的話題清單（依熱度排）；
    all_topics: 全部話題（用來替暴衝新聞找分類）"""
    cat_by_link = {}
    for t in (all_topics or topics):
        for l in [{"link": t.get("link")}] + list(t.get("links", [])):
            if l.get("link") and t.get("cat"):
                cat_by_link.setdefault(l["link"], t["cat"])
    return {
        "surges": [
            {"key": f["news"].get("link") or f["news"]["title"], "title": f["news"]["title"],
             "link": f["news"].get("link", ""), "facts": f.get("facts", []),
             "cat": _surge_cat(f["news"].get("link", ""), cat_by_link)}
            for f in surges
        ],
        "topics": [
            {"key": t.get("link") or t["title"], "title": t["title"], "link": t.get("link", ""),
             "score": t["score"], "unwritten": not t.get("et"), "cat": t.get("cat", "")}
            for t in topics[:TOPIC_LIMIT] if t["score"] >= TOPIC_FLOOR
        ],
    }


def script_html(facts: dict) -> str:
    """零高度的 components.html 內容：把事實交給 notify.js 的 mwNotify.feed()"""
    js = (Path(__file__).parent / "notify.js").read_text(encoding="utf-8")
    data = json.dumps(facts, ensure_ascii=False).replace("</", "<\\/")
    return f"<script>\nconst FACTS = {data};\n{js}\n</script>"


# ══ 背景推播（舊機 tipbot 每 10 分鐘呼叫；手機／電腦裝 ntfy App 收通知）══
# 規則與 notify.js 的 evaluate() 是同一套，改門檻或規則時兩邊要一起改。

DEFAULTS = {
    "enabled": True, "dailyCap": 20, "quietOn": True, "quietFrom": 0, "quietTo": 7,
    "jump":   {"on": True,  "minUp": 5, "within": 30},
    "hotNo":  {"on": True,  "minScore": 5},
    "newTop": {"on": False, "top": 3},
    "multi":  {"on": False, "n": 3},
    "hot":    {"on": False, "minScore": 8},
    "cats":   CATS + [NO_CAT],   # 只通知勾選的分類
}
NTFY_URL = "https://ntfy.sh/"


def merged_cfg(user: dict) -> dict:
    c = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in (user or {}).items():
        if isinstance(c.get(k), dict) and isinstance(v, dict):
            c[k].update(v)
        elif k in c:
            c[k] = v
    return c


def evaluate(f: dict, c: dict) -> list:
    """純規則比對 → [{id, title, body, link}]"""
    out = []
    ok = lambda cat: (cat or NO_CAT) in c["cats"]
    for s in f.get("surges", []):
        if not ok(s.get("cat")):
            continue
        for x in s.get("facts", []):
            t = x["type"]
            if t == "jump" and c["jump"]["on"] and x["from"] - x["to"] >= c["jump"]["minUp"] and x["mins"] <= c["jump"]["within"] + 10:
                out.append({"id": f"jump|{s['key']}|{x['src']}", "title": f"🚀 {x['src']} {x['mins']} 分鐘 {x['from']}→{x['to']} 名",
                            "body": s["title"], "link": s["link"]})
            if t == "new" and c["newTop"]["on"] and x["rank"] <= c["newTop"]["top"]:
                out.append({"id": f"new|{s['key']}|{x['src']}", "title": f"🆕 {x['src']} 空降第 {x['rank']} 名",
                            "body": s["title"], "link": s["link"]})
            if t == "multi" and c["multi"]["on"] and x["n"] >= c["multi"]["n"]:
                out.append({"id": f"multi|{s['key']}", "title": f"📣 {x['n']} 家熱門榜同時上榜", "body": s["title"], "link": s["link"]})
    for t in f.get("topics", []):
        if not ok(t.get("cat")):
            continue
        cat = f"｜{t['cat']}" if t.get("cat") else ""
        if c["hotNo"]["on"] and t["unwritten"] and t["score"] >= c["hotNo"]["minScore"]:
            out.append({"id": f"hotno|{t['key']}", "title": f"⚠️ ETtoday 未寫｜熱度 {t['score']}{cat}", "body": t["title"], "link": t["link"]})
        elif c["hot"]["on"] and t["score"] >= c["hot"]["minScore"]:
            out.append({"id": f"hot|{t['key']}", "title": f"🔥 熱度 {t['score']}{cat}", "body": t["title"], "link": t["link"]})
    return out


def _in_quiet(c: dict, hour: int) -> bool:
    if not c["quietOn"]:
        return False
    a, b = c["quietFrom"], c["quietTo"]
    return (a <= hour < b) if a <= b else (hour >= a or hour < b)


def push_ntfy(facts: dict, state_file: Path, topic: str, user_cfg: dict | None = None,
              recheck=None, send=None) -> int:
    """比對規則並推到 ntfy；回傳送出則數。
    recheck(title)：「ETtoday 未寫」類推播前的最後複查，True＝其實已寫（跳過）、"later"＝查不出來下輪再說、False＝確定沒寫。
    send(payload)->bool：測試時可替換。已推過的 24 小時內不重推；安靜時段不標記，結束後會補發。"""
    c = merged_cfg(user_cfg or {})
    if not c["enabled"] or not topic:
        return 0
    tz = pytz.timezone("Asia/Taipei")
    now = datetime.now(tz)
    try:
        st = json.loads(state_file.read_text())
    except Exception:
        st = {}
    st.setdefault("seen", {})
    if st.get("day") != now.strftime("%Y-%m-%d"):
        st["day"], st["n"] = now.strftime("%Y-%m-%d"), 0
    st["seen"] = {k: v for k, v in st["seen"].items() if time.time() - v < 86400}
    first = "init" not in st      # 第一次只記錄現況，避免一次噴一堆舊消息
    st["init"] = True
    sent = 0
    for a in evaluate(facts, c):
        if a["id"] in st["seen"]:
            continue
        if first:
            st["seen"][a["id"]] = time.time()
            continue
        if _in_quiet(c, now.hour):
            continue
        if st["n"] >= c["dailyCap"]:
            break
        if a["id"].startswith("hotno|") and recheck:
            r = recheck(a["body"])
            if r == "later":      # 查不出來（AI 塞車）：這輪先不推也不標記，下輪再說
                continue
            if r:                 # ETtoday 其實已經寫了
                st["seen"][a["id"]] = time.time()
                continue
        payload = {"topic": topic, "title": a["title"], "message": a["body"], "priority": 4}
        if a["link"]:
            payload["click"] = a["link"]
        ok = send(payload) if send else _post(payload)
        if ok:
            st["seen"][a["id"]] = time.time()
            st["n"] += 1
            sent += 1
    state_file.write_text(json.dumps(st, ensure_ascii=False))
    return sent


def _post(payload: dict) -> bool:
    try:
        return requests.post(NTFY_URL, json=payload, timeout=10).status_code == 200
    except Exception:
        return False
