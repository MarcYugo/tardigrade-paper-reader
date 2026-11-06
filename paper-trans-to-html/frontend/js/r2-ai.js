/* ============ AI 辅助阅读（笔记整理 + 对话）：从 reader2.js 拆出来的独立模块 ============
   与 r2-math.js 同一套路：原来那段代码**按原样搬过来**（缩进没动，方便对照），
   只有两类改动：
     ① 共享状态的读法 —— 原来直接读外层闭包的 `META` / `anno`，现在走 `env.META` /
        `env.anno`（这两个都是 let，换论文 / 重新拉批注时会被整体替换，
        不能在装配时抓一次快照）。
     ② 顺手删掉 `saveAiSummary()` 里重复写了两遍的同一行 `needWrite(...)`（无行为差异）。

   装配（reader2.js 原位置）：
     const { maybeAutoSummarize, syncAiButtons, jumpToAi, renderTocAi, loadAi } =
       R2Ai({ $, docId, findItem, keyParts, pageOfItem,
              needWrite, canWrite: (...a) => canWrite(...a),
              renderInlineMath, katexReady, ensureKatex,
              get META() { return META; }, get anno() { return anno; } });
   注：`canWrite` 声明在 AI 段**之后**（共享权限那一段），所以这里只能包一层转发，
   调用时才会真正去读那个 const（装配那一刻它还处于 TDZ）。

   2026-10 起：对话气泡（含流式增量）与整理结果里的自由文本都走 **Markdown 渲染**
   （r2-md.js 自带的小渲染器，不引第三方库）；文字里若带 $…$ / \(…\) 数学，
   再借 r2-math.js 的 renderInlineMath() 就地换成 KaTeX —— reader2.js 装配时把
   renderInlineMath / katexReady / ensureKatex 三个函数传进来（可选，缺了也不影响）。

   AI 自己的状态（aiSummary / aiChat / aiBusyWhat / aiEdit / …）全部随本文件走，
   不再占用 reader2.js 的作用域；段内 `const busy = …` 是**同名局部变量**（按钮置灰用），
   与外层的 `busy` 无关，所以这里不需要注入 busy。
   ============================================================================ */
