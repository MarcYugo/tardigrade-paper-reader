/* ============ 版面布局：从 reader2.js 拆出来的模块 ============
   负责“渲染完成之后”的一切摆位：
     · 页面宽度与批注栏（`computePageTarget` / `applyNoteChrome`）；
     · 卡片（译文 / 笔记 / 所选句译文）把下面的版式顶开（`relayoutPage`）；
     · 右侧批注栏里笔记卡自上而下避让（`layoutSideNotes`）；
     · 文末 AI 面板宽度不够时的上下堆叠（`layoutAi`）。
   它是所有模块的“枢纽”：笔记 / 目录 / AI 三个模块都要反向调用 relayoutAll、relayoutPage。

   跨模块共享（装配见 reader2.js）：
     · curPageW / noteSide / noteW / noteLeft  本模块既读又写 → getter + setter
     · DOC / pages / itemById 系稳定引用（DOC 换论文会整体替换 → getter）
     · notesCount / notesOfBlock（r2-notes）、tocPanelOpen / TOC_W（r2-nav）装配在本段
       之后 → 转发调用，避免 TDZ
   ============================================================================ */
window.R2Layout = function (env) {
  "use strict";
  const $ = env.$;          // #id 查询（reader2 的局部简写）

  /* 页面比例（--page-scale）先按上次算出来的值铺上：文档没加载完时它还没人写，
     底部 AI 卡片这类浮层就会先按大字号出现、等版式算完才跳成小字号。
     页面实际的字体尺寸在 render() 里由 applyNoteChrome() 重新算，这里只影响"加载中"那一瞬。 */
  const PAGE_SCALE_KEY = "paperReader.pageScale";
  let pageScaleSaved = "";      // 上次写进 localStorage 的值：一样就不重复写（缩放时会反复调用）
  try {
    const savedScale = localStorage.getItem(PAGE_SCALE_KEY);
    if (savedScale) document.body.style.setProperty("--page-scale", savedScale);
  } catch (e) { /* storage unavailable */ }

  /* ================= 布局 =================
     两种展开形式：
       1) 译文卡：留在句子下方，把后续版式内容顶开(“顶开版式”)；
       2) 笔记卡：绝对定位在**页面右侧的批注栏**里，纵向对齐笔记对应句子所在行
          —— 与正文同高、不占版面；同页多张卡自上而下依次避让。
     有笔记时会给页面右侧“预留”批注栏宽度(见 --note-reserve)：
       窗口够宽 → 页面保持原尺寸并整体左移，右边让出批注栏；
       窗口不够宽 → 页面按可用宽度等比缩小(fit)，保证能并排；
       实在放不下(可用宽度 < 520px，或右侧面板展开中) → 笔记回到句子下方内嵌。 */
  function xOverlap(a, b, aw) {
    const w = aw || a.w;
    return Math.max(0, Math.min(a.x + w, b.x + b.w) - Math.max(a.x, b.x)) > 1;
  }
  /* ================= 栏（column）几何 =================
     译文/笔记卡片的宽度要跟**所在栏**一致，不能跟「块」一致：
     一个块常常只是同一行被公式切开的一小段（宽度忽大忽小），按块宽出卡就会
     一行宽一行窄、同一页里参差不齐。

     判定用**左边缘聚类**：一栏的正文绝大多数都从栏起点开始，而「从行中间开始的块」
     （公式切开的后半段）数量少、左边缘也各不相同，很自然会被筛掉。
     单栏论文 ⇒ 只有一个簇，栏宽 = 该簇里最长那一行；双栏论文 ⇒ 每栏一个簇。 */
  const COL_TOL = 16;        // 左边缘相差 ≤ 这个值算同一个「栏起点」
  const COL_MERGE = 24;      // 两个候选栏起点离得这么近 → 合并（首行缩进而已，不是分栏）
  function pageColumns(items) {
    const boxes = items.filter(it => it.kind === "blk" && (it.w || 0) > 8 && (it.h || 0) > 2);
    if (!boxes.length) return [];
    let left = Infinity, right = -Infinity;
    for (const b of boxes) { left = Math.min(left, b.x); right = Math.max(right, b.x + b.w); }
    const textW = right - left;
    if (!(textW > 0)) return [];

    const sorted = boxes.slice().sort((a, b) => a.x - b.x);
    const clusters = [];
    for (const b of sorted) {
      const last = clusters[clusters.length - 1];
      if (last && b.x - last.x <= COL_TOL) last.items.push(b);
      else clusters.push({ x: b.x, items: [b] });
    }
    // 太小的簇是「从行中间开始的碎片」，不当栏起点
    const minN = Math.max(2, Math.round(boxes.length * 0.15));
    let kept = clusters.filter(c => c.items.length >= minN);
    if (!kept.length) kept = [clusters.reduce((a, b) => (b.items.length > a.items.length ? b : a))];
    // 首行缩进造成的近邻簇并回左栏
    const merged = [];
    for (const c of kept) {
      const last = merged[merged.length - 1];
      if (last && c.x - last.x <= COL_MERGE) last.items.push(...c.items);
      else merged.push({ x: c.x, items: c.items.slice() });
    }
    // 栏宽 = 该簇里**行右边缘的 80 分位**：
    //   单栏论文大多数行都是满行 → 取到的是真正的栏右边界；
    //   少数短行（小节标题、最后一行）和跨栏 caption 都当成离群值忽略掉。
    //   ⚠️ 不能用「排除宽块再取 max」：单栏论文的正文本身就是最宽的那些块，
    //   把它们排除后只剩短行，栏宽会被算成 209px（线上踩过）。
    return merged.map(c => {
      const rights = c.items.map(b => b.x + b.w).sort((a, b) => a - b);
      const idx = Math.min(rights.length - 1, Math.floor((rights.length - 1) * 0.8));
      return { x: c.x, w: Math.max(1, rights[idx] - c.x) };
    });
  }
  // 某个块属于哪一栏（取横向重叠最多的那栏）；明显比栏还宽的块用它自己的范围
  function columnOf(it, cols) {
    if (!cols.length) return null;
    let best = null, bestOv = 0;
    for (const c of cols) {
      const ov = Math.min(it.x + it.w, c.x + c.w) - Math.max(it.x, c.x);
      if (ov > bestOv) { bestOv = ov; best = c; }
    }
    if (!best) return null;
    if (it.w > best.w + 4 && bestOv < it.w * 0.7) return { x: it.x, w: it.w };
    return best;
  }
  // 页面尺寸与批注栏宽度只跟窗口/缩放/有无笔记有关，不依赖已渲染的 DOM
  /* Unlimited-OCR 文档的默认显示比例。
     这一套解析给的是 PDF 真实字号（正文 12pt、标题 28pt），按 CSS_W(900) 铺满后
     比网页阅读习惯大一号。这里把**页面基准宽度**乘一个系数 —— 因为 S = pageW / PDF宽，
     所以正文/标题/公式/图片/行高/文字坐标全部同比例缩，块与块的相对位置一点不变。
     （只缩 font-size 不行：版式是绝对定位的，字号变小后每段底部会空出一截。）
     1 = 与 PDF 等比，0.8 = 更小。只影响 parser=unlimited_ocr 的文档。 */
  const DOC_SCALE = 0.85;
  function basePageW() {
    const p = env.DOC && env.DOC.parser;
    const k = (p === "unlimited_ocr" || p === "unlimited_ocr_online") ? DOC_SCALE : 1;
    return env.CSS_W * k;
  }
  function computePageTarget() {
    // 整体缩放时视口在布局坐标里只有 clientWidth/ZOOM 这么宽；页面宽度保持 CSS_W，
    // 由 <html> 的 zoom 把它放大(不支持 zoom 的浏览器才退回“直接算宽页面”的老算法)
    const clientW = env.vpW();
    const want = env.ZOOM_OK ? basePageW() : basePageW() * env.ZOOM();
    const drawerOpen = !!($("side") && $("side").classList.contains("open"));
    // 左侧目录展开时也要给它让出宽度，否则页面会被盖住
    const tocW = env.tocPanelOpen() ? env.TOC_W : 0;
    const need = env.notesCount() > 0 || !!env.editingKey;      // 有笔记(或正在写)才预留
    const reserve = clamp(Math.round(clientW * 0.25), 210, 330);
    const room = clientW - reserve - tocW - 36;         // 让出批注栏/目录后页面能用多宽
    const side = !drawerOpen && env.showNotesOn && need && room >= 520;
    return {
      side, reserve, tocW,
      noteW: Math.max(190, reserve - 28),
      pageW: side ? Math.min(want, room)
                  : (tocW ? Math.min(want, Math.max(320, clientW - tocW - 36)) : want),
    };
  }
  function applyNoteChrome(t) {
    document.body.classList.toggle("notes-side", t.side);
    document.body.style.setProperty("--note-reserve", t.reserve + "px");
    // 卡片(译文/笔记)字号跟着页面的实际渲染比例走，和正文一起缩放
    // (夹紧到 0.78~1.2，避免页面很小时字小到看不清)
    const scale = clamp(t.pageW / env.CSS_W, 0.78, 1.2).toFixed(3);
    document.body.style.setProperty("--page-scale", scale);
    // 记下来给下次进来时用（见上面的 PAGE_SCALE_KEY）：刷新后首帧就是对的字号
    if (scale !== pageScaleSaved) {
      pageScaleSaved = scale;
      try { localStorage.setItem(PAGE_SCALE_KEY, scale); } catch (e) { /* storage unavailable */ }
    }
    env.noteSide = t.side;
    env.noteW = t.noteW;
    env.noteLeft = t.pageW + 14;                            // 页面坐标：贴着页面右边线外侧
  }
  /* `only` = 只重排这几页（Set，逐页加载时空口令：每次只排刚挂上的页，
     其余页的 DOM 一动没动，没必要重新量）。不传 = 全部页。 */
  function relayoutAll(only) {
    syncReadCenter();                                // 目录/抽屉开合会改预留宽度 → 中线先更新
    if (!env.DOC || !env.pages.length) return;      // 论文还没加载好（或加载失败）：别去碰 DOM
    const t = computePageTarget();
    if (Math.abs(t.pageW - env.curPageW) > 0.5) { env.render(); return; }   // 页面尺寸变了 → 整体重排
    applyNoteChrome(t);
    layoutAi();
    env.pages.forEach((pg, pi) => {
      if (only && !only.has(pi)) return;
      relayoutPage(pi);
    });
    // 版式（含公式框位移）刚定下来：高亮公式框的“命中”是按几何算的，要重新对一遍
    env.paintFormulaMarks();
  }

  /* 文末面板：宽度不够时从“左右”变“上下”（.ai-stacked），上下结构下两块等宽。
     判断方式是先把类去掉、量一下两块是否真的落在同一行 —— 比猜一个阈值可靠
     （换行临界是两块 flex-basis 之和，改 CSS 后这里不用跟着改）。
     量完立刻恢复，整个过程在同一个任务里完成，不会闪。 */
  function layoutAi() {
    const box = $("aiBody"), chat = $("aiChat"), sum = $("aiSum");
    if (!box || !chat || !sum) return;
    box.classList.remove("ai-stacked");
    const wrapped = Math.abs(chat.offsetTop - sum.offsetTop) > 2;
    if (wrapped) box.classList.add("ai-stacked");
  }

  /* ---------- 页面列中线：浮动胶囊（页码 / AI 收起胶囊）的横向锚点 ----------
     这两个胶囊是 position: fixed，之前以**窗口**中线居中：目录（左 300px）或
     笔记抽屉（右 384px）一打开，正文整体平移，胶囊却留在原处 → 视觉错位。
     这里量出正文列（#pages 的内容盒，页面用 margin:auto 在它里面居中）的中线，
     写进 CSS 变量 --page-cx，CSS 里用 left: calc(var(--page-cx) + …) 定位。
     ⚠️ getBoundingClientRect() 是**布局坐标**（不乘 <html> 的 zoom），而 CSS 的
     left/bottom 也是布局坐标 → 直接用，不要除/乘缩放比。 */
  let lastCenterX = null;                 // 上次写入的值：一样就不重复写（滚动时会被反复调用）
  function readCenterX() {
    const pagesEl = $("pages");
    if (!pagesEl) return null;
    const r = pagesEl.getBoundingClientRect();
    return r.width ? r.left + r.width / 2 : null;
  }
  function syncReadCenter() {
    const cx = readCenterX();
    if (cx == null) return null;
    const v = Math.round(cx * 100) / 100;
    if (v !== lastCenterX) {
      lastCenterX = v;
      document.documentElement.style.setProperty("--page-cx", v + "px");
    }
    return cx;
  }
  syncReadCenter();
  if ($("pages")) {
    // 预留宽度（目录 / 批注栏）变化会改变 #pages 的宽度；窗口缩放同理
    if (window.ResizeObserver) new ResizeObserver(syncReadCenter).observe($("pages"));
    window.addEventListener("resize", syncReadCenter);
    // 页面比可视区宽时会出现横向滚动，中线要跟着内容走（竖向滚动不受影响，别每次滚动都量）
    let lastScrollX = window.scrollX;
    window.addEventListener("scroll", () => {
      if (window.scrollX === lastScrollX) return;
      lastScrollX = window.scrollX;
      syncReadCenter();
    }, { passive: true });
  }
  // 右侧模式：把笔记容器绝对定位到页面右侧；否则清掉定位、回到句子下方内嵌
  function applyNoteSide(it) {
    const box = it.nbox;
    if (!box) return;
    const side = env.noteSide && !box.hidden;
    box.style.position = side ? "absolute" : "";
    box.style.minWidth = side ? "0" : "";
    box.style.width = side ? env.noteW + "px" : "";
    box.style.left = side
      ? (env.noteLeft - it.x - (it.ulXShift || 0)) + "px" : "";
    if (!side) box.style.top = "";
  }
  // 右侧笔记卡：先按“句子首行”定高，再自上而下依次避让；返回最底部(页面坐标)
  function layoutSideNotes(items) {
    const boxes = items.filter(it => it.kind === "blk" && it.nbox && !it.nbox.hidden);
    for (const it of boxes) {
      const first = env.notesOfBlock(it)[0];
      const si = first ? env.keyParts(first.block_id).si : 0;
      const ld = it.linesData[env.lineOfChar(it, env.sentRange(it, si).a)] || it.linesData[0];
      it.noteDy = ld ? ld.dy : 0;                    // 句子首行相对块顶的偏移
      it.noteY = it.y + it.noteDy + (it.syShift || 0) + (it.ulShift || 0) + (it.shift || 0);  // 目标页面 y（含内容避让 + 译文顶开）
    }
    boxes.sort((a, b) => a.noteY - b.noteY);
    let cursor = -Infinity;
    for (const it of boxes) {
      const h = it.nbox.offsetHeight;
      const y = Math.max(it.noteY, cursor + env.NOTE_GAP);
      // ⚠️ nbox 挂在 `.exp` 里，而 `.exp` 是 `.blk` 的子元素 —— 块被内容避让
      //    （syShift/ulShift，走 style.top）和卡片顶开（shift，走 transform）挪动时，
      //    笔记卡**已经**跟着一起挪了。这里只补「目标页面 y」与「块当前页面 y」的差值；
      //    若再减去 `it.y + contentH`，位移量就被加了两遍：实测一张译文卡把正文顶开
      //    111px 时，笔记卡多下移了整整 111px，与它对应的句子彻底错位。
      const blkPageY = it.y + (it.syShift || 0) + (it.ulShift || 0) + (it.shift || 0);
      it.nbox.style.top = (y - blkPageY - it.contentH) + "px";   // 相对 .exp(块顶+contentH)
      cursor = y + h;
    }
    return boxes.length ? cursor : 0;
  }
  function relayoutPage(pi) {
    const pg = env.pages[pi];
    if (!pg) return;
    const items = pg.items;

    for (const it of items) if (it.kind === "blk") applyNoteSide(it);

    /* 卡片宽度 = **所在栏的宽度**（不是块的宽度）。
       块常常只是同一行被公式切开的一小段，按块宽出卡就会一行宽一行窄。
       先定宽再量高：宽度会改变换行行数，量高必须用最终宽度。 */
    const cols = pageColumns(items);
    for (const it of items) {
      if (it.kind !== "blk") continue;
      const col = it.col = columnOf(it, cols);
      if (col) {
        it.exp.style.left = (col.x - it.x - (it.ulXShift || 0)).toFixed(1) + "px";
        it.exp.style.width = col.w.toFixed(1) + "px";
      } else {
        it.exp.style.left = "";
        it.exp.style.width = "";
      }
    }

    for (const it of items) {
      it.expH = 0; it.expW = 0;
      if (it.kind !== "blk" || !it.exp.classList.contains("open")) continue;
      const inlineNotes = !env.noteSide && !it.nbox.hidden;   // 右侧模式下笔记不占版面
      if (it.tsec.hidden && it.stbox.hidden && !inlineNotes) continue;
      it.expH = it.exp.offsetHeight;
      it.expW = Math.max(it.col ? it.col.w : it.w, inlineNotes ? it.nbox.offsetWidth : 0);
    }
    // ⓪ Surya 内容和图片的避让（settleSuryaVertical 精确计算）：直接改 style.top，
    //    与卡片顶开（transform）互不干扰，幂等（同一布局内 syShift 不变）。
    //    ⚠️ 不走 ①/② 的位移链：那条链是给卡片设计的近似“同化”传播，会把
    //    精确的避让量再放大，反而让下方块被多推、造成新的重叠（第 2 页实测）。
    for (const it of items) {
      if (!it.surya || it.syShift === undefined) continue;
      it.el.style.top = (it.y + it.syShift).toFixed(1) + "px";
    }
    // ⓪-b Unlimited-OCR 内容和图片的二维避让（settleUnlimitedLayout 精确计算）：
    //    避让量存在 ulShift / ulXShift，与 Surya 的 syShift 各自独立。
    for (const it of items) {
      if (!it.unlimited || it.ulShift === undefined) continue;
      it.el.style.top = (it.y + it.ulShift).toFixed(1) + "px";
      it.el.style.left = (it.x + (it.ulXShift || 0)).toFixed(1) + "px";
    }
    /* ① 卡片矩形避让：一张展开的卡片占据**一块矩形**——
       纵向从宿主内容底往下 expH，横向是卡片宽度（expW）。

       规则（迭代到收敛，位移只取 max、不叠加）：
         · 纵向跟矩形相交、且横向重叠的块，顶边必须落到矩形底之下：
           `shift = 卡片底 − 块顶`。
         · 整块在矩形顶上方的（**同一视觉行被公式切成几块的左右邻块**）不动。
         · 同栏（横向重叠）的块位移必须单调不减 —— 否则被卡片推开的块会排到
           后面那些没被推的块上面去。

       ⚠️ 老写法是「命中一张卡就 `+= 卡片高`」，两个坑（线上实测 p7s3/p7s5 两张卡）：
         1) 判定用 `卡片顶 <= 我的底 + 0.5`，同行的邻块（底边与卡片顶只差 0.01px）
            也被推走；② 再把这个假位移传染给**卡片宿主自己**，卡片跟着一起下移，
            于是相对位置没变，卡片原地压住下面的正文；
         2) `+= 卡片高` 只在「块紧贴宿主内容底」时才够，块顶到卡片顶之间原有行距
            没算进去 → 卡片下方总要少推一截（实测压 15px）。 */
    const CARD_GAP = 6;                          // 卡片底与下方正文之间留的间隙
    const layoutX = it => it.x + (it.ulXShift || 0);
    const layoutY = it => it.y + (it.syShift || 0) + (it.ulShift || 0);
    // 卡片的横向范围：卡片已按栏对齐（`exp.style.left` 挪到栏起点），所以不能再用
    // 宿主的 `x/w` 判重叠，要拿**栏**的 x/w 去跟内容比。
    const cardHits = (o, it) => {
      const cx = o.col ? o.col.x : layoutX(o);
      const cw = o.expW || o.w;
      return Math.max(0, Math.min(cx + cw, layoutX(it) + it.w) -
                        Math.max(cx, layoutX(it))) > 1;
    };
    /* 阅读顺序：原始 y 更小 ⇒ 这块正文在卡片宿主**上方**。
       卡片只负责把它**下面**的内容顶开；一块上方的正文若被挤进了卡片矩形，
       说明它是被上面更早的卡片顶下来的 —— 这时要让卡片（连同宿主）挪到它下面，
       而不是把这块更早的正文继续往下拖。

       ⚠️ 少了这条判定会形成“级联下推”：A 卡把 T3 顶下去 → T3 落进 T4 卡的矩形
       → T3 又被 T4 卡顶下去 → 再落进 T5 卡…… 每次叠加一整张卡高 + 间隙，
       实测第 5 页 T3 被推到 594px、其后逐块 831/913px，卡下方空出半页
       （复算脚本可复现）。 */
    const isAbove = (it, o) => layoutY(it) < layoutY(o) - 0.5;
    for (const it of items) it.shift = 0;
    for (let pass = 0; pass <= items.length; pass++) {
      let grew = false;
      // (b) 同栏单调：后一块不能被甩到前一块上面（阅读顺序会乱、卡片区看着像“错行”）
      //     放在 (a) 前面：先把「宿主上面那块被顶开后，宿主自己也得跟着走」跑完，
      //     否则 (a) 会拿宿主还没跟上的旧位置去算卡片顶，一次就多推一截。
      for (let i = 1; i < items.length; i++) {
        const prev = items[i - 1], it = items[i];
        if (prev.kind !== "blk" || it.kind !== "blk") continue;
        if (Math.max(0, Math.min(layoutX(prev) + prev.w, layoutX(it) + it.w) -
                           Math.max(layoutX(prev), layoutX(it))) <= 1) continue;
        if ((it.shift || 0) < (prev.shift || 0) - 0.5) {
          it.shift = prev.shift || 0; grew = true;
        }
      }
      // (a) 卡片矩形：只把实际与卡片矩形相交、且横向重叠的内容移到卡片下方
      //     （`it` 不限于文字块：插图/表格压在卡片下面同样要顶开）。
      //     用实际相交判定而非块顶相对宿主块顶的位置，避免已避让下移的块仍压住卡片。
      //     cardTop 每轮重算：本规则里会改宿主的 shift（让位分支），矩形跟着动。
      for (const o of items) {
        if (o.kind !== "blk" || !o.expH) continue;
        for (const it of items) {
          if (it === o) continue;
          if (!cardHits(o, it)) continue;
          const cardTop = layoutY(o) + o.contentH + (o.shift || 0);
          const cardBottom = cardTop + o.expH + CARD_GAP;
          const ih = it.kind === "blk" ? it.contentH : it.h;    // 内容高（插图直接用外框）
          const top = layoutY(it) + (it.shift || 0);
          // 上方的正文被顶进了卡片矩形：卡片让到它下面（见 isAbove 说明）
          if (isAbove(it, o)) {
            const need = top + ih + CARD_GAP - (layoutY(o) + o.contentH);
            if (need > (o.shift || 0) + 0.5) { o.shift = need; grew = true; }
            continue;
          }
          if (top >= cardBottom - 0.5) continue;                 // 已经在卡片下方
          if (top + ih <= cardTop + 0.5) continue;               // 整块在卡片上方
          const need = cardBottom - layoutY(it);
          if (need > (it.shift || 0) + 0.5) { it.shift = need; grew = true; }
        }
      }
      // (c) 卡与卡也不能叠（卡片现在都是整栏宽，宿主在同一视觉行的两张卡必定相交）：
      //     两张卡矩形纵向相交时，把**阅读顺序靠后**的那张整体往下推。
      for (let oi = 0; oi < items.length; oi++) {
        const o = items[oi];
        if (o.kind !== "blk" || !o.expH) continue;
        const oTop = layoutY(o) + o.contentH + (o.shift || 0);
        const oBottom = oTop + o.expH + CARD_GAP;
        for (let ii = oi + 1; ii < items.length; ii++) {
          const it = items[ii];
          if (it.kind !== "blk" || !it.expH) continue;
          if (!cardHits(o, it)) continue;
          const itTop = layoutY(it) + it.contentH + (it.shift || 0);
          if (itTop + it.expH <= oTop + 0.5 || itTop >= oBottom - 0.5) continue;
          const need = oBottom - (layoutY(it) + it.contentH);
          if (need > (it.shift || 0) + 0.5) { it.shift = need; grew = true; }
        }
      }
      // ② 传递位移：被顶开的宽元素会把压在它正下方的内容也带下去。
      //    在卡片避让循环内传播，确保传播后移动的卡片宿主会再次参与碰撞计算。
      let propagationGrew = true;
      while (propagationGrew) {
        propagationGrew = false;
        for (const it of items) {
          const bottom = layoutY(it) + (it.kind === "blk" ? it.contentH : it.h);
          for (const m of items) {
            if (m === it || m.shift <= it.shift) continue;
            if (layoutY(it) <= layoutY(m) + 1) continue;
            const mb = layoutY(m) + (m.kind === "blk" ? m.contentH : m.h);
            const overlapX = Math.max(0, Math.min(layoutX(m) + m.w, layoutX(it) + it.w) -
                                        Math.max(layoutX(m), layoutX(it)));
            if (mb <= bottom + 0.5 && overlapX > 1) {
              it.shift = m.shift;
              propagationGrew = true;
              grew = true;
            }
          }
        }
      }
      if (!grew) break;
    }
    for (const it of items) {
      it.el.style.transform = it.shift ? "translateY(" + it.shift + "px)" : "";
    }

    /* 页面级公式框覆盖层（`.fbox`）跟随所属文字块的“卡片顶开”位移。
       公式框是页面级绝对定位，不属于块的 DOM 子树，必须单独平移；
       否则展开译文卡后正文下移、公式还停在原位。 */
    for (const it of items) {
      if (!it.local || !(it.formulaBoxes || []).length) continue;
      const dy = it.shift || 0;
      for (const fb of it.formulaBoxes)
        fb.style.transform = dy ? "translateY(" + dy + "px)" : "";
    }

    const noteBottom = env.noteSide ? layoutSideNotes(items) : 0;

    let maxB = pg.h;
    for (const it of items) {
      const base = layoutY(it) + (it.kind === "blk" ? it.contentH : it.h);
      const sy = it.syShift || 0;         // Surya 内容避让量（走 style.top，不在 shift 里）
      const ul = it.ulShift || 0;         // Unlimited-OCR 内容避让量（同口径，独立字段）
      let b = base + (it.shift || 0);
      if (it.expH) b = Math.max(b, base + (it.shift || 0) + it.expH);
      if (b > maxB) maxB = b;
    }
    if (noteBottom > maxB) maxB = noteBottom;   // 右侧笔记卡也不能压到下一页
    pg.el.style.height = Math.max(pg.h, maxB + 22) + "px";
  }

  /* ============ 对外接口 ============ */
  return { computePageTarget, applyNoteChrome, relayoutAll, relayoutPage, layoutAi, xOverlap, pageColumns,
           syncReadCenter, readCenterX, basePageW };
};
