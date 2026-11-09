/* ============ 滚动/页码导航、目录(TOC)、阅读进度、缩放：从 reader2.js 拆出来的独立模块 ============
   与 r2-math.js / r2-ai.js 同一套路：整段**按原样搬过来**（缩进没动，方便对照），
   改动只有三处：
     ① `META` → `env.META`（let，换论文会整体替换；段内只读它的属性、不做绑定赋值，
        所以给 getter 就够，属性赋值仍作用在同一个对象上）；
     ② `zoomIdx` → `env.zoomIdx`（let，且段内**会赋值** → env 里同时给 getter 和 setter）；
     ③ `saveTimer`（原来声明在 reader2.js 顶层）**整个搬进本模块** —— 它只被
        scheduleSave() 用，外面没有任何引用，所以不需要再跨模块暴露。

   装配（reader2.js 原位置）：
     const { currentPageIndex, updatePageInd, … } = R2Nav({ … });
     `canWrite` / `maybeAutoSummarize` 声明在**本段之后**（共享权限段 / AI 段装配处），
     装配这一刻还在 TDZ → 包一层转发，调用时才去读。

   对外接口在文件末尾的 return，共 8 项。
   ============================================================================ */
window.R2Nav = function (env) {
  "use strict";
  const { $, pages, docId, CSS_W, vpH, ZOOMS, pagesEl, ZOOM, GZ, toScrollDelta,
          relayoutAll, applyZoom, pageAnchor, render, canWrite, maybeAutoSummarize } = env;

  let saveTimer = null;        // 进度保存防抖（原来是 reader2.js 顶层的 let，随本段一起搬来）

  /* ================= 滚动 / 页码 / 进度 ================= */
  function currentPageIndex() {
    const mid = vpH() * 0.45;
    let best = 0;
    for (let i = 0; i < pages.length; i++) {
      const r = pages[i].el.getBoundingClientRect();
      if (r.top <= mid) best = i;
    }
    return best;
  }
  function updatePageInd() {
    const si = $("pageIn");
    // 正在输入页号时不要被滚动事件覆盖掉
    if (si && document.activeElement !== si) si.value = currentPageIndex() + 1;
    const tot = $("pageTotal");
    if (tot) tot.textContent = pages.length + " 页";
    syncTocActive();          // 目录里跟着高亮当前所在小节
  }
  function scrollToPage(i, mode) {
    const go = () => {
      const pg = pages[i];
      if (!pg) return;
      const r = pg.el.getBoundingClientRect();
      window.scrollBy({ top: toScrollDelta(r.top - 70), behavior: mode === "instant" ? "auto" : "smooth" });
    };
    // 逐页加载：目标页可能还没建（只有占位页壳）。先滚过去让用户看到进度，
    // 同时请求这一页的数据，建好后原地填进页壳（位置不变，不用再滚一次）。
    if (env.ensurePage && pages[i] && !pages[i].loaded) env.ensurePage(i);
    go();
  }
  /* ---- 页码跳转：底部“1 / 12 页”里直接输入页号回车 ---- */
  function jumpToPage(raw) {
    const total = pages.length;
    if (!total) return;
    const n = parseInt(String(raw).replace(/\D/g, ""), 10);
    if (!n || isNaN(n)) { updatePageInd(); return; }
    if (n < 1 || n > total) {
      toast("页码超出范围：本文共 " + total + " 页");
      updatePageInd();
      return;
    }
    scrollToPage(n - 1, "instant");   // 跳页用即时滚动，数字不会一路乱跳
    updatePageInd();
    toast("已跳到第 " + n + " 页");
  }
  function bindPageJump() {
    const si = $("pageIn");
    if (!si) return;
    si.addEventListener("focus", () => si.select());     // 聚焦即全选，直接输新页号
    si.addEventListener("click", () => si.select());      // 单击也要全选：鼠标抬起会把 focus 里的全选取消掉
    si.addEventListener("input", () => { si.value = si.value.replace(/\D/g, ""); });
    si.addEventListener("keydown", e => {
      if (e.key === "Enter") {
        e.preventDefault();
        const v = si.value;
        si.blur();                                       // 先失焦再跳，blur 里的复位不会盖掉结果
        jumpToPage(v);
      } else if (e.key === "Escape") {
        si.value = currentPageIndex() + 1;
        si.blur();
      }
    });
    si.addEventListener("blur", () => updatePageInd());
  }
  bindPageJump();

  /* ================= 左侧目录（PDF 书签 / LLM 从全文提取） =================
     建论文时就把 PDF 自带书签存进记录里（`toc_source="pdf"`），有就用现成的；
     没有书签时，第一次展开面板会让 LLM 读全文排一份（结果也存库，不重复花 token）。 */
  const TOC_W = 300;                  // 与 style.css 里 body.toc-open 的 padding-left 一致
  let tocItems = [], tocSource = "", tocLoaded = false, tocBusy = false;

  function tocPanelOpen() {
    const p = $("tocPanel");
    return !!(p && p.classList.contains("open"));
  }

  function syncTocActive() {
    const list = $("tocList");
    if (!list || !tocItems.length) return;
    const cur = currentPageIndex() + 1;
    let idx = -1;                                  // 当前所在小节 = 最后一个 page <= 当前页
    for (let i = 0; i < tocItems.length; i++) {
      if (tocItems[i].page && tocItems[i].page <= cur) idx = i;
    }
    list.querySelectorAll(".toc-item").forEach((n, i) => n.classList.toggle("cur", i === idx));
  }

  function renderToc() {
    const list = $("tocList"), foot = $("tocFoot");
    if (!list) return;
    list.textContent = "";
    $("tocCount").textContent = tocItems.length ? tocItems.length + " 条" : "";

    if (tocBusy) {
      const d = el("div", "toc-empty");
      d.innerHTML = '<div class="big">⏳</div>正在用 LLM 读全文提取目录…<br><span style="font-size:12px">首次提取通常要十几秒</span>';
      list.appendChild(d);
      if (foot) foot.textContent = "";
      return;
    }
    if (!tocItems.length) {
      const d = el("div", "toc-empty");
      d.innerHTML = '<div class="big">📑</div>这份 PDF 没有自带书签。<br>可以让 LLM 读全文排一份目录。';
      list.appendChild(d);
    } else {
      for (const it of tocItems) {
        const b = el("button", "toc-item lv" + clamp(it.level || 1, 1, 4));
        b.type = "button";
        b.appendChild(el("span", "tt", it.title || ""));
        if (it.page) b.appendChild(el("span", "tp", "P" + it.page));
        b.title = (it.page ? "第 " + it.page + " 页 · " : "") + (it.title || "");
        b.addEventListener("click", () => { if (it.page) jumpToPage(it.page); });
        list.appendChild(b);
      }
    }

    if (foot) {
      foot.textContent = "";
      foot.appendChild(el("span", "", tocSource === "pdf" ? "来自 PDF 书签"
                                        : tocSource === "llm" ? "LLM 提取" : "暂无来源"));
      const btn = el("button", "", tocItems.length ? "重新提取" : "用 LLM 提取");
      btn.addEventListener("click", () => buildToc(true));
      foot.appendChild(btn);
    }
    syncTocActive();
  }

  async function loadToc() {
    try {
      const r = await api("/api/doc/" + docId + "/toc");
      tocItems = (r && r.items) || [];
      tocSource = (r && r.source) || "";
    } catch (e) { tocItems = []; tocSource = ""; }
    tocLoaded = true;
    renderToc();
    return tocItems.length;
  }

  async function buildToc(force) {
    if (tocBusy) return;
    if (!canWrite()) {                       // 目录存在记录里、大家共用 → 需要写权限
      showNotice("提取目录需要写权限：这是只读共享的论文，只能看已有的目录。", true);
      return;
    }
    tocBusy = true;
    renderToc();
    try {
      const r = await api("/api/doc/" + docId + "/toc",
                          { method: "POST", json: { force: !!force } });
      tocItems = (r && r.items) || [];
      tocSource = (r && r.source) || "llm";
      toast("目录已生成：" + tocItems.length + " 条");
    } catch (err) {
      showNotice("提取目录失败：" + err.message, true);
    } finally {
      tocBusy = false;
      renderToc();
    }
  }

  function openToc() {
    $("tocPanel").classList.add("open");
    document.body.classList.add("toc-open");
    relayoutAll();                                  // 给页面让出左侧空间
    renderToc();
    if (!tocLoaded) {
      loadToc().then(n => { if (!n) buildToc(false); });
    } else if (!tocItems.length && !tocBusy) {
      buildToc(false);
    }
  }
  function closeToc() {
    $("tocPanel").classList.remove("open");
    document.body.classList.remove("toc-open");
    relayoutAll();
  }
  $("btnToc").addEventListener("click", () => { tocPanelOpen() ? closeToc() : openToc(); });
  $("tocClose").addEventListener("click", closeToc);
  function scheduleSave() { clearTimeout(saveTimer); saveTimer = setTimeout(saveProgress, 800); }
  /* 阅读完成度：只按**正文页面**算，不受视口固定的 AI 悬浮卡片影响。 */
  function readRatio() {
    const r = pagesEl.getBoundingClientRect();
    const h = r.height || 1;
    return clamp((vpH() * 0.5 - r.top) / h, 0, 1);
  }
  /* opts.keepalive：页面正在卸载时补存的那一次（fetch 要 keepalive，否则被掐掉） */
  async function saveProgress(opts) {
    if (!env.META || !pages.length) return;      // 还没渲染出正文，别把页码/进度写成 0
    const ratio = readRatio();
    const pi = currentPageIndex();
    let status = env.META.status === "unread" ? "reading" : env.META.status;
    if (ratio > 0.985) status = "done";
    env.META.progress = Math.max(env.META.progress || 0, ratio);
    env.META.last_page = Math.max(env.META.last_page || 0, pi + 1);
    // 精确位置：与只增不减的 progress / last_page 相反，这个是**覆盖写**
    // （用户往回翻就是往回翻，不能又被拽回读到过的最远处）。
    // {pi, ratio} = 第几页 + 页内比例；{pi:-1, fromBottom} = 已滚到正文末尾。
    // 量不到（还没页面）时保留原值，不要用 null 盖掉。
    const pos = pageAnchor();
    if (pos) env.META.last_pos = pos;
    // 页面正在卸载时别再起“自动整理笔记”那种流式请求（必定被掐断，下次打开会重试）
    if (!(opts && opts.keepalive)) maybeAutoSummarize();
    try {
      const r = await api(`/api/doc/${docId}/progress`, {
        method: "POST",
        json: { last_page: env.META.last_page, progress: env.META.progress, status,
                last_pos: env.META.last_pos || null },
        keepalive: !!(opts && opts.keepalive),
      });
      if (r) { env.META.status = r.status; updateFinishBtn(); }
    } catch (e) { /* ignore */ }
  }
  /* 离开阅读页时补存一次：滚动保存用的是 800ms 防抖，
     “往下滑一屏就关掉”那种情况最后一次位置会丢（重进就回到上一次存下的地方）。 */
  window.addEventListener("pagehide", () => { saveProgress({ keepalive: true }); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) saveProgress({ keepalive: true });
  });
  function updateFinishBtn() {
    // META 还没到（首屏、或缺 ?id=）时按“未标记”显示，不抛错
    $("btnFinished").textContent = (env.META && env.META.status === "done") ? "已完成 ✓" : "标记读完";
  }
  $("btnFinished").addEventListener("click", async () => {
    if (!env.META) return;         // 没有可标记的文档（还没加载好）
    const status = env.META.status === "done" ? "reading" : "done";
    await api(`/api/doc/${docId}/progress`, {
      method: "POST", json: { status, progress: status === "done" ? 1 : env.META.progress, last_page: env.META.last_page || pages.length },
    });
    env.META.status = status;
    updateFinishBtn();
    toast(status === "done" ? "🎉 标记为已读完" : "已改为在读");
  });

  /* ================= 缩放 ================= */
  $("zoomOut").addEventListener("click", () => changeZoom(-1));
  $("zoomIn").addEventListener("click", () => changeZoom(1));
  function changeZoom(d) {
    const ni = clamp(env.zoomIdx + d, 0, ZOOMS.length - 1);
    if (ni === env.zoomIdx) return;
    // 先按旧缩放记下“正在看的这处内容”，换完缩放再按同一处还原滚动位置
    // （整体缩放只改视觉比例，元素布局坐标不变，不这样做会跳到别处）
    const anchor = pageAnchor();
    env.zoomIdx = ni;
    applyZoom();
    render(anchor);
  }
  function refreshZoomLabel() {
    // 右侧留出批注栏时页面会等比缩小(fit)，这里显示**实际**渲染比例，避免和看到的不一致。
    // 基准宽度用“本文档的基准宽”(= CSS_W × 文档显示比例)：unlimited-ocr 默认 85%，
    // 但它就是那个文档的 100%，不能因为文档本身小一号就显示 85%。
    const base = env.baseW ? env.baseW() : CSS_W;
    const pg = pages[0];
    const pct = pg ? Math.round(pg.w * GZ() / base * 100) : Math.round(ZOOM() * 100);
    $("zoomVal").textContent = pct + "%";
    const capped = pg && pg.w < base - 1;
    $("zoomVal").title = capped
      ? "窗口宽度要留出侧栏（目录 / 笔记栏），页面已自动适宽；想放大请加宽窗口或收起侧栏"
      : "当前缩放比例";
  }

  /* ============ 对外接口 ============ */
  return {
    currentPageIndex, updatePageInd, scrollToPage, TOC_W, tocPanelOpen,
    scheduleSave, updateFinishBtn, refreshZoomLabel,
  };
};
