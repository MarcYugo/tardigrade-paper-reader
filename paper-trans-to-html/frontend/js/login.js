/* 登录页：① 登录 / 注册新账号 / 首次创建管理员账号  ② 首次登录引导配置 LLM 服务 */
(function () {
  const $ = id => document.getElementById(id);
  const LLM_PRESET = {
    deepseek: ["https://api.deepseek.com", "deepseek-chat"],
    openai: ["https://api.openai.com/v1", "gpt-4o-mini"],
    anthropic: ["https://api.anthropic.com/v1", "claude-sonnet-4-5"],
    custom: ["", ""],
    google: ["", ""],
  };
  // 回跳地址只接受站内相对路径（与服务端 _safe_next 同一套规则）
  const next = (() => {
    const v = queryId("next");
    return (v && v.startsWith("/") && !v.startsWith("//")) ? v : "/";
  })();
  // ?u=账号：从「账号」列表里点了某个账号切过来 → 预填账号名，只需输密码
  const presetUser = (() => {
    const v = (queryId("u") || "").trim();
    return /^[A-Za-z0-9_.@-]{3,32}$/.test(v) ? v : "";
  })();
  let mode = "login";        // login | register | setup
  let signupEnabled = true;  // 服务端是否允许未登录自助注册（ALLOW_SIGNUP）
  let info = {};             // /api/auth/status 的结果
  let keyTouched = false;    // API Key 是否被用户真正输入过（防浏览器自动填充误提交）

  function err(box, msg) { const n = $(box); n.textContent = msg || ""; n.hidden = !msg; }

  function gotoStep(n) {
    $("accForm").hidden = n !== 1;
    $("llmForm").hidden = n !== 2;
    document.querySelectorAll("#steps li").forEach(li => {
      const s = Number(li.dataset.s);
      li.classList.toggle("on", s <= n);
      li.classList.toggle("cur", s === n);
    });
  }

  /* ---------- 第一步：登录 / 注册 / 首次创建管理员 ---------- */
  function setTip() {
    const t = $("accTip");
    if (mode === "setup") {
      t.textContent = "这是本机自用服务：第一个账号就是管理员，请妥善保管密码。";
    } else if (mode === "register") {
      t.textContent = signupEnabled
        ? "注册后直接登录。用户名 3-32 位（字母/数字/_ . @ -），密码至少 8 位。"
        : "本服务已关闭自助注册（ALLOW_SIGNUP=0）。";
    } else {
      t.textContent = info.llm_configured
        ? "当前翻译服务：" + (info.translator || "已配置")
        : "尚未配置 LLM 服务，登录后可在右上角「⚙ 设置」中配置。";
    }
  }

  function setMode(m) {
    mode = m;
    const isReg = m === "register", isSetup = m === "setup";
    err("accErr", "");
    $("loginTitle").textContent = isSetup ? "首次使用 · 创建管理员账号"
                              : isReg ? "注册新账号" : "登录";
    $("loginSub").textContent = isSetup ? "本服务还没有账号，创建后即可登录"
                              : isReg ? "注册完成会直接登录；每个账号有自己独立的论文数据"
                              : "论文 PDF → HTML 阅读器";
    $("confirmWrap").hidden = !(isReg || isSetup);
    $("accBtn").textContent = isSetup ? "创建并继续" : isReg ? "注册并登录" : "登录";
    $("password").setAttribute("autocomplete", (isReg || isSetup) ? "new-password" : "current-password");
    $("accTabs").hidden = isSetup || !signupEnabled;
    $("tabLogin").classList.toggle("on", !isReg);
    $("tabReg").classList.toggle("on", isReg);
    $("switchAcc").textContent = isReg ? "已有账号？直接登录 →" : "使用其他账号登录 →";
    setTip();
  }

  async function submitAcc(e) {
    e.preventDefault();
    err("accErr", "");
    const username = $("username").value.trim();
    const password = $("password").value;
    const btn = $("accBtn");
    if (mode !== "login") {                 // 注册 / 首次创建：多一道确认与长度校验
      if (password.length < 8) { err("accErr", "密码至少 8 位"); return; }
      if (password !== $("password2").value) { err("accErr", "两次输入的密码不一致"); return; }
    }
    btn.disabled = true;
    try {
      const path = mode === "setup" ? "/api/auth/setup"
                 : mode === "register" ? "/api/auth/register"
                 : "/api/auth/login";
      const r = await api(path, { method: "POST", json: { username, password, next }, noRedirect: true });
      if (r.need_llm) {
        await showLlmStep(true);           // 首次登录：引导填 LLM（可点“稍后配置”跳过）
      } else {
        toast(mode === "login" ? "登录成功 ✅" : "账号 " + r.username + " 已创建，已登录 ✅");
        location.replace(r.next || next);
      }
    } catch (e2) {
      err("accErr", e2.message);
    } finally {
      btn.disabled = false;
    }
  }

  /* ---------- 第二步：LLM 配置 ---------- */
  function syncProvider() {
    const p = $("provider").value;
    const need = p !== "google";
    document.querySelectorAll("#llmForm [data-llm=need]").forEach(f => { f.hidden = !need; });
    const [base, model] = LLM_PRESET[p] || ["", ""];
    if (base && !$("base_url").value) $("base_url").value = base;
    if (model && !$("model").value) $("model").value = model;
    providerDrop.render();   // 同步自绘下拉的按钮与浮层
  }

  function payload() {
    const key = $("api_key").value.trim();
    const provider = $("provider").value;
    // Google 免费兜底不需要地址 / 模型：别把切换前残留的模型名一起提交
    const isGoogle = provider === "google";
    return {
      provider,
      base_url: isGoogle ? "" : $("base_url").value.trim(),
      model: isGoogle ? "" : $("model").value.trim(),
      // 留空 = 沿用已保存的 Key（传 null），不是清空；
      // 没被用户输入过(浏览器自动填充)也当作“不改动”
      api_key: (keyTouched && key !== "") ? key : null,
    };
  }

  async function showLlmStep(first) {
    gotoStep(2);
    $("loginTitle").textContent = "配置 LLM 服务";
    $("loginSub").textContent = first ? "首次使用，先设置翻译用的模型服务" : "更新翻译用的模型服务";
    $("llmBtn").textContent = first ? "保存并进入" : "保存";
    try {
      const cur = await api("/api/settings/llm", { noRedirect: true });
      $("provider").value = cur.provider || "deepseek";
      $("base_url").value = cur.base_url || "";
      $("model").value = cur.model || "";
      $("api_key").value = "";
      $("api_key").placeholder = cur.api_key_set ? ("已保存：" + cur.api_key_masked + "（留空不改）") : "sk-...";
      syncProvider();
    } catch (e) { /* 读不到就用默认值 */ }
  }

  async function submitLlm(e) {
    e.preventDefault();
    err("llmErr", "");
    $("llmOk").hidden = true;
    const btn = $("llmBtn");
    const body = payload();
    const savedKey = /已保存/.test($("api_key").placeholder);
    if (body.provider !== "google" && !body.api_key && !savedKey) {
      err("llmErr", "请填写 API Key（或选“Google 免费兜底”跳过）");
      return;
    }
    btn.disabled = true;
    try {
      await api("/api/settings/llm", { method: "POST", json: body });
      toast("已保存 ✅");
      location.replace(next);
    } catch (e2) {
      err("llmErr", e2.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function testLlm() {
    err("llmErr", "");
    $("llmOk").hidden = true;
    const btn = $("testBtn");
    btn.disabled = true;
    btn.textContent = "测试中…";
    try {
      const r = await api("/api/settings/llm/test", { method: "POST", json: payload() });
      const ok = $("llmOk");
      ok.textContent = "✅ " + r.message;
      ok.hidden = false;
    } catch (e2) {
      err("llmErr", "❌ " + e2.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "测试连接";
    }
  }

  /* ---------- 初始化 ---------- */
  async function boot() {
    let st = null;
    try {
      st = await api("/api/auth/status", { noRedirect: true });
    } catch (e) {
      err("accErr", "无法连接后端服务，请确认 python run.py 已启动");
      $("accBtn").disabled = true;
      return;
    }
    info = st;
    if (st.authenticated) { location.replace(next); return; }

    signupEnabled = st.signup !== false;
    if (st.storage === "json" && st.storage_warning) {
      const w = $("loginWarn");
      w.innerHTML = "⚠️ " + esc(st.storage_warning) + "（数据暂存在 <code>data/</code> 下的 JSON 文件里）";
      w.hidden = false;
    }
    setMode(st.initialized ? "login" : "setup");

    // 从「账号」列表点某个账号切过来：账号名自动填好，光标直接落到密码框
    if (presetUser && mode !== "setup") {
      $("username").value = presetUser;
      $("password").value = "";
      $("password2").value = "";
      $("accTip").textContent = `请为账号 ${presetUser} 输入密码（要换别的账号就点下面「使用其他账号登录 →」）。`;
      toast("已退出登录：请输入密码", 2200);
      $("password").focus();
      return;
    }
    // 刚从“退出登录/切换账号”过来：清掉可能被浏览器回填的旧账号
    if (queryId("switch")) {
      $("username").value = "";
      $("password").value = "";
      $("password2").value = "";
      $("accTip").textContent = "已退出登录：请输入账号，或点上面「注册新账号」建一个，点下面清空重填。";
      toast("已退出登录", 2200);
    }
    $("username").focus();
  }

  /* 服务类型下拉：复用 common.js 里的公共组件，与登录后「设置 → LLM 服务」同一套外观 */
  const providerDrop = createProviderDropdown({
    select: $("provider"), btn: $("providerBtn"), menu: $("providerMenu"),
    onChange: () => syncProvider(),   // 重绘按钮，并按需隐藏 / 预填服务地址与模型
  });
  document.addEventListener("click", e => {
    if (!$("providerDrop").contains(e.target)) providerDrop.toggle(false);
  });

  $("accForm").addEventListener("submit", submitAcc);
  $("llmForm").addEventListener("submit", submitLlm);
  $("testBtn").addEventListener("click", testLlm);
  $("api_key").addEventListener("input", () => { keyTouched = true; });
  // 登录 / 注册 切换（未登录也能注册新账号）
  $("tabLogin").addEventListener("click", () => setMode("login"));
  $("tabReg").addEventListener("click", () => setMode(signupEnabled ? "register" : "login"));
  // 换账号：注册模式下回到登录；登录模式下清空输入框（浏览器可能已把上个账号回填进来）
  $("switchAcc").addEventListener("click", e => {
    e.preventDefault();
    if (mode === "register") { setMode("login"); $("username").focus(); return; }
    $("username").value = "";
    $("password").value = "";
    $("password2").value = "";
    $("accTip").textContent = "请输入另一账号的账号密码；还没有账号就点上面「注册新账号」。";
    $("username").focus();
  });
  $("skipLlm").addEventListener("click", e => {
    e.preventDefault();
    toast("已跳过：翻译将使用免费兜底(可能不可用)", 3000);
    location.replace(next);
  });

  boot();
})();