window.R2Ai = function (env) {
  "use strict";
  const { $, docId, findItem, keyParts, pageOfItem, needWrite, canWrite } = env;
  // 可选：KaTeX 三件套（AI 文字里的行内数学用），由 reader2.js 装配传入
  const { renderInlineMath, katexReady, ensureKatex } = env;

  /* ================= 文末 · AI 辅助阅读（笔记整理 + 对话） =================
     读到 ≥95% 时自动把这篇的笔记整理一次（结果存服务端，不重复花 token）；
     左边对话、右边整理结果。笔记上下文由前端组装 —— 只有这里知道
     「这条笔记挂在哪句话上」（block_id = "块id#句号"）。 */
  const AI_TRIGGER = 0.95;
  let aiSummary = null, aiChat = [], aiAutoTried = false, aiStale = false;
  let aiBusyWhat = "";     // "" | "summary" | "chat"：正在忙什么（工作状态显示在聊天框里）
  let aiMeta = {};          // 最近一次服务端状态（整理时间/笔记条数/是否配了 LLM）
  let aiDraft = null;       // 流式回复的已收到文本；null = 还没开始/已结束
  let aiDraftEl = null;     // 上面那个气泡的 DOM，增量更新用（不整框重画）
  let aiToolStatus = "";
  let aiPendingNavigation = null;
  const aiCollapsedKey = "paperReader.aiCollapsed." + docId;
  let aiCollapsed = true;
  try {
    const savedCollapseState = localStorage.getItem(aiCollapsedKey);
    if (savedCollapseState !== null) aiCollapsed = savedCollapseState === "1";
  } catch (e) { /* storage unavailable */ }
  let aiEdit = "";          // 正在就地编辑哪一块（aiKeyOf(...) 的字符串；空串 = 没在编辑）
  let aiSaving = false;     // 正在把手改的整理存回服务端
  let aiMathTried = false;  // 已为文字里的数学懒加载过一次 KaTeX（失败也别反复试）
  let aiDraftRaf = 0;       // 流式重绘的 rAF 句柄（增量猛进来时每帧最多渲染一次）
  const aiBusy = () => aiBusyWhat !== "";

  function positionAiCard() {
    const pager = $("pageInd");
    const page = $("pages").querySelector(".page");
    const section = $("aiEnd");
    if (!page || !section) return;
    const zoom = page.offsetWidth ? page.getBoundingClientRect().width / page.offsetWidth : 1;
    const pagerRect = pager ? pager.getBoundingClientRect() : null;
    const viewW = document.documentElement.clientWidth;
    const viewH = document.documentElement.clientHeight;
    /* 阅读区底部给卡片留位置：展开时留出卡片高度 + 12px，
       文末最后一段就能滚到卡片上方（到底部时不会被浮层盖住）；收起时恢复默认 90px。 */
    const syncAiReserve = () => {
      const main = $("readerMain");
      if (!main) return;
      const top = section.getBoundingClientRect().top;
      const pad = aiCollapsed ? 0 : Math.max(0, Math.round(viewH - top / zoom) + 12);
      main.style.paddingBottom = pad ? pad + "px" : "";
    };
    if (aiCollapsed && pagerRect) {
      /* 收起态只有一行标题：贴在页码指示器左侧，底部与它对齐 */
      const gap = 10 * zoom;
      section.style.width = "auto";
      section.style.maxWidth = Math.max(80, pagerRect.left - gap - 12) + "px";
      section.style.left = "auto";
      section.style.right = Math.max(0, viewW - pagerRect.left / zoom + gap) + "px";
      section.style.bottom = Math.max(0, viewH - pagerRect.bottom / zoom) + "px";
      syncAiReserve();
      return;
    }
    section.style.maxWidth = "";
    /* 展开态：宽度取窗口宽度的 95%，在“避开左右抽屉面板后的可用区间”里居中 */
    const cls = document.body.classList;
    const leftGap = (cls.contains("toc-open") ? 316 : 12) * zoom;
    const rightGap = (cls.contains("side-open") ? 400 : 12) * zoom;
    const availW = Math.max(240 * zoom, viewW * zoom - leftGap - rightGap);
    const width = Math.min(viewW * 0.95 * zoom, availW);
    const left = leftGap + (availW - width) / 2;
    section.style.left = (left / zoom) + "px";
    section.style.right = "auto";
    section.style.width = (width / zoom) + "px";
    /* 底边贴着页码指示器上方（留 8px），而不是固定 68px 的空当 */
    section.style.bottom = pagerRect
      ? Math.max(0, viewH - pagerRect.top / zoom + 8) + "px"
      : "68px";
    syncAiReserve();
  }

  function setAiCollapsed(collapsed, persist = true) {
    aiCollapsed = !!collapsed;
    const body = $("aiBody");
    const toggle = $("aiToggle");
    const section = $("aiEnd");
    body.hidden = aiCollapsed;
    toggle.setAttribute("aria-expanded", String(!aiCollapsed));
    toggle.title = aiCollapsed ? "展开 AI 辅助阅读和笔记" : "收起 AI 辅助阅读和笔记";
    toggle.setAttribute("aria-label", toggle.title);
    section.classList.toggle("ai-collapsed", aiCollapsed);
    positionAiCard();
    if (!aiCollapsed) { env.layoutAi(); positionAiCard(); }   // 堆叠/两栏定下后卡片高度变了，预留重算一次
    if (persist) {
      try { localStorage.setItem(aiCollapsedKey, aiCollapsed ? "1" : "0"); }
      catch (e) { /* storage unavailable */ }
    }
  }
  setAiCollapsed(aiCollapsed, false);
  window.addEventListener("resize", positionAiCard);
  if (window.ResizeObserver) {
    const pageResizeObserver = new ResizeObserver(positionAiCard);
    const observePage = () => {
      const page = $("pages").querySelector(".page");
      if (page) pageResizeObserver.observe(page);
      pageResizeObserver.observe($("pageInd"));
    };
    observePage();
    new MutationObserver(observePage).observe($("pages"), { childList: true });
    /* 目录 / 笔记抽屉开合（body 上的 toc-open / side-open）会改变可用宽度：
       先按新宽度重新定位，再重新判断两栏还是堆叠，最后按新高度算底部预留 */
    new MutationObserver(() => {
      positionAiCard();
      if (!aiCollapsed) env.layoutAi();
      positionAiCard();
    }).observe(document.body, { attributes: true, attributeFilter: ["class"] });
    /* 堆叠与否会改变卡片高度（.ai-stacked 由 layout.js 加上/去掉）：跟着重算预留 */
    new MutationObserver(positionAiCard).observe($("aiBody"), {
      attributes: true, attributeFilter: ["class"],
    });
  }

  function aiNotes() {
    const linked = (env.anno.notes || []).map(n => {
      const p = keyParts(n.block_id);
      const item = findItem(p.id);
      const sents = (item && item.sents) || [];
      const sent = (p.si >= 0 && sents[p.si]) ? sents[p.si].text : "";
      return { id: n.id, sent: sent, note: (n.text || "").trim(),
        page: item ? pageOfItem(item) : 0 };
    }).filter(x => x.note);
    const free = (env.anno.free_notes || []).map(n => ({
      id: n.id, title: n.title || "", note: (n.text || "").trim(), page: 0,
    })).filter(x => x.note);
    return linked.concat(free);
  }

  function renderAiSub() {
    const bits = [];
    if (aiBusyWhat === "summary") bits.push("正在整理笔记…");
    if (aiMeta.summary_at) bits.push("整理于 " + String(aiMeta.summary_at).replace("T", " "));
    if (aiMeta.summary_edited_at) bits.push("已手改");
    if (aiMeta.notes_count) bits.push("共 " + aiMeta.notes_count + " 条笔记");
    if (aiMeta.ready === false) bits.push("⚠️ 未配置 LLM，无法整理/对话");
    if (aiMeta.stale) bits.push("笔记有更新，可重新整理");
    $("aiSub").textContent = bits.join(" · ")
      || "可让 AI 把内容写进右侧整理卡片或定位论文内容；读到 95% 会自动整理笔记";
  }

  /* 忙的时候或只读共享时把按钮置灰，避免重复触发 / 点了才报 403 */
  function syncAiButtons() {
    const busy = aiBusy() || !canWrite();
    $("aiRegen").disabled = busy;
    $("aiSend").disabled = busy;
    $("aiClear").disabled = busy;
    $("aiRegen").title = canWrite() ? "按当前笔记重新整理一次" : "只读共享：不能重新整理";
    $("aiClear").title = canWrite() ? "只清掉对话，整理结果保留" : "只读共享：不能清空对话";
    $("aiText").disabled = !canWrite();
    $("aiText").placeholder = canWrite()
      ? "就这篇论文或你的笔记提问（Enter 发送，Shift+Enter 换行）"
      : "只读共享：你可以看对话与整理，但不能发消息";
  }

  /* 目录侧栏里那一块“AI 辅助阅读”：与目录不是一体（靠分隔线隔开），
     只放状态 + 主题入口 + 跳转按钮，完整内容还在文末。 */
  function jumpToAi() {
    if (!$("aiEnd")) return;
    if (aiCollapsed) setAiCollapsed(false);
  }

  function renderTocAi() {
    const list = $("tocAiList"), state = $("tocAiState");
    if (!list) return;
    list.textContent = "";
    if (aiSummary) {
      state.textContent = aiStale ? "有更新" : "已整理";
      const themes = (aiSummary.themes || []).slice(0, 8);
      for (const t of themes) {
        const b = el("button", "toc-ai-item", "· " + (t.title || "要点"));
        b.type = "button";
        b.title = "跳到文末看这块的整理";
        b.addEventListener("click", jumpToAi);
        list.appendChild(b);
      }
      if (!themes.length) list.appendChild(el("div", "toc-ai-empty", "整理结果在文末，点下面按钮过去看。"));
    } else {
      state.textContent = "";
      const n = aiNotes().length;
      list.appendChild(el("div", "toc-ai-empty", n
        ? "还没整理。读到 95% 会自动整理这 " + n + " 条笔记。"
        : "先在正文里写几条笔记，读完后会自动整理。"));
    }
  }

  function markAiStale() {
    if (!aiSummary) return;
    aiStale = true;
    renderAiSub();
    renderTocAi();
  }

  /* ---------- AI 文字的 Markdown 渲染（对话气泡 + 整理条目） ----------
     渲染器在 r2-md.js（自带实现，不引第三方库）；文字里若带 $…$ / \(…\) 数学，
     再借 r2-math.js 的 KaTeX 就地替换 —— 没加载过就先懒加载，加载完重画面板。
     渲染器出意外时退回纯文本：宁可不好看，也别让面板空白。 */
  function aiMd(host, text) {
    const s = text == null ? "" : String(text);
    host.classList.add("md");
    if (!window.R2Md) { host.textContent = s; return; }
    try { window.R2Md.render(host, s); } catch (e) { host.textContent = s; }
    aiMdMath(host, s);
  }
  function aiMdInline(host, text) {
    const s = text == null ? "" : String(text);
    host.classList.add("md");
    if (!window.R2Md) { host.textContent = s; return; }
    try { window.R2Md.inline(host, s); } catch (e) { host.textContent = s; }
    aiMdMath(host, s);
  }
  function aiMdMath(host, text) {
    if (!renderInlineMath || !window.R2Md || !window.R2Md.hasMath(text)) return;
    if (!katexReady || katexReady()) {          // 已就绪（或没给判断函数）：直接就地渲染
      try { renderInlineMath(host); } catch (e) { /* ignore */ }
      return;
    }
    if (!ensureKatex || aiMathTried) return;    // 只懒加载一次；失败也不反复试
    aiMathTried = true;
    ensureKatex().then(ok => {
      if (!ok) return;
      renderAiChat();                           // 重画后 aiMd 里 katexReady() 已为 true
      if (!aiEdit) renderAiSummary();           // 正在就地编辑时别重画（会吞掉输入框里的字）
    });
  }

  function renderAiChat() {
    const box = $("aiMsgs");
    aiDraftEl = null;
    box.textContent = "";
    if (!aiChat.length && !aiBusy()) {
      const d = el("div", "ai-empty-chat");
      d.innerHTML = aiSummary
        ? "提问、要求记录内容，或让 AI 定位到论文/笔记。<br>AI 会带着全文和笔记一起处理。"
        : "先读到最后（或点右侧「生成整理」），<br>再就笔记里的疑问聊一聊。";
      box.appendChild(d);
      return;
    }
    for (const m of aiChat) {
      const me = m.role === "user";
      const d = el("div", "ai-msg " + (me ? "me" : "ai"));
      d.appendChild(el("div", "who", me ? "我" : "AI"));
      if (me) {
        d.appendChild(el("div", "txt", m.content || ""));   // 自己打的字原样显示（保留换行）
      } else {
        const txt = el("div", "txt");
        aiMd(txt, m.content || "");                          // AI 回复按 Markdown 渲染
        d.appendChild(txt);
      }
      box.appendChild(d);
    }
    if (aiDraft !== null) {
      // 流式回复：气泡跟着已收到的字长大（同样按 Markdown 渲染），光标提示“还在写”
      aiDraftEl = el("div", "ai-msg ai " + (aiDraft ? "streaming" : "pending"));
      aiDraftEl.appendChild(el("div", "who", "AI"));
      const dtxt = el("div", "txt");
      if (aiDraft) aiMd(dtxt, aiDraft);
      else dtxt.textContent = "⏳ 正在想…";
      aiDraftEl.appendChild(dtxt);
      box.appendChild(aiDraftEl);
      if (aiToolStatus) {
        const status = el("div", "ai-msg ai pending");
        status.appendChild(el("div", "who", "Agent"));
        status.appendChild(el("div", "txt", "⏳ " + aiToolStatus));
        box.appendChild(status);
      }
    } else if (aiBusy()) {
      // 还没开始回字（整理笔记 / 刚发出去）：工作状态就放聊天流末尾，
      // 不再用占位符把右边的整理内容顶掉
      const d = el("div", "ai-msg ai pending");
      d.appendChild(el("div", "who", "AI"));
      d.appendChild(el("div", "txt", aiBusyWhat === "summary"
        ? "⏳ 正在读你的笔记…" : ("⏳ " + (aiToolStatus || "正在思考…"))));
      box.appendChild(d);
    }
    if (aiPendingNavigation) {
      const card = el("div", "ai-nav-confirm");
      const target = aiPendingNavigation.target === "paper"
        ? "论文第 " + aiPendingNavigation.page + " 页"
        : "指定笔记";
      card.appendChild(el("div", "txt", "要定位到" + target + "吗？"));
      const actions = el("div", "actions");
      const yes = el("button", "btn primary", "是，定位");
      yes.type = "button";
      yes.addEventListener("click", () => sendAi("yes"));
      const no = el("button", "btn ghost", "暂不");
      no.type = "button";
      no.addEventListener("click", () => sendAi("no"));
      actions.append(yes, no);
      card.appendChild(actions);
      box.appendChild(card);
    }
    box.scrollTop = box.scrollHeight;      // 新消息 / 工作状态总在可见处
  }

  /* 收到一段增量：只重画那一个气泡的文字（Markdown 整体重渲，节流到每帧一次），
     不整框重画（否则长回复会卡） */
  function pushAiDelta(text) {
    if (!text) return;
    if (aiDraft === null) { aiDraft = ""; renderAiChat(); }
    aiDraft += text;
    if (!aiDraftEl) { renderAiChat(); return; }
    aiDraftEl.className = "ai-msg ai streaming";
    if (aiDraftRaf) return;                 // 本帧已排过一次渲染
    aiDraftRaf = requestAnimationFrame(() => {
      aiDraftRaf = 0;
      const txt = aiDraftEl && aiDraftEl.querySelector(".txt");
      if (!txt) return;                     // 流已结束 / 气泡已重建：什么都不用做
      aiMd(txt, aiDraft || "");
      const box = $("aiMsgs");
      box.scrollTop = box.scrollHeight;
    });
  }

  /* 逐行读 SSE（`data: {json}` + 空行），把事件交给 onEvent。
     浏览器不支持流式读取（没有 body reader）时退化成“整段收完再一次解析”，
     至少不会报错。 */
  async function readAiStream(resp, onEvent) {
    const handle = async line => {
      line = (line || "").trim();
      if (!line || line.startsWith(":")) return;
      if (line.startsWith("data:")) line = line.slice(5).trim();
      if (!line) return;
      let ev = null;
      try { ev = JSON.parse(line); } catch (e) { return; }
      if (ev && typeof ev === "object") await onEvent(ev);
    };
    const reader = resp.body && resp.body.getReader ? resp.body.getReader() : null;
    if (!reader) {
      for (const line of (await resp.text()).split("\n")) await handle(line);
      return;
    }
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {   // 一行一个事件，拼够了再解析
        await handle(buf.slice(0, i));
        buf = buf.slice(i + 1);
      }
    }
    if (buf) await handle(buf);
  }

  function renderAiSummary() {
    const box = $("aiSum");
    const keepTop = box.scrollTop;      // 重绘前记下滚动位置，别让用户看到一半又被弹回顶部
    box.textContent = "";
    if (!aiSummary) {                   // 还没整理过：右边只放提示（第一次生成时的进度在聊天框里）
      const busy = aiBusyWhat === "summary";
      const hasNotes = aiNotes().length > 0;
      const d = el("div", "ai-empty");
      d.innerHTML = '<div class="big">' + (busy ? "⏳" : '<i class="ai-mark" aria-hidden="true"></i>') + '</div>' + (busy
        ? "正在读你的笔记，整理好就显示在这里…"
        : hasNotes
          ? "读到 95% 会自动把这篇的笔记整理成<br>「主题 / 关联 / 待想清楚的问题」。"
          : "这篇还没有笔记。<br>先在正文里选中句子、点「笔记」写几条，<br>再回来让 AI 帮你理一理。");
      if (hasNotes && !aiBusy() && canWrite()) {
        const b = el("button", "btn primary", "生成整理");
        b.addEventListener("click", () => genAiSummary(true));
        d.appendChild(b);
      }
      box.appendChild(d);
      renderTocAi();
      return;
    }
    // 已经有整理内容：**一直显示**（重新整理 / 聊天期间也不会再被“工作状态”顶掉）
    if (aiStale) {
      const h = el("div", "ai-stale", "笔记改过了。重新整理会把这一版 + 你聊过的内容一起并成新版。");
      const b = el("button", "btn ghost", "重新整理");
      b.style.marginLeft = "8px"; b.style.padding = "2px 8px"; b.style.fontSize = "12px";
      b.addEventListener("click", () => genAiSummary(true));
      h.appendChild(b);
      box.appendChild(h);
    }
    const s = aiSummary;
    const canEdit = !aiBusy() && !aiSaving && canWrite();

    /* 总览 */
    if (aiEditing("overview")) {
      const holder = el("div", "ai-text ai-over");
      aiInlineEdit(holder, s.overview || "", saveOverview,
                   { placeholder: "用几句话概括这些笔记整体在关心什么…" });
      box.appendChild(holder);
    } else if (s.overview) {
      const holder = el("div", "ai-text ai-over");
      const tx = el("div", "ai-txt");
      aiMd(tx, s.overview);
      holder.appendChild(tx);
      if (canEdit) holder.appendChild(aiTools("overview", null, null, null));
      box.appendChild(holder);
    } else if (canEdit) {
      const b = el("button", "ai-add", "＋ 添加总览");
      b.type = "button";
      b.addEventListener("click", () => { s.overview = ""; aiEdit = aiKeyOf("overview"); renderAiSummary(); });
      box.appendChild(b);
    }

    /* 主题梳理 */
    const thead = el("div", "ai-sec-head");
    thead.appendChild(el("div", "ai-sec-title", "🧩 主题梳理"));
    if (canEdit) {
      const b = el("button", "ai-add", "＋ 加主题");
      b.type = "button";
      b.addEventListener("click", aiAddTheme);
      thead.appendChild(b);
    }
    box.appendChild(thead);
    (s.themes || []).forEach((t, ti) => {
      const card = el("div", "ai-theme");
      const h = el("div", "ai-theme-head");
      const titleBox = el("div", "ai-text ai-theme-title");
      if (aiEditing("theme-title", ti)) {
        aiInlineEdit(titleBox, t.title || "", v => { t.title = v; saveAiSummary(); },
                     { single: true, placeholder: "这一组叫什么？" });
      } else {
        const h4 = el("h4", "");
        aiMdInline(h4, t.title || "要点");
        titleBox.appendChild(h4);
        if (canEdit) {
          titleBox.appendChild(aiTools("theme-title", ti, null, () => aiDelTheme(ti), "删主题"));
        }
      }
      h.appendChild(titleBox);
      card.appendChild(h);
      const ul = el("ul", "ai-list");
      (t.points || []).forEach((p, pi) => {
        const li = el("li");
        li.appendChild(aiTextEl(p, "point", ti, pi,
          v => { if (!v) t.points.splice(pi, 1); else t.points[pi] = v; saveAiSummary(); },
          () => { t.points.splice(pi, 1); saveAiSummary(); }));
        ul.appendChild(li);
      });
      if (canEdit) {
        const b = el("button", "ai-add", "＋ 加要点");
        b.type = "button";
        b.addEventListener("click", () => {
          t.points = t.points || [];
          t.points.push("");
          aiEdit = aiKeyOf("point", ti, t.points.length - 1);
          renderAiSummary();
        });
        ul.appendChild(el("li", "ai-list-add")).appendChild(b);
      }
      card.appendChild(ul);
      box.appendChild(card);
    });

    /* 三个条目区 */
    for (const [key, title] of [["connections", "🔗 笔记之间的关联"],
                                ["questions", "❓ 还没想清楚"],
                                ["next", "➡️ 下一步"]]) {
      const rows = s[key] || [];
      const head = el("div", "ai-sec-head");
      head.appendChild(el("div", "ai-sec-title", title));
      if (canEdit) {
        const b = el("button", "ai-add", "＋ 加一条");
        b.type = "button";
        b.addEventListener("click", () => {
          s[key] = rows;
          rows.push("");
          aiEdit = aiKeyOf(key, rows.length - 1);
          renderAiSummary();
        });
        head.appendChild(b);
      }
      box.appendChild(head);
      if (!rows.length) {
        box.appendChild(el("div", "ai-none", "（暂无）"));
        continue;
      }
      const ul = el("ul", "ai-list");
      rows.forEach((r, i) => {
        const li = el("li");
        li.appendChild(aiTextEl(r, key, i, null,
          v => { if (!v) rows.splice(i, 1); else rows[i] = v; saveAiSummary(); },
          () => { rows.splice(i, 1); saveAiSummary(); }));
        ul.appendChild(li);
      });
      box.appendChild(ul);
    }

    if (canEdit) {
      box.appendChild(el("div", "ai-tip",
        "点「编辑」可直接改（Ctrl+Enter 保存，Esc 取消）。改过的内容会存下来；"
        + "「重新整理」时会连这版和你聊过的内容一起并成新版。"));
    }
    box.scrollTop = keepTop;              // 保持原来看的位置
    renderTocAi();                        // 目录侧栏那块跟着更新
  }

  /* ---------- 整理结果的「就地编辑」 ----------
     跟笔记卡一套交互：点「编辑」→ 变输入框 → Ctrl+Enter 保存 / Esc 取消；
     改完立刻整份存服务端（`/ai/summary/save`），所以刷新/换设备都还在。 */
  function aiKeyOf(kind, a, b) {
    return kind + "|" + (a == null ? "" : a) + "|" + (b == null ? "" : b);
  }
  function aiEditing(kind, a, b) { return aiEdit !== "" && aiEdit === aiKeyOf(kind, a, b); }

  /* 把 host 换成输入框；保存走 onSave(新值)，取消走 aiCancelEdit()（会顺手清掉空条目） */
  function aiInlineEdit(host, value, onSave, opts) {
    const o = opts || {};
    const single = !!o.single;
    const ta = el(single ? "input" : "textarea", "ai-edit");
    if (single) ta.type = "text";
    ta.value = value || "";
    if (o.placeholder) ta.placeholder = o.placeholder;
    const row = el("div", "ai-edit-row");
    const ok = el("button", "btn primary", "保存");
    const no = el("button", "btn ghost", "取消");
    ok.type = "button"; no.type = "button";
    row.append(ok, no);
    host.textContent = "";
    host.append(ta, row);
    ta.focus();
    try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch (e) { /* ignore */ }
    let done = false;
    const commit = () => { if (!done) { done = true; onSave((ta.value || "").trim()); } };
    const cancel = () => { if (!done) { done = true; aiCancelEdit(); } };
    ok.addEventListener("click", commit);
    no.addEventListener("click", cancel);
    ta.addEventListener("keydown", e => {
      if (e.key === "Escape") { e.preventDefault(); cancel(); return; }
      if (e.key !== "Enter") return;
      if (!single && !e.ctrlKey && !e.metaKey) return;      // 多行框里普通回车 = 换行
      e.preventDefault();
      commit();
    });
  }

  /* 条目右侧的小工具（编辑 / 删除）；正在编辑时不画 */
  function aiTools(kind, a, b, onDel, delLabel) {
    const acts = el("span", "ai-tools");
    const be = el("button", "", "编辑");
    be.type = "button";
    be.addEventListener("click", () => { aiEdit = aiKeyOf(kind, a, b); renderAiSummary(); });
    acts.appendChild(be);
    if (onDel) {
      const bd = el("button", "del", delLabel || "删除");
      bd.type = "button";
      bd.addEventListener("click", onDel);
      acts.appendChild(bd);
    }
    return acts;
  }

  /* 一条可编辑文字：点「编辑」→ 输入框 → 存下来 */
  function aiTextEl(text, kind, a, b, onSave, onDel) {
    const holder = el("div", "ai-text");
    if (aiEditing(kind, a, b)) {
      aiInlineEdit(holder, text, onSave, {});
      return holder;
    }
    const tx = el("div", "ai-txt");
    if (String(text || "").trim()) aiMd(tx, text);
    else tx.textContent = "（空）";
    holder.appendChild(tx);
    if (canWrite() && !aiBusy() && !aiSaving) holder.appendChild(aiTools(kind, a, b, onDel));
    return holder;
  }

  const saveOverview = v => {
    if (!v) { toast("总览不能为空"); aiCancelEdit(); return; }
    aiSummary.overview = v;
    saveAiSummary();
  };

  function aiAddTheme() {
    const s = aiSummary;
    if (!s) return;
    s.themes = s.themes || [];
    s.themes.push({ title: "", points: [] });
    aiEdit = aiKeyOf("theme-title", s.themes.length - 1);
    renderAiSummary();
  }

  function aiDelTheme(ti) {
    const t = (aiSummary.themes || [])[ti];
    if (!t) return;
    const n = (t.points || []).length;
    if (!confirm("删除主题「" + (t.title || "要点") + "」" + (n ? "及其 " + n + " 条要点" : "") + "？")) return;
    aiSummary.themes.splice(ti, 1);
    saveAiSummary();
  }

  /* 取消编辑：把“加进来还没写字”的空条目清掉，别留下空壳 */
  function aiCancelEdit() {
    aiEdit = "";
    aiDropBlanks();
    renderAiSummary();
  }
  function aiDropBlanks() {
    const s = aiSummary;
    if (!s) return;
    const clean = arr => (arr || []).filter(v => String(v || "").trim());
    s.connections = clean(s.connections);
    s.questions = clean(s.questions);
    s.next = clean(s.next);
    s.themes = (s.themes || []).map(t => ({
      title: (t.title || "").trim(), points: clean(t.points),
    })).filter(t => t.title || t.points.length);
  }

  async function saveAiSummary() {
    if (aiSaving || !aiSummary) return;
    if (!needWrite("只读共享：不能修改整理内容")) return;
    aiSaving = true;
    aiEdit = "";
    aiDropBlanks();
    renderAiSummary();                    // 先按新内容重画（输入框收起），不必等网络
    try {
      const r = await api("/api/doc/" + docId + "/ai/summary/save",
                          { method: "POST", json: { summary: aiSummary } });
      applyAiState(r);
      toast("整理已更新 ✓");
    } catch (e) {
      showNotice("保存整理失败：" + e.message, true);
      await loadAi();                     // 拉回服务端那版，别让界面跟库里不一致
      return;
    } finally {
      aiSaving = false;
    }
    renderAiSummary();
  }

  function applyAiState(r) {
    aiMeta = r || {};
    aiSummary = aiMeta.summary || null;
    aiChat = aiMeta.chat || [];
    aiStale = !!aiMeta.stale;
    renderAiSub();
  }

  async function loadAi() {
    try {
      applyAiState(await api("/api/doc/" + docId + "/ai"));
    } catch (e) { /* 拿不到就保持空状态，不影响阅读 */ }
    renderAiSummary();
    renderAiChat();
  }

  async function genAiSummary(force) {
    if (aiBusy()) return;
    if (!needWrite("只读共享：不能生成/重新整理笔记")) return;
    const notes = aiNotes();
    if (!notes.length) { toast("这篇还没有笔记：先添加一条笔记再整理"); return; }
    aiBusyWhat = "summary";
    renderAiSub();
    syncAiButtons();
    renderAiSummary();      // 还没有内容时把右边换成“正在读你的笔记…”提示
    renderAiChat();         // 工作状态显示在聊天框里
    try {
      const r = await api("/api/doc/" + docId + "/ai/summary",
                          { method: "POST", json: { notes: notes, force: !!force } });
      applyAiState(r);
      toast(force ? "已按当前笔记重新整理 ✅" : "已按你的笔记生成整理 ✅");
    } catch (e) {
      showNotice("整理笔记失败：" + e.message, true);
    } finally {
      aiBusyWhat = "";
      renderAiSub();
      syncAiButtons();
      renderAiSummary();
      renderAiChat();
    }
  }

  async function sendAi(navigationAnswer = null) {
    const ta = $("aiText");
    const pendingNavigation = aiPendingNavigation;
    const text = navigationAnswer === "yes" ? "是的，请定位到刚才提到的位置。"
      : navigationAnswer === "no" ? "暂时不用定位，谢谢。"
        : (ta.value || "").trim();
    if (!needWrite("只读共享：不能发消息")) return;
    if (!text || aiBusy()) return;
    aiPendingNavigation = null;
    aiBusyWhat = "chat";
    aiToolStatus = "";
    ta.value = "";
    aiChat.push({ role: "user", content: text });
    aiDraft = null;               // 还没收到第一个字：先显示“⏳ 正在想…”
    renderAiChat();               // 只重画聊天框：右边的整理内容保持原样
    syncAiButtons();
    try {
      const request = { message: text, notes: aiNotes() };
      if (pendingNavigation) {
        request.pending_navigation = pendingNavigation;
        if (navigationAnswer) request.navigation_answer = navigationAnswer;
      }
      const resp = await fetch("/api/doc/" + docId + "/ai/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
      });
      if (!resp.ok) {
        let msg = "请求失败(" + resp.status + ")";
        try { const j = await resp.json(); if (j && j.detail) msg = j.detail; } catch (e) { /* ignore */ }
        if (resp.status === 401) gotoLogin();
        throw new Error(msg);
      }
      let failure = "";
      let completed = false;
      let proposedNavigation = null;
      await readAiStream(resp, async ev => {
        if (ev.type === "delta") {
          if (aiToolStatus) {
            aiToolStatus = "";
            renderAiChat();
          }
          pushAiDelta(ev.text || "");          // 边收边显示
        } else if (ev.type === "done") {
          completed = true;
          if (ev.chat) { aiChat = ev.chat; aiDraft = null; }
          aiPendingNavigation = proposedNavigation;
        } else if (ev.type === "error") {
          failure = ev.message || "对话失败";
        } else if (ev.type === "tool") {
          const label = ev.name === "add_note_to_summary" ? "正在写入整理卡片…"
            : "正在定位内容…";
          aiToolStatus = ev.status === "running" ? label : (ev.message || "工具已执行");
          renderAiChat();
        } else if (ev.type === "action") {
          const action = ev.action || {};
          if (action.type === "summary_changed") {
            if (action.summary) {          // 后端已经把新整理落库：直接换到卡片上
              aiSummary = action.summary;
              aiEdit = "";
              renderAiSummary();
              renderAiSub();
            }
          } else if (action.type === "notes_changed") {
            await env.refreshNotes();
            markAiStale();
          } else if (action.type === "navigate") {
            if (action.target === "paper" && Number.isInteger(action.page)) {
              env.scrollToPage(action.page - 1);
              if (action.block_id) env.flashRelatedBlock(action.block_id);
            } else if (action.target === "note") {
              if (!env.jumpToNoteId(action.note_id)) showNotice("没有找到要定位的笔记", true);
            } else if (action.target === "notes_panel") {
              env.openSidePanel();
            }
          }
        } else if (ev.type === "confirm_navigation") {
          proposedNavigation = ev.action || null;
        }
      });
      if (failure) throw new Error(failure);
      if (!completed) throw new Error("对话未正常结束，请重试");
    } catch (e) {
      if (pendingNavigation) aiPendingNavigation = pendingNavigation;
      if (aiDraft) {
        // 已经吐出来一部分：留下来当回复，别让用户看着字消失
        aiChat.push({ role: "assistant", content: aiDraft });
      } else {
        aiChat.pop();                     // 一个字都没收到：把提问收回去，文字还给输入框
        ta.value = text;
      }
      aiDraft = null;
      showNotice("AI 对话失败：" + e.message, true);
    } finally {
      aiBusyWhat = "";
      aiDraft = null;
      renderAiChat();
      // 整理卡片也要重画：Agent 写卡片时是在忙碌中画的（canEdit=false），
      // 那时「编辑 / 加主题 / 加要点 / 加一条」按设计不画；这一轮结束后要补回来
      renderAiSummary();
      syncAiButtons();
    }
  }

  /* 读到 ≥95%：自动整理一次（已经整理过 / 还没写笔记 / 只读共享就不打扰） */
  function maybeAutoSummarize() {
    if (aiAutoTried || aiBusy() || !env.META || aiSummary) return;
    if (!canWrite()) return;                         // 只读共享：整理是共享内容，不能自动改
    if ((env.META.progress || 0) < AI_TRIGGER) return;
    if (!aiNotes().length) return;                          // 没有笔记：等写了笔记再触发
    aiAutoTried = true;
    toast("已读到结尾，正在用 AI 整理你的笔记…", 3600);
    genAiSummary(false);
  }

  /* 这张卡片是**盖在论文上的独立浮层**：鼠标在卡片上时，滚轮只滚卡片里的内容
     （对话 / 整理），滚到顶或到底也**不**把滚动传给论文页面（否则指针一落在卡片上，
     论文就被“顺手”滚跑了）；只有指针离开卡片，滚轮才滚窗口 / 论文。 */
  const aiScrollHost = (node) => {
    let n = node;
    while (n && n !== document.body) {
      if (n.nodeType === 1) {
        const oy = getComputedStyle(n).overflowY;
        if ((oy === "auto" || oy === "scroll") && n.scrollHeight - n.clientHeight > 1) return n;
      }
      if (n.id === "aiEnd") return null;
      n = n.parentElement;
    }
    return null;
  };
  $("aiEnd").addEventListener("wheel", (e) => {
    if (e.ctrlKey || e.metaKey) return;                 // Ctrl+滚轮 = 浏览器缩放，别拦
    if (!e.deltaY) return;                              // 纯横向手势交给浏览器
    const host = aiScrollHost(e.target);
    const dy = e.deltaMode === 1 ? e.deltaY * 16         // 行模式（Firefox）
             : e.deltaMode === 2 ? e.deltaY * e.target.offsetHeight : e.deltaY;
    const room = host
      ? (dy > 0 ? host.scrollHeight - host.clientHeight - host.scrollTop   // 还能往下滚多少
                : host.scrollTop)                                          // 还能往上滚多少
      : 0;
    if (room > 1) return;                               // 卡片里还有得滚 → 交给浏览器原生滚动
    e.preventDefault();                                 // 到顶 / 到底 / 没得滚 → 只吞掉，不带动页面
  }, { passive: false });

  $("aiToggle").addEventListener("click", () => setAiCollapsed(!aiCollapsed));
  $("aiSend").addEventListener("click", sendAi);
  $("aiText").addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendAi(); }
  });
  $("aiRegen").addEventListener("click", () => genAiSummary(true));
  $("tocAiGo").addEventListener("click", () => {
    jumpToAi();
    // 还没整理过但已经有笔记：顺手生成一份（不用先去读满 95%）
    if (!aiSummary && !aiBusy() && canWrite() && aiNotes().length) genAiSummary(false);
  });
  $("aiClear").addEventListener("click", async () => {
    if (!aiChat.length) { toast("对话已经是空的"); return; }
    if (!confirm("清空这篇的对话记录？（笔记整理会保留）")) return;
    try {
      await api("/api/doc/" + docId + "/ai/chat/reset", { method: "POST" });
      aiChat = [];
      renderAiChat();
      toast("对话已清空");
    } catch (e) { showNotice("清空对话失败：" + e.message, true); }
  });

  /* ============ 对外接口 ============ */
  return { maybeAutoSummarize, syncAiButtons, jumpToAi, renderTocAi, loadAi, markAiStale };
};
