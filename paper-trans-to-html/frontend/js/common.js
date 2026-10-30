/* 公共工具 */
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
async function api(path, opts = {}) {
  const opt = { method: opts.method || "GET", headers: opts.headers || {} };
  // GET 一律绕过浏览器 HTTP 缓存：论文版式（重解析）、译文/笔记/状态等都可能
  // 被其它窗口或服务端工具更新 —— 缓存里藏着旧副本会让刷新看到旧数据。
  if (opt.method === "GET") opt.cache = "no-store";
  if (opts.json != null) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(opts.json);
  } else if (opts.body != null) {
    opt.body = opts.body;
  }
  // 页面正在卸载（关标签页 / 切走）时补发的请求要 keepalive，否则随页面一起被掐掉
  if (opts.keepalive) opt.keepalive = true;
  const r = await fetch(path, opt);
  if (!r.ok) {
    let msg = "请求失败(" + r.status + ")";
    try { const j = await r.json(); if (j && j.detail) msg = j.detail; } catch (e) { /* ignore */ }
    if (r.status === 401 && !opts.noRedirect) gotoLogin();
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}
let toastTimer = null;
function toast(msg, ms = 2600) {
  let t = document.getElementById("toast");
  if (!t) { t = el("div", ""); t.id = "toast"; document.body.appendChild(t); }
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}
function showNotice(msg, isError) {
  const n = document.getElementById("notice");
  if (!n) return;
  n.classList.remove("error");
  if (isError) n.classList.add("error");
  n.textContent = msg;
  n.hidden = !msg;
}
function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
function queryId(id) { return new URLSearchParams(location.search).get(id); }

/* ============================================================
   登录态 / 顶栏用户区 / LLM 设置弹窗
   —— 服务端有 AuthGate 中间件兜底（未登录的页面直接 302 到登录页），
      这里主要处理“用着用着 Cookie 过期”的场景，以及提供配置入口。
   ============================================================ */
const LLM_PRESET = {
  deepseek: ["https://api.deepseek.com", "deepseek-chat"],
  openai: ["https://api.openai.com/v1", "gpt-4o-mini"],
  anthropic: ["https://api.anthropic.com/v1", "claude-sonnet-4-5"],
  custom: ["", ""],
  google: ["", ""],
};

function gotoLogin() {
  if (/\/login\.html$/.test(location.pathname)) return;
  const next = location.pathname + location.search;
  location.replace("login.html?next=" + encodeURIComponent(next));
}

let _authStatusP = null;
function authStatus(force) {
  if (!_authStatusP || force) {
    _authStatusP = api("/api/auth/status", { noRedirect: true }).catch(() => null);
  }
  return _authStatusP;
}

let _cfgP = null;
function appConfig(force) {
  if (!_cfgP || force) _cfgP = api("/api/config", { noRedirect: true }).catch(() => null);
  return _cfgP;
}

async function logout(toUser) {
  try { await api("/api/auth/logout", { method: "POST", noRedirect: true }); } catch (e) { /* ignore */ }
  // switch=1：让登录页知道这是“刚退出”；带 u=账号 时预填该账号名（点列表里某个账号切过去用）
  const q = toUser ? ("?switch=1&u=" + encodeURIComponent(toUser)) : "?switch=1";
  location.replace("login.html" + q);
}

/* ---------- 主题（暗夜模式） ----------
   两级存储：
     1) localStorage —— 首屏由各页面 <head> 里的一行小脚本先套上 class，避免闪一下白；
     2) 账号偏好（/api/prefs 的 theme）—— 登录后为准，换设备也跟着走。
   切主题时在 `window` 上派发 `themechange`，正文/高亮那类需要重算的模块监听它。 */
const THEME_KEY = "paperReader.theme";
function isDark() { return document.documentElement.classList.contains("dark"); }
function currentTheme() { return isDark() ? "dark" : "light"; }

function applyTheme(theme, opts) {
  const o = opts || {};
  const dark = theme === "dark";
  document.documentElement.classList.toggle("dark", dark);
  try { localStorage.setItem(THEME_KEY, dark ? "dark" : "light"); } catch (e) { /* ignore */ }
  document.querySelectorAll("[data-theme-btn]").forEach(b => {
    b.textContent = dark ? "☀️" : "🌙";
    b.title = dark ? "切换到日间模式" : "切换到暗夜模式";
    b.setAttribute("aria-label", b.title);
  });
  if (o.notify) window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: dark ? "dark" : "light" } }));
  if (o.save) {
    // 未登录时（登录页）这个请求会 401：别让它把页面重定向走，静默失败即可
    api("/api/prefs", { method: "POST", json: { theme: dark ? "dark" : "light" }, noRedirect: true })
      .catch(err => showNotice("主题偏好没能保存到服务端：" + err.message, true));
  }
  return dark;
}

function toggleTheme() {
  applyTheme(isDark() ? "light" : "dark", { save: true, notify: true });
}

/* 任何带 data-theme-btn 的元素都是开关（顶栏用户区、登录页角落…），
   统一用事件委派，新加按钮不用再接线。 */
document.addEventListener("click", e => {
  const b = e.target.closest && e.target.closest("[data-theme-btn]");
  if (b) toggleTheme();
});
applyTheme(currentTheme());     // 页面一进来就把开关的图标/提示同步好（登录页没有用户区）

/* 登录后拉一次账号偏好里的主题（拿不到就用本地的） */
async function syncTheme() {
  try {
    const p = await api("/api/prefs");
    if (p && (p.theme === "dark" || p.theme === "light")) {
      applyTheme(p.theme, { notify: true });
    }
  } catch (e) { /* 未登录 / 接口不可用：保持本地那份 */ }
}

/* 顶栏右侧：LLM 设置 + 当前用户 + 退出 */
async function mountUserBar() {
  const bar = document.querySelector(".topbar");
  if (!bar || bar.querySelector(".user-wrap")) return;
  const st = await authStatus();
  if (!st) return;
  if (!st.authenticated) { gotoLogin(); return; }
  const name = st.username || "user";
  const wrap = el("div", "user-wrap");
  wrap.innerHTML =
    `<button class="btn ghost" id="btnTheme" data-theme-btn type="button"></button>` +
    `<button class="btn ghost" id="btnLlmSet" title="设置：LLM 服务 / 存储 与 OCR / 账号">⚙ <span class="hide-sm">设置</span></button>` +
    `<span class="user-chip" title="当前账号：${esc(name)}（点此管理账号/切换账号）">` +
      `<span class="av">${esc(name.slice(0, 1).toUpperCase())}</span>` +
      `<span class="uname">${esc(name)}</span></span>` +
    `<button class="btn ghost" id="btnLogout" title="退出登录">退出</button>`;
  bar.appendChild(wrap);
  wrap.querySelector("#btnLlmSet").onclick = () => openLlmModal();
  wrap.querySelector(".user-chip").onclick = () => openLlmModal("acc");
  wrap.querySelector("#btnLogout").onclick = logout;
  applyTheme(currentTheme());     // 先把按钮上的图标同步好
  syncTheme();                    // 再按账号偏好覆盖
}

/* 共享弹窗：把这篇论文（含笔记/高亮/译文/AI 整理）共享给别的账号，并决定读写权限。
   首页与阅读器共用；`onChange` 用于让调用方刷新列表/界面。

   账号选择用的是和「LLM 配置」同一套的自定义下拉（`.drop*`）——原生 `<select>` 又窄又难看；
   权限用分段按钮（只读 / 可写），一眼能看出当前选的是哪个。 */
const SHARE_PERM_TIP = {
  read: "只读：能看笔记 / 高亮 / 译文与 AI 整理，能自己翻译、记自己的阅读进度；不能改任何内容。",
  write: "可写：笔记、高亮、AI 整理与对话都能一起改（相当于共同阅读同一份笔记）。",
};

