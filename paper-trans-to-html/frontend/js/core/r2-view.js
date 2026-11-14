/* ============ 视口坐标 / 缩放 / 滚动位置还原：从 reader2.js 拆出来的模块 ============
   缩放是“整体缩放”：把缩放比写到 <html> 的 zoom 上，顶栏 / 目录 / 笔记栏 / AI 面板
   连同正文一起放大，而不只是把页面撑宽。zoom 生效后测量 API 分成两套坐标，别混用：
     · getBoundingClientRect() / offsetWidth|Height             → 布局坐标(未乘 zoom)
     · clientWidth|Height / innerWidth|Height / scrollY|Height  → 视觉坐标(已乘 zoom)
   凡是两套坐标相遇的地方(滚动、点击取句、视口宽度)，都用这里几个换算函数过一手。
   ⚠️ 上面这套口径**只有 Chrome 成立**：Firefox / Safari 的 getBoundingClientRect() 会把 zoom
   乘进去。要「先量 rect、再把数字当 CSS 长度写回去」时不能用固定换算，得走下面的 unit()。

   跨模块共享的可变状态（装配时注入，段内既读又写）：
     · META        —— 只读（restoreLastPos 取 `last_pos`）；换论文时会被整体替换 → getter
     · posRestored —— “首次进入是否已按 last_pos 定位过”，render() 也会读 → getter + setter
   其余（pages / ZOOM 等）装配时取一次即可。

   装配（reader2.js）：
     const { ZOOM_OK, GZ, vpW, vpH, scrolled, toScrollTop, toScrollDelta, applyZoom,
             anchorProbe, pageAnchor, restoreAnchor, posValid, posIsTail, restoreLastPos } =
       R2View({ ZOOM, pages, get META() { return META; },
                get posRestored() { return posRestored; },
                set posRestored(v) { posRestored = v; } });
   ============================================================================ */
