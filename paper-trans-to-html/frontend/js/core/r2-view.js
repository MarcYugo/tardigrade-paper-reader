/* ============ 视口坐标 / 缩放 / 滚动位置还原：从 reader2.js 拆出来的模块 ============
   缩放是“整体缩放”：把缩放比写到 <html> 的 zoom 上，顶栏 / 目录 / 笔记栏 / AI 面板
   连同正文一起放大，而不只是把页面撑宽。zoom 生效后测量 API 分成两套坐标，别混用：
     · getBoundingClientRect() / offsetWidth|Height             → 布局坐标(未乘 zoom)
     · clientWidth|Height / innerWidth|Height / scrollY|Height  → 视觉坐标(已乘 zoom)
   凡是两套坐标相遇的地方(滚动、点击取句、视口宽度)，都用这里几个换算函数过一手。
   ⚠️ 上面这套口径**只有 Chrome 成立**：Firefox / Safari 的 getBoundingClientRect() 会把 zoom
   乘进去。要「先量 rect、再把数字当 CSS 长度写回去」时不能用固定换算，得走下面的 unit()。

   applyZoom() 带**缩放过渡**：改 zoom 会让整篇重排（实测一次 0.4~1.5s），所以过渡不用 zoom 做 ——
   先落位（写 zoom + 按比例改滚动），再把视觉用 GPU transform 摆回旧比例、动画回真实状态。
   GZ() 返回 `<html>` 上**实际写着**的比例（落位后就是目标档位）。详见 applyZoom 上方的注释。
   flipCapture() / flipPlay() 是同一套时长/缓动的**通用 FLIP**：DOM 重排（笔记卡
   从句子下方 ↔ 右侧批注栏）前后量一遍矩形，用 transform 补出过渡，不触发布局。

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
  /* ---- 缩放过渡（丝滑的关键全在这段注释里） ----
     ⚠️ **不能逐帧改 `<html>` 的 zoom**：改一次 zoom 就要重排整篇 —— 实测这篇论文
        （8.5k~9.8k 元素 / 15 页）：样式重算 ~130ms + 布局 250~1500ms + 重绘 ~600ms，
        点一下缩放就要 **1~2 秒**主线程。200ms 的动画连两帧都跑不完，「丝滑」无从谈起
        （对照：改 body 的 font-size 只要 27ms；换成在 body / #pages 上写 zoom 也一样贵）。
     ⚠️ 也不能「先落位再动画」：落位后整篇的栅格全部作废，动画每一帧都在重画，帧间隔 200~400ms。
     ✅ 实际采用的顺序（只动 GPU，不触发重排）：
        ① **在旧档位下动画**：给 `.page` 逐页（而不是那个 15000px 高的 `#pages` 巨型合成层 ——
           它每帧都要重绘，实测一帧卡 1~3s）+ 顶栏 / 已展开的侧栏套 translate+scale，
           插值到「落位后该在的位置」（见 zoomFxTargets / setZoomFx）。此时版式与栅格都还是旧的
           （已画好、没有待处理的重绘），纯合成层缩放 —— 实测 10~13 帧 / 200ms；
        ② **最后一帧才落位**：写 zoom + 按比例改滚动 + 摘 transform，全在同一个任务里。
           因为进度到 1 时 transform 与落位后的几何**逐像素重合**（实测残留 ≤0.3px），
           所以那一笔 1~2s 的重排重绘发生在画面已经到位之后 —— 用户看不到卡顿。
     代价：过渡那 200ms 里正文是栅格放大（5% 一档无感；跳大档会略糊）；
     浮动胶囊（页码 / AI）不参与 —— 它们只差一个档位的几像素。 */
  const ZOOM_ANIM_MS = 200;
  /* 过渡缓动：起步不突兀、收尾不硬停（纯 easeOut 第一帧会跳掉 1/4 的距离）。
     缩放过渡与笔记卡的 FLIP 过渡共用这一个，节奏才一致。 */
  const animEase = (p) => (p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2);
  /* 跟着一起过渡的元素 + 水平位置的预测方式（竖直一律按「普通缩放」推）：
       page  —— `.page`：落位后是否还居中要按**目标档位**算（放不下时 `margin:auto` 退化成 0，
                页面从居中变靠左，布局位置会变 — 不换预期位置的话落位那一帧会横跳 70~120px）；
       left  —— 顶栏 / 目录：贴左上角，布局位置不随 zoom 变；
       right —— 侧栏：贴右边缘，布局位置 = 布局视口宽 − 自身宽。
     ⚠️ 自己带 transform 的元素（滑入滑出的面板）跳过 —— 不能把别人的位移覆盖掉。 */
  const ZOOM_FX_SEL = [[".page", "page"], [".topbar", "left"], [".toc-panel", "left"], [".side-panel", "right"]];
  let zoomRaf = 0;          // 过渡的 rAF id（0 = 没在跑）
  let zoomDone = null;      // 过渡结束（已落位）后的回调（r2-nav 用它刷新数值 + relayoutAll）
  let zoomFx = [];          // 本轮正在 transform 的元素：[{ el, ex, ey, px }]
  /* 真正作用在整页上的缩放比 = `<html>` 上**实际写着**的值。落位前它还是旧档位，
     所有 rect / 滚动换算都必须用这个，不是 ZOOM()。 */
  const htmlZoom = () => parseFloat(document.documentElement.style.zoom) || ZOOM();
  const GZ = () => (ZOOM_OK ? htmlZoom() : 1);
  const vpW = () => document.documentElement.clientWidth / GZ();    // 视口宽(布局坐标)
  /* 指定档位下的布局视口宽：`clientWidth` 是**视觉**宽（不随 zoom 变），所以能提前算出
     「落位到 z 之后布局视口有多宽」。缩放过渡前要预判「笔记栏还放不放得下」就得用它
     （此刻 `<html>` 上还是旧档位，GZ() 给的是旧值）。
     不支持 CSS zoom 的老浏览器：布局视口根本不随档位变 → 恒等于 clientWidth（与 vpW() 同）。 */
  const vpWAt = (z) => !ZOOM_OK ? document.documentElement.clientWidth
                              : document.documentElement.clientWidth / (z > 0 ? z : GZ());
  const vpH = () => window.innerHeight / GZ();                      // 视口高(布局坐标)
  const scrolled = () => window.scrollY / GZ();                     // 已滚动距离(布局坐标)
  const toScrollTop = (y) => Math.max(0, Math.round(y * GZ()));     // 布局坐标 → scrollTo 的视觉坐标
  const toScrollDelta = (d) => Math.round(d * GZ());                // 布局位移 → scrollBy 的视觉位移
  const writeZoom = (z) => { document.documentElement.style.zoom = String(z); };
  /* 落位：写 zoom，并把滚动按比例重算 —— 让「视口左上角那处内容」还是原来那一处。
     （zoom 是绕**文档原点**缩放的：不重算滚动的话，放大时整篇会往上噬。） */
  function commitZoom(to, zf) {
    const sx = window.scrollX, sy = window.scrollY;
    writeZoom(to);
    if (Math.abs(to - zf) > 1e-4 && (sx || sy)) window.scrollTo(sx / zf * to, sy / zf * to);
  }
  function clearZoomFx() {
    for (let i = 0; i < zoomFx.length; i++) {
      const st = zoomFx[i].el.style;
      st.transform = "";
      st.transformOrigin = "";
      st.willChange = "";
    }
    zoomFx = [];
  }
  /* 收集本轮要一起过渡的元素，并算出它**落位后会在哪**（px/ey 都是 CSS 长度、视口相对）。
     · ex / ey / wl —— 当前的位置与宽度（rect 是视口相对的布局位置，/unit().k 换算成 CSS 长度，
       跨引擎口径 —— Firefox / Safari 的 rect 会把 zoom 乘进去）；
     · px —— 落位后的水平位置：普通缩放就还是 ex；页面要按「目标档位下容器够不够宽」重算居中量；
       侧栏按「布局视口宽 − 自身宽」。 */
  function zoomFxTargets(to, zf) {
    const u = unit();
    const de = document.documentElement;
    const pagesEl = document.getElementById("pages");
    const pbox = pagesEl ? pagesEl.getBoundingClientRect() : null;
    const contL = pbox ? pbox.left / u.k : 0;
    /* 目标档位下的页面容器宽度 = 现在宽度 + 布局视口宽的变化量（左右 padding 是固定 px） */
    const roomTo = pbox ? pbox.width / u.k + de.clientWidth / to - de.clientWidth / zf : 0;
    const lvTo = de.clientWidth / to;                   // 目标档位下的布局视口宽
    const out = [];
    for (let i = 0; i < ZOOM_FX_SEL.length; i++) {
      const sel = ZOOM_FX_SEL[i][0], rule = ZOOM_FX_SEL[i][1];
      const list = document.querySelectorAll(sel);
      for (let j = 0; j < list.length; j++) {
        const node = list[j];
        if (getComputedStyle(node).transform !== "none") continue;
        const r = node.getBoundingClientRect();
        const ex = r.left / u.k, ey = r.top / u.k, wl = r.width / u.k;
        let px = ex;
        if (rule === "page") px = contL + (wl <= roomTo ? (roomTo - wl) / 2 : 0);
        else if (rule === "right") px = lvTo - wl;
        out.push({ el: node, ex: ex, ey: ey, px: px });
      }
    }
    return out;
  }
  /* 把第 q 步（q = 缓动后的进度，0 → 1）写上去。K = to/zf（整轮不变），k = 1 + (K−1)q 是缩放比：
     左边缘从 ex 插到 px·K（K 是因为动画运行在**旧档位**的坐标系里），上边缘从 ey 插到 ey·K。 */
  function setZoomFx(q, K) {
    const sc = (1 + (K - 1) * q).toFixed(5);
    for (let i = 0; i < zoomFx.length; i++) {
      const it = zoomFx[i];
      const tx = (it.px * K - it.ex) * q, ty = it.ey * (K - 1) * q;
      it.el.style.transform = (Math.abs(tx) < 0.05 && Math.abs(ty) < 0.05)
        ? "scale(" + sc + ")"
        : "translate(" + tx.toFixed(2) + "px," + ty.toFixed(2) + "px) scale(" + sc + ")";
    }
  }
  function applyZoom(onDone) {
    // 不支持 CSS zoom 的老浏览器退回旧行为(只缩放正文页宽)，不至于点了按钮没反应
    if (!ZOOM_OK) { if (onDone) onDone(); return; }
    const to = ZOOM();
    const zf = htmlZoom();                       // 落位前 `<html>` 上的比例
    const pagesEl = document.getElementById("pages");
    if (zoomRaf) { cancelAnimationFrame(zoomRaf); zoomRaf = 0; }   // 连点 / 连滚：上一条直接落位
    clearZoomFx();                               // 摘掉上一条的 transform，rect 才量得准
    zoomDone = onDone || null;
    const reduce = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    const kTo = zf > 0 ? to / zf : 1;            // 过渡终点（相对现在的比例）
    if (!pagesEl || reduce || Math.abs(kTo - 1) < 1e-4) {
      commitZoom(to, zf);
      finishZoom();
      return;
    }

    zoomFx = zoomFxTargets(to, zf);
    /* 一页都没有（首屏还没建好）就没什么好过渡的，直接落位 */
    if (!zoomFx.length || !document.querySelector(".page")) { commitZoom(to, zf); finishZoom(); return; }
    for (let i = 0; i < zoomFx.length; i++) {
      zoomFx[i].el.style.transformOrigin = "0 0";
      zoomFx[i].el.style.willChange = "transform";
    }

    /* 几何：动画跑在**旧档位** zf 的坐标系里，所以「落位后视觉位置 = px·zt」
       在这套坐标系里要写成「rect = px·K」（K = zt/zf）。每帧按缓动进度 q：
         k  = 1 + (K−1)·q        缩放比
         tx = (px·K − ex)·q      左边缘：ex → px·K
         ty = ey·(K−1)·q         上边缘：ey → ey·K（竖直方向页面位置不随 zoom 变）
       q=0 就是现在；q=1 就是落位后的真实布局（px/ey 本来就是按落位后的布局算的），
       所以落位那一帧不跳（实测 100→150 / 150→95 / 95→100 三组偏差 ≤0.3px）。
       `px` 的预测方式见 zoomFxTargets：页面临界处“居中 ↔ 靠左”会变，必须按目标档位算。 */
    // easeInOutCubic：起步不突兀、收尾不硬停
    /* 时间推进用「有效帧时长」（单帧封顶 48ms）：遇到后台重排/大绘制造成的长卡顿，
       动画不会被一下子白跳过去，而是接着慢慢走完。 */
    let last = 0, elapsed = 0;
    const frame = (now) => {
      if (last) elapsed += Math.min(48, now - last);
      last = now;
      const p = Math.min(1, elapsed / ZOOM_ANIM_MS);
      if (p < 1) {
        setZoomFx(animEase(p), kTo);
        zoomRaf = requestAnimationFrame(frame);
        return;
      }
      /* 最后一帧：落位 + 摘 transform，同一个任务里完成 —— 视觉与上一帧完全重合，
         这之后 1~2s 的重排/重绘用户是看不到的。 */
      zoomRaf = 0;
      commitZoom(to, zf);
      clearZoomFx();
      finishZoom();
    };
    zoomRaf = requestAnimationFrame(frame);
  }
  function finishZoom() {
    const fn = zoomDone;
    zoomDone = null;
    if (fn) fn();
  }

  /* ---- 通用 FLIP 过渡（笔记卡「并排右侧 ↔ 收回句子下方」这类重排用）----
     重排（render）会把 DOM 重建，卡片会从句子下方「瞬移」到右边批注栏。这里用
     First-Last-Invert-Play 补一段动画：重排前量一遍矩形（flipCapture），重排后再量一遍，
     用 transform 把元素摆回旧的位置/尺寸，然后 200ms 内回到 0 —— 位置和大小都平滑过去，
     纯 transform 不触发布局（与缩放过渡同一套时长和缓动）。
     ⚠️ 元素靠 `data-nkey` 认身份（r2-notes.js 建卡片时写的，同一张卡重排后 key 不变）。 */
  let flipRaf = 0;
  let flipItems = [];                 // 正在动画的元素：[{ el }]
  function flipClear() {
    if (flipRaf) { cancelAnimationFrame(flipRaf); flipRaf = 0; }
    for (let i = 0; i < flipItems.length; i++) {
      const st = flipItems[i].el.style;
      st.transform = "";
      st.transformOrigin = "";
      st.willChange = "";
    }
    flipItems = [];
  }
  function flipNow() {
    const k = unit().k, z = htmlZoom(), out = {};
    const list = document.querySelectorAll(".ncard[data-nkey]");
    for (let i = 0; i < list.length; i++) {
      const r = list[i].getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;         // 隐藏的卡片（.nbox hidden）不参与
      // 存 CSS px + 当时的 zoom：重排经常跟缩放同时发生，后面要换算成「视觉位置」对齐
      out[list[i].dataset.nkey] = {
        el: list[i], z: z,
        l: r.left / k, t: r.top / k, w: r.width / k, h: r.height / k,
      };
    }
    return out;
  }
  /* 重排前：量一遍，返回给 flipPlay 用（没有卡片就返回 null） */
  function flipCapture() {
    flipClear();
    const now = flipNow();
    return Object.keys(now).length ? now : null;
  }
  /* 重排后：把卡片摆回旧位置再动画到真实位置。
     `extra(q)`（可选）是同一根时间轴上的另一份动画（如正文块的 shift 位移），
     q = 缓动后的进度；它在 q=0 时先把要动画的东西摆回旧状态，q=1 时写终值。
     `done`（可选）在动画收尾（含“没有要动的元素”那种直接返回）时调一次 ——
     「先让笔记卡移到位、再启动缩放过渡」就是靠它接力的（见 layout.js::switchSideFor）。
     ⚠️ extra(0) 必须在量卡片之前调：卡片的新位置要按「块还在旧位移上」来量，
        否则卡片的 FLIP 会把块位移再算一遍。 */
  function flipPlay(before, extra, done) {
    if (extra) extra(0);
    const now = before ? flipNow() : null;
    const zt = htmlZoom();                             // 当前档位
    const items = [];
    for (const key in (now || {})) {
      const b = before[key], a = now[key];
      if (!a) continue;
      /* 旧矩形换算到**当前档位**的视觉 px（缩放和重排可能同时发生：zoom 变了就要按比例换） */
      const f = b.z / zt;
      const sx = (b.w * f) / a.w, sy = (b.h * f) / a.h;
      const tx = b.l * f - a.l, ty = b.t * f - a.t;
      // 位置/尺寸几乎没变的（比如根本没切模式）不参与，免得白套一层合成层
      if (Math.abs(tx) < 0.5 && Math.abs(ty) < 0.5 && Math.abs(sx - 1) < 0.01 && Math.abs(sy - 1) < 0.01) continue;
      a.el.style.transformOrigin = "0 0";
      a.el.style.willChange = "transform";
      items.push({ el: a.el, tx: tx, ty: ty, sx: sx, sy: sy });
    }
    if (!items.length && !extra) { if (done) done(); return; }
    flipItems = items;
    const set = (q) => {
      const r = 1 - q;
      for (let i = 0; i < items.length; i++) {
        const it = items[i];
        it.el.style.transform = "translate(" + (it.tx * r).toFixed(2) + "px," + (it.ty * r).toFixed(2) + "px)"
          + " scale(" + (1 + (it.sx - 1) * r).toFixed(4) + "," + (1 + (it.sy - 1) * r).toFixed(4) + ")";
      }
      if (extra) extra(q);
    };
    set(0);                                   // 同步摆回旧位置：与 render 在同一个任务里，不会闪
    /* 时间推进用「有效帧时长」（单帧封顶 48ms）：render 之后那一笔大绘制可能把主线程卡住几百 ms，
       按墙钟计时的话动画会在卡顿里直接跑到终点（只看到“动一下”）；这样只会少走几帧，接着走完。 */
    let last = 0, elapsed = 0;
    const frame = (ts) => {
      if (last) elapsed += Math.min(48, ts - last);
      last = ts;
      const p = Math.min(1, elapsed / ZOOM_ANIM_MS);
      if (p >= 1) {                                    // set(1) 把块/公式框写成终值，flipClear 只清卡片的临时 transform
        set(1); flipClear();
        if (done) done();
        return;
      }
      set(animEase(p));
      flipRaf = requestAnimationFrame(frame);
    };
    flipRaf = requestAnimationFrame(frame);
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
    ZOOM_OK, GZ, vpW, vpWAt, vpH, scrolled, toScrollTop, toScrollDelta, applyZoom, unit,
    anchorProbe, pageAnchor, restoreAnchor, posValid, posIsTail, restoreLastPos,
    flipCapture, flipPlay,          // 通用 FLIP：重排前后把元素位置/尺寸平滑过去（笔记卡并排 ↔ 内嵌）
  };
};