function openShareModal(docId, title, onChange) {
  if (document.getElementById("shareModal")) return;
  const m = el("div", "modal");
  m.id = "shareModal";
  m.innerHTML = `
    <div class="modal-mask" data-close></div>
    <div class="modal-card sh-card">
      <div class="modal-head"><b>🔗 共享这篇论文</b>
        <button class="modal-x" data-close title="关闭">×</button></div>
      <div class="modal-body">
        <p class="hint sh-note">
          把《<b id="shTitle"></b>》<b>连同笔记、高亮、译文、AI 整理与对话</b>一起共享给对方。
        </p>
        <div class="field">
          <label>共享给</label>
          <div class="drop sh-drop" id="shDrop">
            <button class="drop-btn" id="shUserBtn" type="button"
                    aria-haspopup="listbox" aria-expanded="false"></button>
            <div class="drop-menu" id="shUserMenu" role="listbox" hidden></div>
            <select id="shUser" hidden></select>
          </div>
        </div>
        <div class="field">
          <label>对方权限</label>
          <div class="seg sh-permseg" id="shPermSeg">
            <button type="button" data-perm="read" class="on">只读</button>
            <button type="button" data-perm="write">可写</button>
          </div>
          <div class="hint" id="shPermHint"></div>
        </div>
        <div class="form-err" id="shErr" hidden></div>
        <div class="row-actions"><button class="btn primary" id="shAdd" type="button">共享给对方</button></div>
        <div class="field" style="margin-top:16px">
          <label>已共享给</label>
          <div class="acclist" id="shList">读取中…</div>
        </div>
      </div>
      <div class="modal-foot"><button class="btn" id="shClose" type="button">关闭</button></div>
    </div>`;
  document.body.appendChild(m);

  const $ = id => m.querySelector("#" + id);
  const err = msg => { const n = $("shErr"); n.textContent = msg || ""; n.hidden = !msg; };
  $("shTitle").textContent = title || "";

  let accounts = [];          // 别的账号（带 papers 数）
  let items = [];             // 已共享名单
  let perm = "read";          // 新共享用的权限

  /* 头像：按用户名挑一个固定的渐变底色，比一行灰字好认 */
  function avCls(name) {
    let h = 7;
    for (const ch of String(name || "")) h = (h * 31 + ch.charCodeAt(0)) % 9973;
    return "u" + (h % 4 + 1);
  }
  const initial = n => esc(String(n || "?").slice(0, 1).toUpperCase());
  const avHtml = n => `<span class="drop-av ${avCls(n)}">${initial(n)}</span>`;
  const subOf = n => {
    const u = accounts.find(x => x.username === n);
    return u && u.papers != null ? u.papers + " 篇论文" : "可共享";
  };

  function setPerm(p) {
    perm = p;
    m.querySelectorAll("#shPermSeg button").forEach(b =>
      b.classList.toggle("on", b.dataset.perm === p));
    $("shPermHint").textContent = SHARE_PERM_TIP[p] || "";
  }
  m.querySelectorAll("#shPermSeg button").forEach(b =>
    b.addEventListener("click", () => setPerm(b.dataset.perm)));

  function toggleUserMenu(open) {
    const btn = $("shUserBtn"), menu = $("shUserMenu");
    if (open == null) open = menu.hidden;
    menu.hidden = !open;
    btn.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", String(open));
  }
  function onDocClick(e) { if (!$("shDrop").contains(e.target)) toggleUserMenu(false); }
  $("shUserBtn").onclick = () => toggleUserMenu();

  function renderUserBtn(left) {
    const btn = $("shUserBtn");
    const cur = $("shUser").value;
    $("shAdd").disabled = !cur;          // 没有人可选时，别让“共享给对方”看着能点
    if (!cur) {
      btn.disabled = true;
      btn.innerHTML = `<span class="drop-av k-new">＋</span>
        <span class="drop-txt"><span class="drop-name">没有可共享的账号</span>
        <span class="drop-sub">${accounts.length
          ? "所有账号都已经共享过了"
          : "让对方先注册，或在「⚙ 设置 → 账号」里新建一个"}</span></span>
        <span class="drop-chev">▾</span>`;
      return;
    }
    btn.disabled = false;
    btn.innerHTML = avHtml(cur) +
      `<span class="drop-txt"><span class="drop-name">${esc(cur)}</span>` +
      `<span class="drop-sub">${esc(subOf(cur))}</span></span>` +
      `<span class="drop-chev">▾</span>`;
  }

  function renderUserMenu(left) {
    const menu = $("shUserMenu");
    if (!left.length) {
      menu.innerHTML = `<div class="drop-empty">${accounts.length
        ? "所有账号都已经共享过了" : "还没有别的账号"}</div>`;
      return;
    }
    const cur = $("shUser").value;
    menu.innerHTML = left.map(u =>
      `<button type="button" class="drop-item${u.username === cur ? " on" : ""}"` +
      ` data-pick="${esc(u.username)}" role="option" aria-selected="${u.username === cur}">` +
        `<span class="drop-check">${u.username === cur ? "✓" : ""}</span>` +
        avHtml(u.username) +
        `<span class="drop-txt"><span class="drop-name">${esc(u.username)}</span>` +
        `<span class="drop-sub">${u.papers != null ? u.papers + " 篇论文" : ""}</span></span>` +
      `</button>`).join("");
    menu.querySelectorAll("[data-pick]").forEach(b => {
      b.onclick = () => {
        $("shUser").value = b.dataset.pick;
        toggleUserMenu(false);
        err("");
        renderUserBtn(left);
        renderUserMenu(left);
      };
    });
  }

  /* 小号分段按钮：就地改某个人的权限（不用再点开什么） */
  function permSeg(user, cur) {
    const seg = el("div", "sh-mini");
    for (const [p, label] of [["read", "只读"], ["write", "可写"]]) {
      const b = el("button", p === cur ? "on" : "", label);
      b.type = "button";
      b.title = "改为 " + label;
      b.addEventListener("click", async () => {
        if (p === cur) return;
        try {
          await api("/api/doc/" + docId + "/share",
                    { method: "POST", json: { user: user, perm: p } });
          toast("已把 " + user + " 改为" + label + " ✓");
          if (onChange) onChange();
          reload();
        } catch (e) { toast(e.message, 3200); }
      });
      seg.appendChild(b);
    }
    return seg;
  }

  function renderList() {
    const list = $("shList");
    list.textContent = "";
    if (!items.length) {
      list.appendChild(el("div", "sh-empty", "还没有共享给任何人。"));
      return;
    }
    for (const it of items) {
      const row = el("div", "accrow");
      const av = el("span", "drop-av " + avCls(it.user));
      av.textContent = String(it.user).slice(0, 1).toUpperCase();
      const txt = el("div", "drop-txt");
      txt.appendChild(el("span", "drop-name", it.user));
      txt.appendChild(el("span", "drop-sub",
        it.perm === "write" ? "可写 · 能一起改笔记与 AI 整理" : "只读 · 只能看，改不了内容"));
      row.append(av, txt, permSeg(it.user, it.perm));
      const del = el("button", "btn danger", "取消共享");
      del.type = "button";
      del.addEventListener("click", async () => {
        if (!confirm("取消对「" + it.user + "」的共享？（他的阅读进度也会清掉，论文本身不动）")) return;
        try {
          await api("/api/doc/" + docId + "/share/" + encodeURIComponent(it.user),
                    { method: "DELETE" });
          toast("已取消共享");
          if (onChange) onChange();
          reload();
        } catch (e) { toast(e.message, 3200); }
      });
      row.appendChild(del);
      list.appendChild(row);
    }
  }

  async function reload() {
    let st = null;
    try {
      st = await api("/api/doc/" + docId + "/share");
    } catch (e) {
      $("shList").textContent = "读取失败：" + e.message;
      return;
    }
    items = st.items || [];
    try {
      const u = await api("/api/auth/users");
      accounts = (u.users || []).filter(x => x.username !== u.current);
    } catch (e) { accounts = []; }
    const taken = new Set(items.map(s => s.user));
    const left = accounts.filter(u => !taken.has(u.username));   // 已经共享过的就不再列出来
    const sel = $("shUser");
    const keep = sel.value;
    sel.innerHTML = left.map(u =>
      `<option value="${esc(u.username)}">${esc(u.username)}</option>`).join("");
    if (!left.some(u => u.username === keep)) sel.value = left.length ? left[0].username : "";
    toggleUserMenu(false);
    renderUserBtn(left);
    renderUserMenu(left);
    renderList();
  }

  $("shAdd").onclick = async () => {
    const who = $("shUser").value;
    if (!who) { err("没有可共享的账号"); return; }
    err("");
    if ($("shAdd").disabled) { err("没有可共享的账号"); return; }
    $("shAdd").disabled = true;
    try {
      await api("/api/doc/" + docId + "/share", { method: "POST", json: { user: who, perm: perm } });
      toast("已按「" + (perm === "write" ? "可写" : "只读") + "」共享给 " + who + " ✓");
      if (onChange) onChange();
      reload();
    } catch (e) {
      err(e.message);
    } finally {
      if ($("shUser").value) $("shAdd").disabled = false;
    }
  };

  function close() {
    document.removeEventListener("click", onDocClick);
    m.remove();
  }
  m.querySelectorAll("[data-close]").forEach(b => b.onclick = close);
  $("shClose").onclick = close;
  document.addEventListener("click", onDocClick);

  setPerm("read");
  reload();
}

