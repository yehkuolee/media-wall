"""通知規則：全部是程式比對，不呼叫任何 AI（不花 Claude 額度）。

流程：Python 把「目前的暴衝／熱門話題」整理成事實清單（facts），
交給瀏覽器裡的 notify.js 依使用者設定的門檻比對、發系統通知。
門檻、開關存在使用者自己的瀏覽器（localStorage），每台裝置各自設定。
"""
import json
from pathlib import Path

TOPIC_FLOOR = 4      # 熱度低於這個的話題不送進前端（UI 門檻最低 4）
TOPIC_LIMIT = 15     # 只看熱度前 15 名，避免替一堆冷門話題比對 ETtoday


def build_facts(surges: list, topics: list) -> dict:
    """surges: detect_surges() 的結果；topics: 已經標好 et 的話題清單（依熱度排）"""
    return {
        "surges": [
            {"key": f["news"].get("link") or f["news"]["title"], "title": f["news"]["title"],
             "link": f["news"].get("link", ""), "facts": f.get("facts", [])}
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
