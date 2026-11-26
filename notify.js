// 在 components.html 的 iframe 裡執行；把面板、通知按鈕掛到父頁面（同源）。
(function () {
  const P = window.parent, D = P.document;
  const KEY_CFG = "mw-notify-cfg", KEY_SEEN = "mw-notify-seen", KEY_SENT = "mw-notify-sent";
  const CATS = ["政治", "社會", "生活", "娛樂", "財經", "國際", "體育", "科技", "寵物", "未分類"];
  const DEFAULTS = {
    cats: CATS.slice(),
    enabled: false, dailyCap: 20, quietOn: true, quietFrom: 0, quietTo: 7,
    jump:   { on: true,  minUp: 5, within: 30 },
    hotNo:  { on: true,  minScore: 5 },
    newTop: { on: false, top: 3 },
    multi:  { on: false, n: 3 },
    hot:    { on: false, minScore: 8 },
  };
  const ls = {
    get(k, d) { try { const v = P.localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch (e) { return d; } },
    set(k, v) { try { P.localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
  };
  // 匿名計數：儲存設定時回報「隨機編號、開關、作業系統」（不含個人資料）。網址留空就不送。
  const PING_URL = "https://script.google.com/macros/s/AKfycbxtaKkcTcTMC6SESoMNdC4s4jR0NtVE-8XgW-uNlSivroB6joiUoE4RUUbig13XegQm/exec";
  function ping(enabled) {
    if (!PING_URL) return;
    try {
      let id = ls.get("mw-notify-id", null);
      if (!id) { id = (P.crypto && P.crypto.randomUUID) ? P.crypto.randomUUID() : String(Date.now()) + Math.random().toString(36).slice(2); ls.set("mw-notify-id", id); }
      const pf = P.navigator.platform || "", os = /Win/i.test(pf) ? "Windows" : /Mac/i.test(pf) ? "Mac" : "其他";
      P.fetch(PING_URL, { method: "POST", mode: "no-cors", headers: { "Content-Type": "text/plain" }, body: JSON.stringify({ id, enabled: !!enabled, os }) }).catch(() => {});
    } catch (e) {}
  }
  const cfg = () => { const c = ls.get(KEY_CFG, {}); return Object.assign({}, DEFAULTS, c,
    Object.fromEntries(Object.keys(DEFAULTS).filter(k => typeof DEFAULTS[k] === "object" && !Array.isArray(DEFAULTS[k])).map(k => [k, Object.assign({}, DEFAULTS[k], c[k] || {})]))); };

  // ── 規則（純比對）──────────────────────────────────────────
  function evaluate(f, c) {
    const out = [];
    const ok = cat => (c.cats || CATS).includes(cat || "未分類");
    for (const s of f.surges || []) if (ok(s.cat)) for (const x of s.facts || []) {
      if (x.type === "jump" && c.jump.on && x.from - x.to >= c.jump.minUp && x.mins <= c.jump.within + 10)
        out.push({ id: "jump|" + s.key + "|" + x.src, title: `🚀 ${x.src} ${x.mins} 分鐘 ${x.from}→${x.to} 名`, body: s.title, link: s.link });
      if (x.type === "new" && c.newTop.on && x.rank <= c.newTop.top)
        out.push({ id: "new|" + s.key + "|" + x.src, title: `🆕 ${x.src} 空降第 ${x.rank} 名`, body: s.title, link: s.link });
      if (x.type === "multi" && c.multi.on && x.n >= c.multi.n)
        out.push({ id: "multi|" + s.key, title: `📣 ${x.n} 家熱門榜同時上榜`, body: s.title, link: s.link });
    }
    for (const t of f.topics || []) {
      if (!ok(t.cat)) continue;
      if (c.hotNo.on && t.unwritten && t.score >= c.hotNo.minScore)
        out.push({ id: "hotno|" + t.key, title: `⚠️ ETtoday 未寫｜熱度 ${t.score}${t.cat ? "｜" + t.cat : ""}`, body: t.title, link: t.link });
      else if (c.hot.on && t.score >= c.hot.minScore)
        out.push({ id: "hot|" + t.key, title: `🔥 熱度 ${t.score}${t.cat ? "｜" + t.cat : ""}`, body: t.title, link: t.link });
    }
    return out;
  }

  function quiet(c) {
    if (!c.quietOn) return false;
    const h = new Date(new Date().toLocaleString("en-US", { timeZone: "Asia/Taipei" })).getHours();
    return c.quietFrom <= c.quietTo ? (h >= c.quietFrom && h < c.quietTo) : (h >= c.quietFrom || h < c.quietTo);
  }

  function fire(a) {
    const n = new P.Notification(a.title, { body: a.body, tag: a.id, icon: "/app/static/icon-192.png" });
    n.onclick = () => { try { P.focus(); } catch (e) {} if (a.link) P.open(a.link, "_blank"); n.close(); };
  }

  function feed(f) {
    const c = cfg(), now = Date.now();
    const alerts = evaluate(f, c);
    let seen = ls.get(KEY_SEEN, null);
    const first = seen === null;           // 第一次：只記錄現況，不一次噴一堆
    seen = Object.fromEntries(Object.entries(seen || {}).filter(([, t]) => now - t < 86400000));
    let sent = ls.get(KEY_SENT, { day: "", n: 0 });
    const today = new Date().toDateString();
    if (sent.day !== today) sent = { day: today, n: 0 };
    for (const a of alerts) {
      if (seen[a.id]) continue;
      if (first || !c.enabled || P.Notification.permission !== "granted") { seen[a.id] = now; continue; }
      if (quiet(c)) continue;              // 安靜時段不標記已看過，等時段結束後仍會補發
      if (sent.n >= c.dailyCap) break;
      fire(a); seen[a.id] = now; sent.n++;
    }
    ls.set(KEY_SEEN, seen); ls.set(KEY_SENT, sent);
    P.__mwLast = { alerts: alerts.length, sent: sent.n };
    const el = D.getElementById("mwn-status"); if (el) el.textContent = status(c);
  }

  function status(c) {
    if (!("Notification" in P)) return "這個瀏覽器不支援通知";
    if (P.Notification.permission === "denied") return "瀏覽器已封鎖通知，請到網址列的鎖頭圖示開啟";
    if (!c.enabled) return "通知目前關閉";
    if (P.Notification.permission !== "granted") return "尚未授權通知";
    const s = ls.get(KEY_SENT, { n: 0 });
    return `通知已開啟，今天已發 ${s.n || 0} 則` + (quiet(c) ? "（安靜時段中）" : "");
  }

  // ── 安裝成 App 所需的 manifest ───────────────────────────────
  if (!D.querySelector('link[rel="manifest"]')) {
    const l = D.createElement("link"); l.rel = "manifest"; l.href = "/app/static/manifest.json"; D.head.appendChild(l);
  }

  // ── 面板（只建一次）──────────────────────────────────────────
  if (!P.__mwNotifyUI) {
    P.__mwNotifyUI = true;
    const st = D.createElement("style");
    st.textContent = `
      #mwn-btn{position:fixed;left:18px;bottom:18px;z-index:99999;width:46px;height:46px;border-radius:50%;border:none;
        background:#e94c15;color:#fff;font-size:20px;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.3)}
      #mwn-panel{position:fixed;left:18px;bottom:74px;z-index:99999;width:320px;max-height:78vh;overflow:auto;background:#fff;color:#222;
        border:1px solid #ddd;border-top:4px solid #e94c15;padding:14px;font:13px/1.5 "PingFang TC","Microsoft JhengHei",Arial,sans-serif;display:none;box-shadow:0 4px 16px rgba(0,0,0,.25)}
      #mwn-panel h3{margin:0 0 6px;font-size:15px} #mwn-panel label{display:flex;align-items:center;gap:6px;margin:6px 0}
      #mwn-panel input[type=number]{width:54px;padding:2px 4px} #mwn-panel .sub{color:#888;font-size:12px;margin:2px 0 8px}
      #mwn-panel hr{border:none;border-top:1px dotted #ccc;margin:10px 0} #mwn-panel button.act{background:#e94c15;color:#fff;border:none;padding:6px 12px;cursor:pointer;margin-right:6px}
      #mwn-panel button.sec{background:#eee;color:#333;border:none;padding:6px 12px;cursor:pointer}
      html.mw-dark #mwn-panel{background:#111;color:#e6e6e6;border-color:#333}`;
    D.head.appendChild(st);
    const btn = D.createElement("button"); btn.id = "mwn-btn"; btn.title = "通知設定"; btn.textContent = "🔔"; D.body.appendChild(btn);
    const pn = D.createElement("div"); pn.id = "mwn-panel"; D.body.appendChild(pn);
    btn.onclick = () => { pn.style.display = pn.style.display === "block" ? "none" : "block"; if (pn.style.display === "block") render(); };

    function num(id, v, min, max) { return `<input type="number" id="${id}" value="${v}" min="${min}" max="${max}">`; }
    function render() {
      const c = cfg();
      pn.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center"><h3>🔔 通知設定</h3><button id="mwn-x" title="關閉" aria-label="關閉通知設定" style="border:none;background:transparent;color:inherit;font-size:20px;line-height:1;cursor:pointer;padding:2px 6px">✕</button></div>
        <div class="sub" id="mwn-status">${status(c)}</div>
        <label><input type="checkbox" id="mwn-en" ${c.enabled ? "checked" : ""}> 開啟通知</label>
        <hr>
        <label><input type="checkbox" id="mwn-j" ${c.jump.on ? "checked" : ""}> 名次 ${num("mwn-jw", c.jump.within, 25, 120)} 分鐘內升 ${num("mwn-ju", c.jump.minUp, 5, 20)} 名以上</label>
        <label><input type="checkbox" id="mwn-h" ${c.hotNo.on ? "checked" : ""}> ETtoday 未寫且熱度 ≥ ${num("mwn-hs", c.hotNo.minScore, 4, 30)}</label>
        <label><input type="checkbox" id="mwn-n" ${c.newTop.on ? "checked" : ""}> 空降熱門榜前 ${num("mwn-nt", c.newTop.top, 1, 5)} 名</label>
        <label><input type="checkbox" id="mwn-m" ${c.multi.on ? "checked" : ""}> ${num("mwn-mn", c.multi.n, 3, 8)} 家以上熱門榜同時上榜</label>
        <label><input type="checkbox" id="mwn-t" ${c.hot.on ? "checked" : ""}> 熱度 ≥ ${num("mwn-ts", c.hot.minScore, 4, 30)}（不管 ETtoday 有沒有寫）</label>
        <hr>
        <div class="sub" style="margin:0 0 2px">只通知這些分類（暴衝新聞若分不出類別，歸在「未分類」）</div>
        <div id="mwn-cats" style="display:flex;flex-wrap:wrap;gap:2px 12px">${CATS.map(k => `<label style="margin:2px 0"><input type="checkbox" class="mwn-cat" value="${k}" ${(c.cats || CATS).includes(k) ? "checked" : ""}> ${k}</label>`).join("")}</div>
        <div class="sub"><a href="#" id="mwn-all">全選</a>　<a href="#" id="mwn-none">全不選</a></div>
        <hr>
        <label>每天最多 ${num("mwn-cap", c.dailyCap, 1, 200)} 則</label>
        <label><input type="checkbox" id="mwn-q" ${c.quietOn ? "checked" : ""}> 安靜時段 ${num("mwn-qf", c.quietFrom, 0, 23)} 點到 ${num("mwn-qt", c.quietTo, 0, 23)} 點不通知</label>
        <hr>
        <button class="act" id="mwn-save">儲存</button><button class="sec" id="mwn-test">測試通知</button><button class="sec" id="mwn-close" style="margin-left:6px">關閉</button>
        <div class="sub" style="margin-top:8px">規則全由程式比對，不使用 AI。設定存在這台裝置的瀏覽器。需要保持這個頁面或 App 在背景開著才收得到。</div>`;
      const g = id => D.getElementById(id), v = id => Number(g(id).value);
      const setAll = v => D.querySelectorAll(".mwn-cat").forEach(x => { x.checked = v; });
      g("mwn-all").onclick = e => { e.preventDefault(); setAll(true); };
      g("mwn-none").onclick = e => { e.preventDefault(); setAll(false); };
      const closePanel = () => { pn.style.display = "none"; };
      g("mwn-x").onclick = closePanel; g("mwn-close").onclick = closePanel;
      g("mwn-save").onclick = async () => {
        const n = { cats: [...D.querySelectorAll(".mwn-cat")].filter(x => x.checked).map(x => x.value), enabled: g("mwn-en").checked, dailyCap: v("mwn-cap"), quietOn: g("mwn-q").checked, quietFrom: v("mwn-qf"), quietTo: v("mwn-qt"),
          jump: { on: g("mwn-j").checked, within: v("mwn-jw"), minUp: v("mwn-ju") }, hotNo: { on: g("mwn-h").checked, minScore: v("mwn-hs") },
          newTop: { on: g("mwn-n").checked, top: v("mwn-nt") }, multi: { on: g("mwn-m").checked, n: v("mwn-mn") }, hot: { on: g("mwn-t").checked, minScore: v("mwn-ts") } };
        ls.set(KEY_CFG, n);
        ping(n.enabled);
        if (n.enabled && "Notification" in P && P.Notification.permission === "default") await P.Notification.requestPermission();
        g("mwn-status").textContent = status(cfg());
        btn.textContent = n.enabled ? "🔔" : "🔕";
      };
      g("mwn-test").onclick = async () => {
        if (!("Notification" in P)) return alert("這個瀏覽器不支援通知");
        if (P.Notification.permission === "default") await P.Notification.requestPermission();
        const out = g("mwn-status");
        if (P.Notification.permission !== "granted") { out.textContent = "通知被封鎖了，請到瀏覽器網址列的鎖頭圖示開啟"; return; }
        try {
          fire({ id: "test" + Date.now(), title: "🔔 測試通知", body: "收到這則代表通知功能正常", link: "" });
          out.textContent = "已送出測試通知。如果沒看到，請檢查 Mac「系統設定 → 通知 → Google Chrome」和「專注模式」";
        } catch (e) { out.textContent = "發送失敗：" + e.message; }
      };
    }
    btn.textContent = cfg().enabled ? "🔔" : "🔕";
  }

  feed(FACTS);
})();