/* ---------- 服务类型下拉：自绘浮层（原生 select 只当状态载体） ----------
   设置弹窗与登录页「首次引导」共用同一套渲染，保证两处风格与交互一致。 */
const LLM_PROVIDERS = [
  { provider: "deepseek",  name: "DeepSeek",           sub: "api.deepseek.com · deepseek-chat" },
  { provider: "openai",    name: "OpenAI",             sub: "api.openai.com · gpt-4o-mini" },
  { provider: "anthropic", name: "Anthropic (Claude)", sub: "api.anthropic.com · claude-sonnet-4-5" },
  { provider: "custom",    name: "OpenAI 兼容服务",     sub: "自建 / 中转 / 本地 Ollama", badge: "通用" },
  { provider: "google",    name: "Google 免费兜底",     sub: "无需 API Key，配置最省事",  badge: "免费" },
];
function llmProviderKey(v) {
  return LLM_PROVIDERS.some(p => p.provider === v) ? v : "custom";
}
function llmProviderMeta(v) {
  return LLM_PROVIDERS.find(p => p.provider === llmProviderKey(v)) || LLM_PROVIDERS[0];
}
function llmProviderInitial(v) {
  return { deepseek: "D", openai: "O", anthropic: "A", custom: "⚙", google: "G" }
    [llmProviderKey(v)] || "L";
}
function llmProviderAvatar(v) {
  return `<span class="drop-av k-${llmProviderKey(v)}">${esc(llmProviderInitial(v))}</span>`;
}

/* 把「hidden select + 按钮 + 浮层菜单」组装成一个服务类型下拉。
   返回 { render, set, toggle, isOpen }；onChange(value) 在选中某项后回调，
   onOpen() 在展开前回调（可用于收起别的下拉）。 */
function createProviderDropdown(o) {
  const select = o.select, btn = o.btn, menu = o.menu;
  function render() {
    const cur = select.value;
    const meta = llmProviderMeta(cur);
    btn.innerHTML =
      llmProviderAvatar(cur) +
      `<span class="drop-txt"><span class="drop-name">${esc(meta.name)}</span>` +
      `<span class="drop-sub">${esc(meta.sub)}</span></span>` +
      `<span class="drop-chev">▾</span>`;
    menu.innerHTML = LLM_PROVIDERS.map((p) =>
      `<button type="button" class="drop-item${p.provider === cur ? " on" : ""}"` +
      ` data-prov="${esc(p.provider)}" role="option" aria-selected="${p.provider === cur}">` +
        `<span class="drop-check">${p.provider === cur ? "✓" : ""}</span>` +
        llmProviderAvatar(p.provider) +
        `<span class="drop-txt"><span class="drop-name">${esc(p.name)}</span>` +
        `<span class="drop-sub">${esc(p.sub)}</span></span>` +
        (p.badge ? `<span class="drop-badge">${esc(p.badge)}</span>` : "") +
      `</button>`).join("");
    menu.querySelectorAll("[data-prov]").forEach(b => {
      b.onclick = () => set(b.dataset.prov);
    });
  }
  function set(v) {
    select.value = v;
    toggle(false);
    render();
    if (o.onChange) o.onChange(v);
  }
  function isOpen() { return !menu.hidden; }
  function toggle(open) {
    if (open == null) open = !isOpen();
    if (open && o.onOpen) o.onOpen();
    menu.hidden = !open;
    btn.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", String(open));
  }
  btn.addEventListener("click", () => toggle());
  render();
  return { render, set, toggle, isOpen };
}

/* ---------- 翻译目标语言下拉：与「服务类型」同一套自绘浮层 ----------
   语言清单由服务端下发（`target_langs`：键 -> 名字），所以后端加语言不用改前端；
   收起时是一张卡片（语言短码 + 名称 + 语言代码），展开是浮层列表，样式与上面一致。 */
const LANG_SHORT = {
  "zh-CN": "简", "zh-TW": "繁", en: "EN", ja: "日", ko: "한",
  fr: "FR", de: "DE", es: "ES", pt: "PT", ru: "РУ", ar: "ع", hi: "हि",
};
function langAvatar(key) {
  const short = LANG_SHORT[key] || String(key || "").slice(0, 2).toUpperCase();
  return `<span class="drop-av k-lang">${esc(short)}</span>`;
}
function createLangDropdown(o) {
  const select = o.select, btn = o.btn, menu = o.menu;
  let names = {};                      // 语言键 -> 显示名（服务端下发）
  function render() {
    const cur = select.value;
    const items = Object.keys(names);
    const curName = names[cur] || cur || "选择语言";
    btn.innerHTML =
      langAvatar(cur) +
      `<span class="drop-txt"><span class="drop-name">${esc(curName)}</span>` +
      `<span class="drop-sub">${esc(cur || "未选择")}</span></span>` +
      `<span class="drop-chev">▾</span>`;
    menu.innerHTML = items.map(k =>
      `<button type="button" class="drop-item${k === cur ? " on" : ""}"` +
      ` data-lang="${esc(k)}" role="option" aria-selected="${k === cur}">` +
        `<span class="drop-check">${k === cur ? "✓" : ""}</span>` +
        langAvatar(k) +
        `<span class="drop-txt"><span class="drop-name">${esc(names[k])}</span>` +
        `<span class="drop-sub">${esc(k)}</span></span>` +
      `</button>`).join("") || `<div class="drop-empty">没有可选语言</div>`;
    menu.querySelectorAll("[data-lang]").forEach(b => {
      b.onclick = () => set(b.dataset.lang);
    });
  }
  function set(v) {
    select.value = v;
    toggle(false);
    render();
    if (o.onChange) o.onChange(v);
  }
  function isOpen() { return !menu.hidden; }
  function toggle(open) {
    if (open == null) open = !isOpen();
    if (open && o.onOpen) o.onOpen();
    menu.hidden = !open;
    btn.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", String(open));
  }
  btn.addEventListener("click", () => toggle());
  render();
  return { render, set, toggle, isOpen,
           setNames(v) { names = v || {}; render(); } };
}

/* ---------- OCR 解析后端下拉：与上面同一套自绘浮层，但不带头像 ----------
   收起时是一张文字卡片（选项名 + 说明），展开是浮层列表；边框、圆角、悬浮 / 聚焦
   高亮与「服务类型」「目标语言」完全一致，只是没有左侧的图案。 */
const OCR_BACKENDS = [
  { value: "auto", name: "自动（本地解析方案）", sub: "默认：PyMuPDF 版式 + 本地公式/表格模型", badge: "默认" },
  { value: "pymupdf", name: "本地解析方案", sub: "PyMuPDF + texify 公式 + OXR 版面/SLANet 表格" },
  { value: "detection_service_group", name: "本地解析方案（不调用服务组）", sub: "历史取值，内部等价于本地解析方案" },
  { value: "surya", name: "Surya 2 推理服务", sub: "整页 OCR：文字 / 公式 / 插图" },
  { value: "unlimited_ocr", name: "Unlimited-OCR 推理服务", sub: "整页 OCR：文字 / 公式 / 表格 / 插图" },
];
function ocrBackendMeta(v) {
  return OCR_BACKENDS.find(b => b.value === v) || OCR_BACKENDS[0];
}
function createOcrBackendDropdown(o) {
  const select = o.select, btn = o.btn, menu = o.menu;
  function render() {
    const cur = select.value;
    const meta = ocrBackendMeta(cur);
    btn.innerHTML =
      `<span class="drop-txt"><span class="drop-name">${esc(meta.name)}</span>` +
      `<span class="drop-sub">${esc(meta.sub)}</span></span>` +
      `<span class="drop-chev">▾</span>`;
    menu.innerHTML = OCR_BACKENDS.map(b =>
      `<button type="button" class="drop-item${b.value === cur ? " on" : ""}"` +
      ` data-backend="${esc(b.value)}" role="option" aria-selected="${b.value === cur}">` +
        `<span class="drop-check">${b.value === cur ? "✓" : ""}</span>` +
        `<span class="drop-txt"><span class="drop-name">${esc(b.name)}</span>` +
        `<span class="drop-sub">${esc(b.sub)}</span></span>` +
        (b.badge ? `<span class="drop-badge">${esc(b.badge)}</span>` : "") +
      `</button>`).join("");
    menu.querySelectorAll("[data-backend]").forEach(x => {
      x.onclick = () => set(x.dataset.backend);
    });
  }
  function set(v) {
    select.value = v;
    toggle(false);
    render();
    if (o.onChange) o.onChange(v);
  }
  function isOpen() { return !menu.hidden; }
  function toggle(open) {
    if (open == null) open = !isOpen();
    if (open && o.onOpen) o.onOpen();
    menu.hidden = !open;
    btn.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", String(open));
  }
  btn.addEventListener("click", () => toggle());
  render();
  return { render, set, toggle, isOpen };
}