window.R2View = function (env) {
  "use strict";
  const { ZOOM, pages } = env;

  const ZOOM_OK = "zoom" in document.documentElement.style;
  const GZ = () => (ZOOM_OK ? ZOOM() : 1);                          // 真正作用在整页上的缩放比
  const vpW = () => document.documentElement.clientWidth / GZ();    // 视口宽(布局坐标)
  const vpH = () => window.innerHeight / GZ();                      // 视口高(布局坐标)
  const scrolled = () => window.scrollY / GZ();                     // 已滚动距离(布局坐标)
  const toScrollTop = (y) => Math.max(0, Math.round(y * GZ()));     // 布局坐标 → scrollTo 的视觉坐标
  const toScrollDelta = (d) => Math.round(d * GZ());                // 布局位移 → scrollBy 的视觉位移
  function applyZoom() {
    // 不支持 CSS zoom 的老浏览器退回旧行为(只缩放正文页宽)，不至于点了按钮没反应
    if (ZOOM_OK) document.documentElement.style.zoom = String(ZOOM());
  }

  /* ---- 跨浏览器 zoom 口径（实测，不猜引擎）----
     同一个 px 数字，各家 rect 给的单位并不一样：
       · Chrome：rect 不含 zoom（与 offsetWidth 相同）→ 数字可以直接当 CSS 长度用；
       · Firefox / Safari：rect 会把 zoom 乘进去 → 同一个数字再写成 CSS 长度，
         会被 zoom **再乘一次**。
     于是「先量 rect、再把数字当 CSS 长度写回去」的地方（页码胶囊的 --page-cx、
     AI 收起胶囊的 right / maxWidth / padding-bottom）在 Firefox 上整组偏出页面中线，
     缩放越大偏越多（150% 最明显）。
     这里用探针实测两件事，与引擎无关：
       · k  = 1 个 CSS 长度单位 = 多少 rect 单位（1000px 宽的条量出来就是 k×1000）；
       · vw / vh = 视口尺寸，单位是 **CSS 长度**（铺满视口的 fixed 盒 rect / k）。
     需要「rect → CSS 长度」时一律 /k。
     ⚠️ 探针挂在 body 上：只有 <html> 有 zoom，两个胶囊与页面都在同一个 zoom 上下文里。
     ⚠️ k / vw / vh 只跟当前 zoom 与视口大小有关，量到就缓存；zoom 或窗口变了会自动重测。 */
  const UNIT_PX = 1000;                       // 探针基准宽：越大相对误差越小
  const unitCache = { stamp: "", k: 1, vw: 0, vh: 0, box: null, bar: null };
  function unit() {
    const de = document.documentElement;
    const stamp = (de.style.zoom || "") + "|" + de.clientWidth + "x" + de.clientHeight
                + "|" + window.innerWidth + "x" + window.innerHeight;
    if (unitCache.stamp === stamp) return unitCache;
    const host = document.body || de;
    if (!unitCache.box) {
      /* 铺满视口的 fixed 盒：它的 rect 就是「视口」在 rect 口径下的尺寸。
         overflow:hidden 是为了让下面那条 1000px 的量尺**不撑出可滚动区域**。 */
      const box = document.createElement("div");
      box.setAttribute("aria-hidden", "true");
      box.style.cssText = "position:fixed;left:0;top:0;right:0;bottom:0;display:block;"
        + "overflow:hidden;visibility:hidden;pointer-events:none;z-index:-1";
      /* 定宽 0 高的一条：只量宽度，不会撑出滚动区 */
      const bar = document.createElement("div");
      bar.style.cssText = "position:absolute;left:0;top:0;width:" + UNIT_PX + "px;height:0;display:block";
      box.appendChild(bar);
      unitCache.box = box;
      unitCache.bar = bar;
    }
    if (unitCache.box.parentNode !== host) host.appendChild(unitCache.box);
    const rb = unitCache.box.getBoundingClientRect();
    const ra = unitCache.bar.getBoundingClientRect();
    unitCache.k = ra.width > 0 ? ra.width / UNIT_PX : 1;
    unitCache.vw = rb.width / unitCache.k;
    unitCache.vh = rb.height / unitCache.k;
    unitCache.stamp = stamp;
    return unitCache;
  }

  /* ---- 重排时的“视觉锚点” ----
     缩放 / 窗口变化 / 目录与笔记栏开关都会改变页面尺寸，进而整页重建；
     重建后如果不按**同一处内容**还原滚动位置，用户就会莫名其妙地“跳页”
     （尤其是目录跳转之后 —— 旧行为是每次重排都滚回读到过的最后一页）。
     锚点记成「第几页 + 页内比例」，重排完再换算回新的像素位置。
     注意：getBoundingClientRect() 是**视口坐标**，探针也必须用视口坐标量，
     否则会算出“文档坐标减视口坐标”这种毫无意义的比值。 */
  function anchorProbe() {
    // 顶栏是 sticky 的，量它的底边：正文里第一行「看得见」的内容就在这下面一点
    const bar = document.querySelector(".topbar");
    return (bar ? bar.getBoundingClientRect().bottom : 0) + 8;
  }
  function pageAnchor() {
    if (!pages.length) return null;
    const probe = anchorProbe();
    const last = pages[pages.length - 1].el.getBoundingClientRect();
    if (last.bottom <= probe) {
      // 视口已滚到正文末尾：按“离文档底部多远”记，重排后保持在正文末尾。
      return { pi: -1, fromBottom: (document.documentElement.scrollHeight - window.scrollY) / GZ() };
    }
    let pi = 0;
    for (let i = 0; i < pages.length; i++) {
      if (pages[i].el.getBoundingClientRect().top <= probe) pi = i;
    }
    const r = pages[pi].el.getBoundingClientRect();
    return { pi, ratio: clamp((probe - r.top) / (r.height || 1), 0, 1) };
  }
  function restoreAnchor(a) {
    if (!a) return;
    if (a.pi < 0) {
      const maxTop = document.documentElement.scrollHeight / GZ();
      const top = Math.max(0, maxTop - (a.fromBottom || 0));
      window.scrollTo({ top: toScrollTop(top), behavior: "auto" });
      return;
    }
    if (!pages[a.pi]) return;
    const r = pages[a.pi].el.getBoundingClientRect();
    const top = scrolled() + r.top + a.ratio * r.height - anchorProbe();
    window.scrollTo({ top: toScrollTop(top), behavior: "auto" });
  }

  /* ---- “上次停在哪儿”的精确定位（服务端 `META.last_pos`） ----
     以前只记 `last_page`（页码，而且还是 `Math.max` 只增不减的最远那页），
     重进阅读器只能落在**页首**，往回翻过的还会被拽回最远处。
     现在滚动时把**当前这处内容**记下来，重进按它原样还原：
       · `{pi, ratio}`           第几页 + 页内比例（与窗口宽度 / 缩放无关）
       · `{pi:-1, fromBottom}`   已经滚到正文末尾，记“离文档底部多远” */
  function posValid(p) {
    if (!p || typeof p !== "object" || !pages.length) return false;
    if (typeof p.fromBottom === "number" && p.fromBottom >= 0) return true;
    return typeof p.pi === "number" && p.pi >= 0 && typeof p.ratio === "number";
  }
  function posIsTail(p) {
    return !!p && (typeof p.fromBottom === "number"
      || (typeof p.pi === "number" && p.pi < 0));
  }
  /* 还原到 `META.last_pos`；没有可用数据返回 false（调用方退回页码逻辑）。
     onlyTail=true 只处理“停在正文末尾”那种位置。 */
  function restoreLastPos(onlyTail) {
    const p = env.META && env.META.last_pos;
    if (!posValid(p)) return false;
    env.posRestored = true;                                   // 首次定位已经发生
    if (onlyTail && !posIsTail(p)) return true;               // 已经摆好了，不再动
    if (typeof p.pi === "number" && p.pi >= pages.length) return false;   // 页数变了（重解析）
    restoreAnchor(p);
    return true;
  }

  /* ============ 对外接口 ============ */
  return {
    ZOOM_OK, GZ, vpW, vpH, scrolled, toScrollTop, toScrollDelta, applyZoom, unit,
    anchorProbe, pageAnchor, restoreAnchor, posValid, posIsTail, restoreLastPos,
  };
};
