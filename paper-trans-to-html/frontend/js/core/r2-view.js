/* ============ 视口坐标 / 缩放 / 滚动位置还原：从 reader2.js 拆出来的模块 ============
   缩放是“整体缩放”：把缩放比写到 <html> 的 zoom 上，顶栏 / 目录 / 笔记栏 / AI 面板
   连同正文一起放大，而不只是把页面撑宽。zoom 生效后测量 API 分成两套坐标，别混用：
     · getBoundingClientRect() / offsetWidth|Height             → 布局坐标(未乘 zoom)
     · clientWidth|Height / innerWidth|Height / scrollY|Height  → 视觉坐标(已乘 zoom)
   凡是两套坐标相遇的地方(滚动、点击取句、视口宽度)，都用这里几个换算函数过一手。

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
    ZOOM_OK, GZ, vpW, vpH, scrolled, toScrollTop, toScrollDelta, applyZoom,
    anchorProbe, pageAnchor, restoreAnchor, posValid, posIsTail, restoreLastPos,
  };
};