/* 设置弹窗：LLM 服务 / 存储 与 OCR / 账号 三个页签（首页 / 阅读页共用） */
function openLlmModal(tab) {
  if (document.getElementById("llmModal")) return;
  const m = el("div", "modal");
  m.id = "llmModal";
  m.innerHTML = `
    <div class="modal-mask" data-close></div>
    <div class="modal-card">
      <div class="modal-head"><b>⚙ 设置</b>
        <button class="modal-x" data-close title="关闭">×</button></div>
      <div class="tabs">
        <button class="tab on" id="tabLlm" type="button">LLM 服务</button>
        <button class="tab" id="tabSys" type="button">存储 / OCR</button>
        <button class="tab" id="tabAcc" type="button">账号</button>
      </div>
      <div class="modal-body" id="paneLlm">
        <p class="hint" style="margin:0 0 12px">
          <b>LLM 配置跟账号走</b>：这里填的地址 / API Key / 模型只属于当前登录账号，
          别的账号看不到也用不到（与论文数据一样隔离）。
        </p>
        <div class="field">
          <label for="lmProfileBtn">已保存的配置（最多 <span id="lmMax">3</span> 套，可随时切换）</label>
          <div class="drop" id="lmProfileDrop">
            <button class="drop-btn" id="lmProfileBtn" type="button"
                    aria-haspopup="listbox" aria-expanded="false"></button>
            <div class="drop-menu" id="lmProfileMenu" role="listbox" hidden></div>
            <select id="lmProfile" hidden></select>
          </div>
          <div class="row-actions" style="margin-top:8px">
            <button class="btn" id="lmActivate" type="button" title="把选中的配置设为当前使用">切换使用</button>
            <button class="btn" id="lmNew" type="button" title="另存为一套新配置">＋ 新建</button>
            <button class="btn" id="lmDelete" type="button" title="删除选中的配置">删除</button>
          </div>
          <div class="hint" id="lmProfileHint"></div>
        </div>
        <div class="field">
          <label for="lmProfileName">配置名称（可选，便于区分）</label>
          <input id="lmProfileName" placeholder="如：官方 / 中转 / 本地 Ollama">
        </div>
        <div class="sep-line"></div>
        <div class="field">
          <label for="lmProviderBtn">服务类型</label>
          <div class="drop" id="lmProviderDrop">
            <button class="drop-btn" id="lmProviderBtn" type="button"
                    aria-haspopup="listbox" aria-expanded="false"></button>
            <div class="drop-menu" id="lmProviderMenu" role="listbox" hidden></div>
            <select id="lmProvider" hidden>
              <option value="deepseek">DeepSeek</option>
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic (Claude)</option>
              <option value="custom">OpenAI 兼容服务(自建/中转)</option>
              <option value="google">Google 免费兜底(无需 Key)</option>
            </select>
          </div>
        </div>
        <div class="field" data-llm="need">
          <label for="lmBase">服务地址 (Base URL)</label>
          <input id="lmBase" placeholder="https://api.deepseek.com">
        </div>
        <div class="field" data-llm="need">
          <label for="lmKey">API Key</label>
          <input id="lmKey" type="password" autocomplete="new-password" spellcheck="false"
                 placeholder="留空表示不修改">
          <label class="chk" style="margin-top:6px"><input type="checkbox" id="lmClearKey"> 清除已保存的 Key</label>
        </div>
        <div class="field" data-llm="need">
          <label for="lmModel">模型</label>
          <input id="lmModel" placeholder="deepseek-chat">
          <div class="hint">如 deepseek-chat / gpt-4o-mini / claude-sonnet-4-5 / qwen2.5:7b</div>
        </div>
        <div class="field">
          <label for="lmLangBtn">翻译目标语言</label>
          <div class="drop" id="lmLangDrop">
            <button class="drop-btn" id="lmLangBtn" type="button"
                    aria-haspopup="listbox" aria-expanded="false"></button>
            <div class="drop-menu" id="lmLangMenu" role="listbox" hidden></div>
            <select id="lmLang" hidden></select>
          </div>
          <div class="hint">
            原文语言不用选：交给大模型自动识别（英文/日文/德文…都行），这里只决定<b>译文</b>用哪种语言。
            译文缓存按语言分开存，换语言后不会把上一个语言的译文当成本次结果（切回去仍然在）。
          </div>
        </div>
        <div class="form-err" id="lmErr" hidden></div>
        <div class="form-ok" id="lmOk" hidden></div>
        <div class="hint" id="lmState"></div>
      </div>
      <div class="modal-body" id="paneSys" hidden>
        <p class="hint" style="margin:0 0 12px">
          数据存储与 <b>PDF 解析 / 图片与公式检测</b>是安装级配置，本机所有账号共用，
          与账号无关。
        </p>
        <div class="field">
          <label>数据存储</label>
          <div class="hint" id="lmStorage">读取中…</div>
          <div class="row-actions" style="margin-top:8px">
            <button class="btn" id="lmReconnect" type="button">重试连接 MongoDB</button>
          </div>
        </div>
        <div class="field">
          <label for="ocrBackendBtn">PDF 解析后端 / 图片与公式检测</label>
          <div class="drop" id="ocrBackendDrop">
            <button class="drop-btn" id="ocrBackendBtn" type="button"
                    aria-haspopup="listbox" aria-expanded="false"></button>
            <div class="drop-menu" id="ocrBackendMenu" role="listbox" hidden></div>
            <select id="ocrBackend" hidden>
              <option value="auto">自动（本地解析方案：PyMuPDF + 本地模型）</option>
              <option value="pymupdf">本地解析方案</option>
              <option value="detection_service_group">本地解析方案（不调用服务组）</option>
              <option value="surya">Surya 2 推理服务（整页 OCR：文字 / 公式 / 插图）</option>
              <option value="unlimited_ocr">Unlimited-OCR 推理服务（整页 OCR：文字 / 公式 / 表格 / 插图）</option>
            </select>
          </div>
          <div class="row-actions" style="margin-top:8px">
            <button class="btn primary" id="ocrSave" type="button">保存解析后端</button>
          </div>
          <div class="hint" id="lmParser">读取中…</div>
        </div>
        <div class="form-err" id="sysErr" hidden></div>
      </div>
      <div class="modal-body" id="paneAcc" hidden>
        <div class="field">
          <label>当前账号</label>
          <div class="hint" id="accCur">读取中…</div>
        </div>
        <div class="field">
          <label>全部账号</label>
          <div class="acclist" id="accList">读取中…</div>
        </div>
        <div class="field">
          <label>新增账号</label>
          <input id="accName" placeholder="3-32 位字母/数字/_.@-" autocomplete="off">
          <input id="accPwd" type="password" autocomplete="new-password" spellcheck="false"
                 placeholder="密码（至少 8 位）">
          <div class="row-actions" style="margin-top:8px">
            <button class="btn" id="accAdd" type="button">添加账号</button>
          </div>
        </div>
        <div class="form-err" id="accErr" hidden></div>
        <div class="form-ok" id="accOk" hidden></div>
        <div class="row-actions">
          <button class="btn primary" id="accSwitch" type="button">切换账号（退出登录）</button>
        </div>
        <div class="hint" style="margin-top:10px">
          <b>论文数据按账号隔离</b>：每个账号只看到、只能操作自己上传的论文（含标注、笔记、译文）。
          <b>点列表里的账号可直接切过去</b>（登录页会填好账号名，只需输密码）。
        </div>

        <div class="danger-zone">
          <div class="dz-title">⚠️ 注销当前账号</div>
          <div class="hint">
            注销会删除当前账号，并把它名下的<b>全部论文数据</b>（版式数据、高亮、笔记、译文、图片）
            一并清除，<b class="red">删除后无法恢复</b>。
          </div>
          <input id="closePwd" type="password" autocomplete="current-password" spellcheck="false"
                 placeholder="当前密码">
          <input id="closeName" autocomplete="off" placeholder="输入用户名以确认">
          <button class="btn danger block" id="accClose" type="button">我已了解，确认注销</button>
        </div>
      </div>
      <div class="modal-foot" id="lmFoot">
        <button class="btn" id="lmTest">测试连接</button>
        <button class="btn primary" id="lmSave">保存</button>
      </div>
    </div>`;
  document.body.appendChild(m);

  const $ = id => m.querySelector("#" + id);
  const err = msg => { const n = $("lmErr"); n.textContent = msg || ""; n.hidden = !msg; };
  const ok = msg => { const n = $("lmOk"); n.textContent = msg || ""; n.hidden = !msg; };
  let savedKeyMasked = "";
  let keyTouched = false;   // 只有用户真输过才提交，防浏览器自动填充误提交
  let currentUser = "";     // 当前登录账号（注销时提示/校验用）
  let myPapers = 0;         // 当前账号论文数
  let profiles = [];        // 已保存的配置（服务端返回，Key 已打码）
  let activeId = "";        // 当前生效的那套
  let editingId = "";       // 表单现在对应哪套（"" = 新建）
  let maxProfiles = 3;
  let currentLang = "";     // 服务端当前的翻译目标语言（保存后据此判断“语言是否换了”）

  function setKeyHint(masked) {
    savedKeyMasked = masked || "";
    $("lmKey").value = "";
    keyTouched = false;
    $("lmClearKey").checked = false;
    $("lmKey").placeholder = savedKeyMasked
      ? ("已保存：" + savedKeyMasked + "（留空不改）") : "sk-...";
  }

  function fillForm(p, masked, id) {
    editingId = id || "";
    $("lmProfileName").value = p.name || "";
    $("lmProvider").value = llmProviderKey(p.provider || "deepseek");
    $("lmBase").value = p.base_url || "";
    $("lmModel").value = p.model || "";
    setKeyHint(masked);
    syncProvider();
  }

  function profileLabel(p) { return p.label || p.name || p.base_url || p.provider || "配置"; }

  /* ---------- 配置下拉（自绘：原生 select 只当状态载体） ---------- */
  /* 服务类型下拉：复用全局 createProviderDropdown，与登录页引导同一套渲染 */
  const providerDrop = createProviderDropdown({
    select: $("lmProvider"), btn: $("lmProviderBtn"), menu: $("lmProviderMenu"),
    onOpen: () => { toggleProfileMenu(false); langDrop.toggle(false); ocrDrop.toggle(false); },
    onChange: () => syncProvider(),            // 重绘按钮，并按需隐藏 / 预填地址与模型
  });
  /* 翻译目标语言下拉：与上面同款自绘浮层（语言清单来自服务端） */
  const langDrop = createLangDropdown({
    select: $("lmLang"), btn: $("lmLangBtn"), menu: $("lmLangMenu"),
    onOpen: () => { toggleProfileMenu(false); providerDrop.toggle(false); ocrDrop.toggle(false); },
  });
  /* OCR 解析后端下拉：与上面同款自绘浮层（无头像，收起时是纯文字卡片） */
  const ocrDrop = createOcrBackendDropdown({
    select: $("ocrBackend"), btn: $("ocrBackendBtn"), menu: $("ocrBackendMenu"),
    onOpen: () => {
      toggleProfileMenu(false); providerDrop.toggle(false); langDrop.toggle(false);
    },
  });

  function profileSub(p) {
    return [p.provider_text || "", p.model || p.base_url || ""].filter(Boolean).join(" · ")
      || "未填写服务地址";
  }

  function profileAvatar(p) {
    return llmProviderAvatar(p.provider);
  }
  function profileText(p) {
    return `<span class="drop-txt">` +
      `<span class="drop-name">${esc(profileLabel(p))}</span>` +
      `<span class="drop-sub">${esc(profileSub(p))}</span></span>`;
  }
  function newProfileSub(full) {
    return full ? `已达上限（${maxProfiles} 套），先删一套再新建`
                : `最多保留 ${maxProfiles} 套，还可新建 ${maxProfiles - profiles.length} 套`;
  }

  /* 按当前选中值刷新“按钮上的那张卡片”和浮层里的列表 */
  function renderProfileDropdown(full) {
    full = full == null ? profiles.length >= maxProfiles : full;
    const cur = $("lmProfile").value;
    const curProf = profiles.find(x => x.id === cur);

    toggleProfileMenu(false);
    $("lmProfileBtn").innerHTML = curProf
      ? profileAvatar(curProf) + profileText(curProf) +
        (curProf.active ? `<span class="drop-badge">使用中</span>` : "") +
        `<span class="drop-chev">▾</span>`
      : `<span class="drop-av k-new">＋</span>` +
        `<span class="drop-txt"><span class="drop-name">新建配置</span>` +
        `<span class="drop-sub">${esc(newProfileSub(full))}</span></span>` +
        `<span class="drop-chev">▾</span>`;

    const items = profiles.map(x =>
      `<button type="button" class="drop-item${x.id === cur ? " on" : ""}"` +
      ` data-pick="${esc(x.id)}" role="option" aria-selected="${x.id === cur}">` +
        `<span class="drop-check">${x.id === cur ? "✓" : ""}</span>` +
        profileAvatar(x) + profileText(x) +
        (x.active ? `<span class="drop-badge">使用中</span>` : "") +
      `</button>`).join("");

    const newItem =
      `<button type="button" class="drop-item new${cur === "new" ? " on" : ""}"` +
      ` data-pick="new"${full ? " disabled" : ""} role="option">` +
        `<span class="drop-check">${cur === "new" ? "✓" : ""}</span>` +
        `<span class="drop-av k-new">＋</span>` +
        `<span class="drop-txt"><span class="drop-name">新建配置</span>` +
        `<span class="drop-sub">${esc(newProfileSub(full))}</span></span>` +
      `</button>`;

    const menu = $("lmProfileMenu");
    menu.innerHTML = (items || `<div class="drop-empty">还没有保存的配置</div>`) +
      (items ? `<div class="drop-sep"></div>` : "") + newItem;
    menu.querySelectorAll("[data-pick]").forEach(b => {
      b.onclick = () => { if (!b.disabled) setProfile(b.dataset.pick); };
    });
  }

  /* 选中某一套（列表里的项 / “＋新建”按钮都走这里） */
  function setProfile(id) {
    $("lmProfile").value = id;
    renderProfileDropdown();
    syncProfileButtons();
    onProfileChange();
  }

  function toggleProfileMenu(open) {
    const btn = $("lmProfileBtn"), menu = $("lmProfileMenu");
    if (open == null) open = menu.hidden;
    menu.hidden = !open;
    btn.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", String(open));
  }

  function onDocClick(e) {
    if (!$("lmProfileDrop").contains(e.target)) toggleProfileMenu(false);
    if (!$("lmProviderDrop").contains(e.target)) providerDrop.toggle(false);
    if (!$("lmLangDrop").contains(e.target)) langDrop.toggle(false);
  }

  function renderProfiles() {
    const sel = $("lmProfile");
    const full = profiles.length >= maxProfiles;
    sel.innerHTML = profiles.map(p =>
      `<option value="${esc(p.id)}">${esc(profileLabel(p))}</option>`
    ).join("") + `<option value="new"${full ? " disabled" : ""}>＋ 新建配置</option>`;
    sel.value = (editingId && profiles.some(p => p.id === editingId)) ? editingId
              : (profiles.some(p => p.id === activeId) ? activeId : "new");
    $("lmMax").textContent = maxProfiles;
    renderProfileDropdown(full);
    syncProfileButtons();
  }

  function syncProfileButtons() {
    const isNew = $("lmProfile").value === "new";
    $("lmActivate").disabled = isNew || !profiles.length;
    $("lmDelete").disabled = isNew || !profiles.length;
    $("lmNew").disabled = profiles.length >= maxProfiles;
  }

  /* 下拉换一个配置：只把它的值填进表单（要保存或点“切换使用”才生效） */
  function onProfileChange() {
    err(""); ok("");
    const v = $("lmProfile").value;
    if (v === "new") {
      const act = profiles.find(p => p.id === activeId) || {};
      fillForm({ provider: act.provider || "deepseek", base_url: "", model: "" }, "", "");
      $("lmProfileHint").innerHTML =
        `正在<b>新建</b>一套配置（还可新建 ${Math.max(0, maxProfiles - profiles.length)} 套），` +
        `填好后点「保存」，保存即切换到这个新配置。`;
    } else {
      const p = profiles.find(x => x.id === v);
      if (!p) return;
      fillForm(p, p.api_key_masked, p.id);
      $("lmProfileHint").innerHTML = p.active
        ? "这套就是<b>当前正在使用</b>的配置。"
        : "点「切换使用」立即生效；或修改后点「保存」，保存后也会切到这套。";
    }
    syncProfileButtons();
  }

  function applyState(cur) {
    profiles = cur.profiles || [];
    activeId = cur.active_id || "";
    maxProfiles = cur.max_profiles || 3;
    renderLangs(cur);
    let sel = profiles.find(p => p.id === editingId);
    if (!sel) { editingId = activeId; sel = profiles.find(p => p.id === activeId); }
    // 表单展示选中的那套；一套都没有时展示“当前生效值”（可能来自环境变量/config.json）
    if (sel) {
      fillForm(sel, sel.api_key_masked, sel.id);
    } else {
      fillForm({ provider: cur.provider, base_url: cur.base_url, model: cur.model },
               cur.api_key_masked, "");
    }
    renderProfiles();
    const act = profiles.find(p => p.id === activeId);
    $("lmProfileHint").innerHTML = act
      ? `当前生效：<b>${esc(profileLabel(act))}</b>（${esc(act.model || act.provider_text || "")}）`
      : "本账号还没保存任何配置，当前用的是安装级默认（环境变量 / config.json / 内置默认）。";
  }

  /* 翻译目标语言下拉：选项由服务端给（`target_langs` 键 -> 名字），加语言不用改前端 */
  function renderLangs(cur) {
    const langs = cur.target_langs || {};
    const keys = Object.keys(langs);
    currentLang = cur.target_lang || cur.default_target_lang || "";
    if (!keys.length) { langDrop.toggle(false); return; }
    const sel = $("lmLang");
    if (sel.dataset.keys !== keys.join(",")) {
      sel.innerHTML = keys.map(k =>
        `<option value="${esc(k)}">${esc(langs[k])}</option>`).join("");
      sel.dataset.keys = keys.join(",");
      langDrop.setNames(langs);
    }
    if (currentLang) sel.value = currentLang;
    langDrop.toggle(false);                 // 刷新时收起浮层（内容变了，重画）
    langDrop.render();
  }

  /* 目标语言换了：阅读页得把上一个语言的译文清掉再按新语言拉一次（服务端也是按语言分开存的） */
  function notifyLangChange(r) {
    const name = (r.target_langs || {})[r.target_lang] || r.target_lang || "";
    toast("翻译目标语言已改为：" + name);
    document.dispatchEvent(new CustomEvent("paper-target-lang",
      { detail: { lang: r.target_lang, name } }));
  }

  /* 保存/切换/删除后，阅读页的“未配置翻译服务”提示跟着变 */
  function syncNotice(r) {
    const n = document.getElementById("notice");
    if (!r.ready) {
      showNotice("⚠️ 未配置翻译服务：译文功能不可用。点右上角「⚙ 设置 → LLM 服务」填写服务地址与 API Key。", true);
    } else if (n && /未配置翻译服务/.test(n.textContent || "")) {
      showNotice("");
    }
  }

  /* 底部那行“当前生效 / Key 来源 / 译文语言 / 已保存几套” */
  function renderState(r) {
    const src = r.source_text || {};
    $("lmState").innerHTML =
      `当前生效：<b>${esc(r.status_text || "未配置")}</b>` +
      `　·　属于账号 <b>${esc(r.owner || "")}</b>` +
      `　·　译文语言 <b>${esc(r.target_lang_text || r.target_lang || "")}</b><br>` +
      `Key 来源：${esc(src.api_key || "-")}` +
      (r.using_account_key
        ? "　（本账号专属）"
        : "　<span class=\"warn\">← 非本账号配置；填上自己的 Key 后只在本账号生效</span>") +
      `　·　已保存 ${(r.profiles || []).length}/${r.max_profiles || maxProfiles} 套配置`;
  }

  function syncProvider() {
    const need = $("lmProvider").value !== "google";
    m.querySelectorAll("[data-llm=need]").forEach(f => { f.hidden = !need; });
    const [base, model] = LLM_PRESET[$("lmProvider").value] || ["", ""];
    if (base && !$("lmBase").value) $("lmBase").value = base;
    if (model && !$("lmModel").value) $("lmModel").value = model;
    providerDrop.render();
  }

  function payload() {
    const key = $("lmKey").value.trim();
    return {
      provider: $("lmProvider").value,
      base_url: $("lmBase").value.trim(),
      model: $("lmModel").value.trim(),
      // 留空/未真输过=不改动(null)；勾选“清除”=传空串清空
      api_key: $("lmClearKey").checked ? "" : ((keyTouched && key !== "") ? key : null),
      profile_id: editingId || "new",   // 有 id 就改写那套，否则新建一套（服务端限制最多 3 套）
      profile_name: $("lmProfileName").value.trim(),   // 自定义名，留空则自动显示“配置 N（模型）”
      // 译文目标语言：跟账号走（users.prefs.target_lang），与上面几套配置槽无关
      target_lang: $("lmLang").value || null,
    };
  }

  function close() {
    document.removeEventListener("click", onDocClick);
    m.remove();
  }

  function renderStorage(s) {
    const el2 = $("lmStorage");
    const mo = s.mongo || {};
    // 连的哪个库、用哪个账号、参数从哪来 —— 连不上时看这三样就能定位
    const who = mo.user
      ? `<br>连接信息：<code>${esc(mo.user)}</code> @ <code>${esc(mo.host || "")}</code>` +
        `　·　库 <code>${esc(mo.db || "")}</code>` +
        (mo.source ? `　·　参数来自 ${esc(mo.source)}` : "")
      : "";
    // 认证失败时后端会给一段修复建议；已包含在 warning 里就别重复
    const hint = (!mo.ok && mo.hint && !(s.warning || "").includes(mo.hint))
      ? `<br>💡 ${esc(mo.hint)}` : "";
    if (s.backend === "mongo") {
      el2.innerHTML = `✅ <b>MongoDB</b>：${esc(mo.host || "")} / 库 <code>${esc(mo.db || "")}</code>` +
        (s.papers != null ? `（论文 ${s.papers} 篇）` : "") + who;
      $("lmReconnect").hidden = true;
    } else {
      el2.innerHTML = `📁 <b>本地 JSON</b>：<code>${esc(s.data_dir || "")}/</code>` +
        (s.warning ? `<br>⚠️ ${esc(s.warning)}` : "") + who + hint;
      $("lmReconnect").hidden = s.preferred === "json";
    }
  }

  async function loadStorage() {
    try {
      renderStorage(await api("/api/storage/status"));
    } catch (e) {
      $("lmStorage").textContent = "数据存储：状态获取失败（" + e.message + "）";
    }
  }

  /* PDF 解析后端：
       · auto / pymupdf / detection_service_group：**本地解析方案** —— PyMuPDF 版式
         + texify 公式识别 + OXR 版面分析 + SLANet 表格识别，前端逐行绝对定位渲染；
       · surya：整页 OCR —— 文字 / 公式（<math>→LaTeX）/ 插图全用 Surya 的数据；
       · unlimited_ocr：整页 OCR —— 文字 / 公式（LaTeX）/ 表格（HTML）/ 插图全用
         Unlimited-OCR 的数据。
     服务没起 / 依赖没装时这里如实显示状态。 */
  const OCR_TEXT = {
    detection_service_group: "本地解析方案（不调用服务组）",
    surya: "Surya 2 推理服务（文字 / 公式 / 插图全用 Surya）",
    unlimited_ocr: "Unlimited-OCR 推理服务（文字 / 公式 / 表格 / 插图全用 OCR）",
    pymupdf: "本地解析方案（PyMuPDF + 本地公式/表格模型）",
    unavailable: "不可用（本地解析方案的模型/依赖未就绪）",
  };
  const OCR_NAME = { auto: "自动（本地解析方案）", detection_service_group: "本地解析方案（不调用服务组）", surya: "Surya 2", unlimited_ocr: "Unlimited-OCR", pymupdf: "本地解析方案" };

  function renderParser(p) {
    const el2 = $("lmParser");
    if (!p) { el2.textContent = "状态获取失败（可能未登录）"; return; }
    if ($("ocrBackend") && p.backend) {
      $("ocrBackend").value = p.backend;
      ocrDrop.render();               // 服务端配置可能变了：同步卡片上的文字
    }
    const eff = p.effective || "unavailable";
    const effOk = eff === "pymupdf" || eff === "surya" || eff === "unlimited_ocr";
    let html = `${effOk ? "✅" : "⛔"} 实际使用：` +
      `<b>${esc(OCR_TEXT[eff] || eff)}</b>（配置：<code>${esc(p.backend || "")}</code>）`;
    // 本地解析方案：PyMuPDF + texify / OXR 版面 / SLANet（模型与依赖都要就绪）
    html += `<br>${p.local_ready ? "✅" : "⛔"} 本地解析方案 ` +
      `<code>${esc(p.local_model_dir || "local_parse_pipeline/models")}</code>` +
      (p.local_ready ? "（模型与依赖已就绪）"
                     : `（${esc(p.local_message || "模型/依赖未就绪")}）`);
    html += `<br>${p.surya_ready ? "✅" : "⛔"} Surya 2 ` +
      `<code>${esc(p.surya_url || "")}</code>` +
      (p.surya_client_ready === false ? "（本机缺 surya-ocr 客户端依赖）" : "") +
      (p.surya_message ? `<span title="${esc(p.surya_message)}">` : "") +
      "</span>";
    html += `<br>${p.unlimited_ocr_ready ? "✅" : "⛔"} Unlimited-OCR ` +
      `<code>${esc(p.unlimited_ocr_url || "")}</code>` +
      `（模型 <code>${esc(p.unlimited_ocr_model || "")}</code>）` +
      (p.unlimited_ocr_message
        ? `<span title="${esc(p.unlimited_ocr_message)}"> ⓘ</span>` : "");
    if (p.message) html += `<br>ℹ️ ${esc(p.message)}`;
    if (p.env_override) html += `<br>⚠️ 已设置环境变量 <code>PDF_PARSER_BACKEND</code>，` +
      `它会<b>覆盖</b>这里保存的值（环境变量优先级高于 <code>config.json</code>）—— ` +
      `要按面板的设置生效，请去掉它（<code>docker/.env</code> / compose 里的 ` +
      `<code>PDF_PARSER_BACKEND</code>）再 <code>docker compose up -d</code> 重建容器`;
    for (const w of (p.warnings || [])) html += `<br>⚠️ ${esc(w)}`;
    el2.innerHTML = html;
  }

  async function loadParser() {
    try {
      // 强制刷新：状态随服务/依赖变化，不能吃 appConfig 的缓存
      const cfg = await appConfig(true);
      renderParser(cfg && cfg.parser);
    } catch (e) {
      $("lmParser").textContent = "PDF 解析：状态获取失败（" + e.message + "）";
    }
  }

  /* 保存解析后端：安装级配置，写 config.json 的 parser.backend */
  $("ocrSave").onclick = async () => {
    const v = $("ocrBackend").value;
    const btn = $("ocrSave");
    btn.disabled = true; btn.textContent = "保存中…";
    try {
      const r = await api("/api/settings/parser", { method: "POST", json: { backend: v } });
      const p = r.parser || r;
      renderParser(p);
      // 环境变量优先级高于 config.json：这种情况“保存成功”≠“真的切过去了”，
      // 必须直说，否则看起来就像“选了 pymupdf 但解析依旧走 OCR”。
      toast(p.env_override
        ? "已写入配置，但环境变量 PDF_PARSER_BACKEND 会覆盖它（见下方提示）"
        : "PDF 解析后端已切换为：" + (OCR_NAME[v] || v));
      appConfig(true);            // 让其它页面读到最新状态
    } catch (e) {
      $("lmParser").innerHTML = `❌ 切换失败：${esc(e.message)}`;
    } finally {
      btn.disabled = false; btn.textContent = "保存解析后端";
    }
  };

  async function load() {
    try {
      const cur = await api("/api/settings/llm");
      editingId = cur.active_id || "";    // 默认打开“当前生效”的那套
      applyState(cur);
      renderState(cur);
    } catch (e) { err(e.message); }
  }

  $("lmSave").onclick = async () => {
    err(""); ok("");
    const langBefore = currentLang;                 // 保存前服务端的语言，用来判断“这次是否换了语言”
    try {
      const r = await api("/api/settings/llm", { method: "POST", json: payload() });
      editingId = r.active_id || editingId;      // 保存即切换：新/改的那套成为当前生效
      const saved = (r.profiles || []).find(p => p.id === editingId);
      applyState(r);
      ok("✅ 已保存到本账号（" + (r.owner || "") + "），当前生效：" + (r.status_text || "") +
         "；已保留 " + profiles.length + "/" + maxProfiles + " 套配置");
      toast("LLM 设置已保存（本账号）：" + (saved ? profileLabel(saved) : ""));
      renderState(r);
      syncNotice(r);
      if (r.target_lang && r.target_lang !== langBefore) notifyLangChange(r);
    } catch (e) { err(e.message); }
  };

  /* 只切换生效的配置，不改写内容 */
  $("lmActivate").onclick = async () => {
    err(""); ok("");
    const id = $("lmProfile").value;
    const p = profiles.find(x => x.id === id);
    if (!p) { err("请先选中一套配置"); return; }
    const btn = $("lmActivate");
    btn.disabled = true;
    try {
      const r = await api("/api/settings/llm/activate", { method: "POST", json: { profile_id: id } });
      editingId = r.active_id || id;
      applyState(r);
      ok("✅ 已切换到「" + profileLabel(p) + "」，当前生效：" + (r.status_text || ""));
      toast("已切换 LLM 配置：" + profileLabel(p));
      renderState(r);
      syncNotice(r);
    } catch (e) { err(e.message); } finally { btn.disabled = false; syncProfileButtons(); }
  };

  $("lmNew").onclick = () => {
    if (profiles.length >= maxProfiles) {
      err("最多只能保留 " + maxProfiles + " 套配置，请先删除一套再新建");
      return;
    }
    setProfile("new");
    $("lmKey").focus();
  };

  $("lmDelete").onclick = async () => {
    err(""); ok("");
    const id = $("lmProfile").value;
    const p = profiles.find(x => x.id === id);
    if (!p) return;
    if (!confirm(`删除配置「${profileLabel(p)}」？\n\n` +
      `只删掉这套保存的地址/Key/模型，其它配置与论文数据不受影响。` +
      (p.active ? "\n\n这是当前生效的配置，删除后会自动切到剩下的一套。" : ""))) return;
    const btn = $("lmDelete");
    btn.disabled = true;
    try {
      const r = await api("/api/settings/llm/delete", { method: "POST", json: { profile_id: id } });
      applyState(r);
      ok("已删除配置「" + profileLabel(p) + "」，还剩 " + profiles.length + " 套");
      renderState(r);
      syncNotice(r);
    } catch (e) { err(e.message); } finally { btn.disabled = false; syncProfileButtons(); }
  };

  $("lmTest").onclick = async () => {
    err(""); ok("");
    const btn = $("lmTest");
    btn.disabled = true; btn.textContent = "测试中…";
    try {
      const r = await api("/api/settings/llm/test", { method: "POST", json: payload() });
      ok("✅ " + r.message);
    } catch (e) { err("❌ " + e.message); } finally {
      btn.disabled = false; btn.textContent = "测试连接";
    }
  };

  m.querySelectorAll("[data-close]").forEach(b => b.onclick = close);
  $("lmProfileBtn").onclick = () => toggleProfileMenu();
  document.addEventListener("click", onDocClick);
  $("lmKey").addEventListener("input", () => { keyTouched = true; });

  /* ---- 页签：LLM 服务 / 存储与 OCR / 账号 ---- */
  function switchTab(which) {
    const isLlm = which === "llm";
    const isSys = which === "sys";
    const isAcc = which === "acc";
    toggleProfileMenu(false);        // 切页签时收起配置下拉
    providerDrop.toggle(false);      // 服务类型下拉也一并收起
    langDrop.toggle(false);          // 目标语言下拉同理
    ocrDrop.toggle(false);           // OCR 后端下拉也一样
    $("tabLlm").classList.toggle("on", isLlm);
    $("tabSys").classList.toggle("on", isSys);
    $("tabAcc").classList.toggle("on", isAcc);
    $("paneLlm").hidden = !isLlm;
    $("paneSys").hidden = !isSys;
    $("paneAcc").hidden = !isAcc;
    $("lmFoot").hidden = !isLlm;    // 底部按钮只属于 LLM 页签
    if (isAcc) loadUsers();
    if (isSys) { loadStorage(); loadParser(); }   // 进面板时刷新存储 / 解析状态
  }
  $("tabLlm").onclick = () => switchTab("llm");
  $("tabSys").onclick = () => switchTab("sys");
  $("tabAcc").onclick = () => switchTab("acc");

  /* ---- 账号管理：列表 / 新增 / 删除 / 切换 ---- */
  const accErr = msg => { const n = $("accErr"); n.textContent = msg || ""; n.hidden = !msg; };
  const accOk = msg => { const n = $("accOk"); n.textContent = msg || ""; n.hidden = !msg; };

  function renderUsers(data) {
    $("accCur").textContent = "已登录：" + (data.current || "");
    const list = data.users || [];
    currentUser = data.current || "";
    const me = list.find(u => u.username === data.current);
    myPapers = me && me.papers != null ? me.papers : 0;
    $("closeName").placeholder = "输入用户名 " + currentUser + " 以确认";
    $("accList").innerHTML = list.length ? list.map(u => {
      const self = u.username === data.current;
      // 点整行 = 切到这个账号（会退出现有登录，登录页把账号名预填好）
      return `<div class="accrow${self ? " self" : " switchable"}"` +
        (self ? "" : ` data-switch="${esc(u.username)}" title="点击切到这个账号登录"`) + ">" +
        `<span class="an">${esc(u.username)}</span>` +
        `<span class="ap">论文 ${u.papers != null ? u.papers : "?"} 篇</span>` +
        `<span class="at">${self ? "当前" : esc((u.created_at || "").replace("T", " "))}</span>` +
        (self ? "" : `<button class="btn ghost" data-del="${esc(u.username)}" ` +
          `data-papers="${u.papers != null ? u.papers : 0}" type="button">删除</button>`) +
        `</div>`;
    }).join("") : "（还没有账号）";
    $("accList").querySelectorAll("button[data-del]").forEach(b => {
      b.onclick = e => { e.stopPropagation(); delUser(b.dataset.del, Number(b.dataset.papers || 0)); };
    });
    $("accList").querySelectorAll(".accrow[data-switch]").forEach(row => {
      row.onclick = () => switchTo(row.dataset.switch);
    });
  }

  // 切到指定账号：退出当前登录 → 登录页已填好账号名，只需输密码
  function switchTo(name) {
    if (name === currentUser) return;
    if (!confirm(`切换到账号 ${name} ？\n\n会先退出当前登录（${currentUser}），论文、笔记、译文都不会丢。`)) return;
    logout(name);
  }

  async function loadUsers() {
    accErr(""); accOk("");
    try { renderUsers(await api("/api/auth/users")); }
    catch (e) { $("accList").textContent = "读取失败：" + e.message; }
  }

  async function delUser(name, papers) {
    if (!confirm(`删除账号 ${name} ？\n它名下有 ${papers} 篇论文。`)) return;
    let purge = true;
    if (papers > 0 && !confirm(
      `同时删除 ${name} 名下的 ${papers} 篇论文（含标注/笔记/译文）？\n\n` +
      `确定 = 连数据一起删（不可恢复）\n取消 = 只删账号，论文保留在库里但不再可见`)) {
      purge = false;
    }
    accErr(""); accOk("");
    try {
      const r = await api("/api/auth/users/" + encodeURIComponent(name) + "?purge=" + (purge ? "1" : "0"),
                          { method: "DELETE" });
      loadUsers();
      accOk(purge ? `已删除账号 ${name} 及其 ${r.removed_papers} 篇论文`
                  : `已删除账号 ${name}（论文数据保留在库里）`);
    } catch (e) { accErr(e.message); }
  }

  $("accAdd").onclick = async () => {
    accErr(""); accOk("");
    const username = $("accName").value.trim();
    const password = $("accPwd").value;
    if (!username || !password) { accErr("请填写用户名和密码"); return; }
    const btn = $("accAdd");
    btn.disabled = true;
    try {
      renderUsers(await api("/api/auth/users", { method: "POST", json: { username, password } }));
      $("accName").value = ""; $("accPwd").value = "";
      accOk("已添加账号 " + username + "：点下面「切换账号」就能用它登录");
    } catch (e) { accErr(e.message); } finally { btn.disabled = false; }
  };

  $("accSwitch").onclick = () => {
    if (confirm("切换账号将退出当前登录（论文、笔记、译文都不会丢），确定吗？")) logout();
  };
  /* ---- 注销当前账号：删号 + 清空该账号名下数据，不可恢复 ---- */
  $("accClose").onclick = async () => {
    accErr(""); accOk("");
    const pwd = $("closePwd").value;
    const name = $("closeName").value.trim();
    if (!pwd || !name) { accErr("请填写当前密码，并输入用户名确认"); return; }
    if (name.toLowerCase() !== currentUser.toLowerCase()) {
      accErr("确认用户名与当前账号不一致"); return;
    }
    if (!confirm(
      `⚠️ 确认注销账号 ${currentUser} ？\n\n` +
      `该账号将立即被删除，名下 ${myPapers} 篇论文及其阅读数据（标注 / 笔记 / 译文）会一起永久清除。\n` +
      `此操作无法恢复！\n\n要真的注销吗？`)) return;
    const btn = $("accClose");
    btn.disabled = true;
    try {
      const r = await api("/api/auth/deactivate",
                          { method: "POST", json: { password: pwd, confirm: name } });
      toast(`账号 ${r.username} 已注销，已清除 ${r.removed_papers} 篇论文`, 5000);
      setTimeout(() => location.replace("login.html"), 1200);
    } catch (e) {
      accErr(e.message);
      btn.disabled = false;
    }
  };

  const sysErr = msg => { const n = $("sysErr"); n.textContent = msg || ""; n.hidden = !msg; };

  $("lmReconnect").onclick = async () => {
    sysErr("");
    const btn = $("lmReconnect");
    btn.disabled = true; btn.textContent = "重连中…";
    try {
      const s = await api("/api/storage/reconnect", { method: "POST" });
      renderStorage(s);
      toast("存储后端：" + (s.backend === "mongo" ? "MongoDB ✅" : "本地 JSON"));
      if (s.relogin) {
        toast("已切到 MongoDB，账号需已迁移，请重新登录", 5000);
        setTimeout(() => location.replace("login.html"), 1200);
        return;
      }
      setTimeout(() => location.reload(), 800);
    } catch (e) {
      sysErr(e.message);
    } finally {
      btn.disabled = false; btn.textContent = "重试连接 MongoDB";
    }
  };
  document.addEventListener("keydown", function onEsc(e) {
    if (e.key !== "Escape" || !document.getElementById("llmModal")) return;
    if (providerDrop.isOpen()) { providerDrop.toggle(false); return; }  // 先收下拉
    if (langDrop.isOpen()) { langDrop.toggle(false); return; }
    if (ocrDrop.isOpen()) { ocrDrop.toggle(false); return; }
    if (!$("lmProfileMenu").hidden) { toggleProfileMenu(false); return; }
    close();
    document.removeEventListener("keydown", onEsc);
  });
  load();
  if (tab === "acc") switchTab("acc");
  else if (tab === "sys") switchTab("sys");
}

document.addEventListener("DOMContentLoaded", mountUserBar);

/* 存储状态：没连上 MongoDB（还在用本地 JSON）时在首页提示一次，
   详细状态与“重试连接”在「⚙ 设置 → 存储 / OCR」弹窗里。 */
async function storageHint() {
  if (!document.querySelector(".topbar")) return;
  const cfg = await appConfig();
  const s = cfg && cfg.storage;
  if (s && s.backend !== "mongo" && s.preferred !== "json") {
    toast("⚠️ 未连接 MongoDB，正在用本地 JSON 存储（点右上角 ⚙ 设置 → 存储 / OCR 可重试连接）", 6000);
  }
}
document.addEventListener("DOMContentLoaded", storageHint);
