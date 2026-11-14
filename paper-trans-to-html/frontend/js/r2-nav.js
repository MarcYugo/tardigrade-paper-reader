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

  /* ================= 缩放：顶栏的滑轮条（数值在上、滑杆在下） =================
     滑杆给的是**请求的档位**（80% ~ 150%，每格 5%，min/max/step 写在 reader.html 里），
     数值显示的是**实际渲染比例** —— 右侧要留批注栏时页面会等比适宽，
     两者可能不一致（悬停数值有提示）。
     ⚠️ 档位下标换算用滑杆自己身上的 min/step，别再写死 80 / 5：
        它们与 reader2.js 的 ZOOM_PCT_* 是同一套数（那边按同样规则生成 ZOOMS）。
     ⚠️ **拖动途中不要真的改 zoom**：滑杆本身也在 <html zoom> 里，改 zoom 会让它跟着变宽、
       指针捕获被打断 —— 实测按下后拖到最右只前进一档（100 → 105 就卡住），而且改回 100%
       还得靠键盘。所以 input 只预览数值，松手（change）/键盘/失焦时才真正应用。 */
  const zoomRange = $("zoomRange");
  const zMin = zoomRange ? Number(zoomRange.min) || 80 : 80;
  const zStep = zoomRange ? Number(zoomRange.step) || 5 : 5;
  const idxOfSlider = () => clamp(Math.round((Number(zoomRange.value) - zMin) / zStep), 0, ZOOMS.length - 1);
  /* 滑杆位置 + 「已滑过」的填充（WebKit 用渐变画，Firefox 用 ::-moz-range-progress） */
  function paintZoom(pctWanted) {
    if (!zoomRange) return;
    const want = String(Math.round(pctWanted));
    if (zoomRange.value !== want) zoomRange.value = want;
    const hi = Number(zoomRange.max) || 150;
    const fill = hi > zMin ? ((Number(want) - zMin) / (hi - zMin)) * 100 : 0;
    zoomRange.style.setProperty("--zoom-fill", clamp(fill, 0, 100).toFixed(1) + "%");
  }
  if (zoomRange) {
    zoomRange.addEventListener("input", () => {           // 拖动中：只改数值，不动版式
      const pct = Math.round(ZOOMS[idxOfSlider()] * 100);
      $("zoomVal").textContent = pct + "%";
      $("zoomVal").title = "松开鼠标后应用";
      paintZoom(pct);
    });
    zoomRange.addEventListener("change", () => setZoomIdx(idxOfSlider()));   // 松手 / 点轨道
    zoomRange.addEventListener("keyup", () => setZoomIdx(idxOfSlider()));    // 键盘 ←/→/Home/End 兜底
    zoomRange.addEventListener("blur", () => setZoomIdx(idxOfSlider()));
  }

  /* ---- 滚轮缩放：鼠标停在缩放控件上时，**上滚放大、下滚缩小**（每格 5%）----
     和拖动一个节奏：**滚的时候只改数值，停手（180ms 没新滚动）才真正缩放**。
     这样连续滚十几格也只会在最后统一应用一次，不会一格一卡；顺带绕开了「控件自己在
     <html zoom> 里、缩放时相对窗口滑走」的老问题（滚动期间版式根本不动）。
     几个要点：
     · 事件挂在 window 上 + 1.2s 手势锁：滚动途中指针即使滑出控件也继续累计
       （指针离开起点 >90px 或停手 >1.2s 就解锁，新手势仍要求指针真的在控件上）；
     · 归一化 deltaMode（像素 / 行 / 页）并累计到 60 再走一格：触控板一次只给几像素，
       不累计根本滚不动；走一格就把余数清零，否则鼠标滚轮一格（~100px）会连跳两格；
     · 100ms 节流：惯性滚动 / 一次滚轮派发多个事件时不会一帧跳好几档；
     · Ctrl+滚轮是浏览器自己的页面缩放，放行；横向滚（Shift+滚轮 / 触控板横滑）也不管。 */
  const zoomBox = document.querySelector(".zoom-group");
  const WHEEL_UNIT = 60;         // 累计到这个量走一格
  const WHEEL_GAP = 100;         // 两格之间至少隔这么久（ms）：连续猛滚 / 惯性滚动也不会一帧跳几档
  const WHEEL_LOCK = 1200;       // 一次滚轮手势的锁定时间（ms）
  const WHEEL_SETTLE = 180;      // 停手多久后应用（≈ 拖动的 pointerup）
  const WHEEL_DRIFT = 90;        // 指针离开手势起点超过这么多 px 就解锁（用户可能想滚页面）
  let wheelAcc = 0, wheelUntil = 0, wheelAnchor = null, wheelStepAt = 0;
  let wheelPending = null, wheelTimer = 0;   // 滚动中的「待应用档位」+ 停手定时器
  function wheelPreview() {
    const pct = Math.round(ZOOMS[wheelPending] * 100);
    $("zoomVal").textContent = pct + "%";
    $("zoomVal").title = "滚轮停下后应用";
    paintZoom(pct);
  }
  function wheelCommit() {
    if (wheelTimer) { clearTimeout(wheelTimer); wheelTimer = 0; }
    if (wheelPending == null) return;
    const ni = wheelPending;
    wheelPending = null;
    setZoomIdx(ni);            // 里面会 refreshZoomLabel，把数值 / 滑杆对齐到真实比例
  }
  window.addEventListener("wheel", (e) => {
    if (!zoomBox || e.ctrlKey || !e.deltaY) return;
    const now = performance.now();
    let live = now < wheelUntil;
    if (live && wheelAnchor
        && Math.hypot(e.clientX - wheelAnchor.x, e.clientY - wheelAnchor.y) > WHEEL_DRIFT) live = false;
    if (!live) {
      if (!zoomBox.contains(e.target)) { wheelAcc = 0; wheelUntil = 0; wheelAnchor = null; return; }
      wheelAcc = 0;                                   // 新的一次手势：重新累计
      wheelStepAt = 0;
      wheelAnchor = { x: e.clientX, y: e.clientY };
    }
    e.preventDefault();                               // 别让整页跟着上下滚
    wheelUntil = now + WHEEL_LOCK;
    wheelAcc += e.deltaMode === 1 ? e.deltaY * 40      // 行（Firefox 一格 ≈ 3 行）
             : e.deltaMode === 2 ? e.deltaY * 400      // 页
             : e.deltaY;                               // 像素
    if (Math.abs(wheelAcc) < WHEEL_UNIT) return;       // 触控板还没攒够，先不动
    if (now - wheelStepAt < WHEEL_GAP) return;         // 节流：等下一格的时间窗，累计值留着
    wheelStepAt = now;
    const dir = wheelAcc < 0 ? 1 : -1;                 // 向上（deltaY < 0）= 放大
    wheelAcc = 0;                                      // 一次事件最多走一格
    if (wheelPending == null) wheelPending = env.zoomIdx;
    wheelPending = clamp(wheelPending + dir, 0, ZOOMS.length - 1);
    wheelPreview();                                    // 只动数值，版式一点没动
    if (wheelTimer) clearTimeout(wheelTimer);
    wheelTimer = setTimeout(wheelCommit, WHEEL_SETTLE);
  }, { passive: false });
  if (zoomBox) {
    /* 指针移开就立刻应用，免得「滚完马上走」还要等停手计时 */
    zoomBox.addEventListener("mouseleave", () => { wheelAcc = 0; if (wheelTimer) wheelCommit(); });
  }
  /* 兼容旧的 ± 按钮（旧页面/自检页里可能还有），存在才绑 */
  if ($("zoomOut")) $("zoomOut").addEventListener("click", () => changeZoom(-1));
  if ($("zoomIn")) $("zoomIn").addEventListener("click", () => changeZoom(1));
  function changeZoom(d) { setZoomIdx(env.zoomIdx + d); }
  function setZoomIdx(ni) {
    ni = clamp(Math.round(ni), 0, ZOOMS.length - 1);
    if (ni === env.zoomIdx) { refreshZoomLabel(); return; }
    /* 缩放 = 纯视觉比例（写到 <html> 的 zoom 上）：页面宽度只由「文档基准宽」决定，
       版式与各元素的布局坐标**一点都没变** → 不需要 render() 重建 DOM
       （以前每点一次缩放都整篇重建，38 页要卡一下，滚动位置还得靠 anchor 再摆回来）。
       固定浮层（页码胶囊 / AI 卡片）跟着 #pages 的尺寸变化由各自的 ResizeObserver 更新。 */
    env.zoomIdx = ni;
    applyZoom();
    refreshZoomLabel();
    /* 视觉比例变了：批注栏预留宽（窗口的 25%）与「能不能并排」要按新视口重算。
       relayoutAll 只在页面宽度或并排模式真的变了时才退回整体 render。 */
    relayoutAll();
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
    /* 滑杆位置 = **请求**的档位（不是实际渲染比例），换论文 / 首屏 / 被夹紧后都对齐一次 */
    paintZoom(Math.round(ZOOM() * 100));
  }

  /* ============ 对外接口 ============ */
  return {
    currentPageIndex, updatePageInd, scrollToPage, TOC_W, tocPanelOpen,
    scheduleSave, updateFinishBtn, refreshZoomLabel,
  };
};
