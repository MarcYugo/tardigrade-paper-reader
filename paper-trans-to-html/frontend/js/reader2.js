/* 阅读器 v2：句子级交互
 *  - 版式还原(绝对坐标) + 每行按 PDF 线框适配宽度，避免双栏文字侵入中间空隙
 *  - 句子级高亮标注(黄/绿/粉，可清除)
 *  - 句子级笔记：直接内嵌在句子下方(与正文同层，默认展开)，右侧面板只做“全部笔记”总览
 *  - 段落级批量翻译(非实时、缓存)、阅读进度保存、缩放
 */
(function () {
  "use strict";

  const CSS_W = 900;                       // zoom=1 时页面目标宽度
  const ZOOMS = [0.8, 1, 1.2, 1.5];
  const FONT = {
    sans: '"Segoe UI","Helvetica Neue",Arial,"Microsoft YaHei",sans-serif',
    serif: 'Georgia,"Times New Roman","SimSun",serif',
    mono: '"Consolas","Courier New",monospace',
  };

  const docId = queryId("id");
  const pagesEl = document.getElementById("pages");

  /* 滚动位置由我们自己还原（`META.last_pos`，见 restoreLastPos）：关掉浏览器的
     自动滚动恢复，否则刷新/后退时它会按旧的像素偏移再滚一次，和我们的还原打架。 */
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";

  let DOC = null, META = null;
  let zoomIdx = 1;
  let posRestored = false;   // 首次进入是否已按服务端的 `last_pos` 定位过（见 restoreLastPos）
  const ZOOM = () => ZOOMS[zoomIdx];
  const pages = [];
  const itemById = {};
  const zh = {};            // 段落(块)id -> 中文
  const noZh = new Set();
  let anno = { highlights: {}, notes: [] };
  /* 暗夜下正文颜色的反色缓存（r2-color.js 的 pageInk 用，r2-notes.js 换主题时清空） */
  const inkCache = new Map();
  let sel = null;           // 选中：{ sents: [{item, si}, …] }，按阅读顺序(支持拖拽多选)
  let ordSeq = 0;           // 文字块全页连续序号(重置于 render)，用于比较先后
  const nextOrd = () => ordSeq++;   // 交给 js/items/* 里的块渲染器分配（render 时归零）
  let showZhOn = false;
  let showNotesOn = true;   // 笔记默认展开，和正文一起看
  let editingKey = null;    // 正在内嵌编辑的句子 key("p0b3#1")
  let noteDraft = "";       // 编辑中的草稿，重绘时不丢字
  let selTransList = [];    // 多选“只翻译所选句”的卡片列表：{ key, itemId, siFrom, text }
  // 笔记卡位置：默认贴在页面右侧批注栏、与句子同高；放不下则退回句子下方内嵌
  let noteSide = true, noteW = 300, noteLeft = 0;
  let curPageW = 0;         // 当前渲染的页面宽度(px)：变化需要整体 render
  const NOTE_GAP = 10;      // 右侧相邻笔记卡的最小垂直间距
  let _fontsHook = false;      // 字体就绪后的基线重排只挂一次
  const ZH_STORE_OLD = "paperReader.showZh";   // 旧版本的浏览器本地开关（升级后不再使用）

  const $ = (id) => document.getElementById(id);

  /* ---- 纯工具模块装配（都无状态，装配一次即可） ----
     · r2-text.js  句子切分 / 字符区间 / 复制文本 / 句子 key
     · r2-color.js HSL 换算 / 暗夜反色（共用上面的 inkCache）
     · r2-fit.js   逐行横向压缩（本地解析方案的绝对定位行）
     · r2-view.js  缩放 / 视口坐标换算 / 滚动位置还原（META、posRestored 用 getter） */
  const { splitSentences, sentOf, sentRange, lineOfChar,
          sentKey, keyParts, copyRangeText, copySentenceText, itemCopyText,
          normTrans } = R2Text();
  const { hex2rgb, rgb2hsl, hsl2hex, invertInk, pageInk, hexToRgba } =
    R2Color({ inkCache, isDark });
  const { gapStats, fitLineEl, fitItemLines } = R2Fit();
  const { ZOOM_OK, GZ, vpW, vpH, scrolled, toScrollTop, toScrollDelta, applyZoom,
          anchorProbe, pageAnchor, restoreAnchor, posValid, posIsTail,
          restoreLastPos } = R2View({
    ZOOM, pages,
    get META() { return META; },
    get posRestored() { return posRestored; },
    set posRestored(v) { posRestored = v; },
  });

  /* 解析后端名字 → 界面显示名（doc.json 里存的是稳定取值） */
  function parserLabel(name) {
    return name === "pymupdf" ? "本地解析方案"
         : name === "surya" ? "Surya 2"
         : name === "unlimited_ocr" ? "Unlimited-OCR"
         : (name || "?");
  }

  /* 句子切分 / 字符区间 / 复制文本 / 句子 key：实现在 r2-text.js（装配见上方 R2Text） */

  /* ================= 基础查询 ================= */
  function findItem(id) { return itemById[id] || null; }
  function sentenceText(item, si) {
    const ss = item.sents[si];
    return ss ? item.canonical.slice(ss.s, ss.e).replace(/\n+/g, " ").trim() : "";
  }
  function noteOf(key) { return (anno.notes || []).find(x => x.block_id === key) || null; }

  /* ================= 选中（句子级，可多句）：已拆到 js/select/selection.js =================
     本段持有“选中”的全部交互：点 / 拖拽取句、公式框选句锚定、选中态重绘，
     以及挂在 pagesEl、document 上的鼠标事件。
     · `sel` 是**跨模块共享的可变状态**（render / 布局 / 笔记 / 工具条都会读它）→
       装配用 getter + setter，改的还是本作用域里这个 `sel`；
     · `DOC` 换论文时会整体替换 → getter；
     · placeBar / updateBar / hideBar 来自后面的《浮动工具条》段，装配这一刻还在
       TDZ → 包一层转发，等真正调用时再去读。 */
  const { selCount, selAnchor, selItems, selKeys, isSelRef, refsBetween, pageOfItem,
          setSelection, clearSelection, selectSentence, refreshSelectionVisual,
          attachFormulaBoxes, applySelRanges } = R2Selection({
    get DOC() { return DOC; },
    get sel() { return sel; }, set sel(v) { sel = v; },
    findItem, itemById, lineOfChar, pages, pagesEl, GZ,
    paintFormulaMarks, rebuildItem, sentKey, sentOf, sentRange, setAnnoRanges,
    placeBar: (...a) => placeBar(...a),
    updateBar: (...a) => updateBar(...a),
    hideBar: (...a) => hideBar(...a),
  });

  /* ================= 渲染 ================= */
  /* 主题配色（HSL 换算 / 暗夜反色 / hex→rgba）：实现在 r2-color.js（装配见上方 R2Color） */

  function styleRun(sp, r, bodyS) {
    sp.style.fontFamily = FONT[r.fam] || FONT.serif;
    sp.style.fontSize = Math.max(r.s * r.S, 1) + "px";
    sp.style.color = pageInk(r.c);
    if (r.b) sp.style.fontWeight = "700";
    if (r.i) sp.style.fontStyle = "italic";
    if (r.up) {
      // 上标只认「确实比正文小」的 run。PyMuPDF 的 flags bit0 是按**行首 span
      // 的基线**判定的：行内公式所在的行往往以公式下标/小字号字形开头，紧跟其后的
      // 整段正文会被误标成上标（latex_sample 实测 21 个 up 里 19 个是正文）。
      // 这类假上标字号与正文一致，若照旧乘 0.7 就会明显变小、还整体抬高。
      // TeX 的真上标会直接把字排小（约正文 0.7×），用字号当闸门即可分辨；
      // 代价是 Word 式「只抬升、不缩小」的上标不再抬升，但学术 PDF 里极少见。
      const isSup = bodyS > 0 && r.s <= bodyS * 0.9 + 1e-6;
      if (isSup) {
        sp.style.verticalAlign = "super";
        // 字号取「本身字号」与「正文×0.7」的较小值：前者不会被缩两次，
        // 后者能补出上标该有的视觉大小。
        const s = Math.min(r.s, bodyS * 0.7);
        sp.style.fontSize = Math.max(s * r.S, 1) + "px";
      }
    }
  }

  /* 高亮底色/荧光：日间是半透明底色，暗夜换成“荧光笔”观感 ——
     很淡的同色底 + 高亮度霓虹字 + 外发光（不用 box-shadow，那句子的笔记下划线要留着）。 */
  function hlPaint(sp, key) {
    const c = hlColor(key);
    if (!isDark()) {
      sp.style.background = hexToRgba(c.color, c.alpha);
      return;
    }
    const rgb = hex2rgb(c.color) || [255, 214, 74];
    const [h, s] = rgb2hsl(rgb[0], rgb[1], rgb[2]);
    sp.style.background = hexToRgba(c.color, .2);
    sp.style.color = hsl2hex(h, Math.min(1, s * 1.1 + .15), .76);
    sp.style.textShadow = "0 0 8px " + hexToRgba(c.color, .95) +
                          ", 0 0 2px " + hexToRgba(c.color, .9);
  }

  /* ---- 页面级公式框（.fbox）的高亮 ----
     本地解析方案把公式渲染成页面级覆盖层；选句/高亮时需要让公式框跟着变色。
     公式框与句子通过 `_copyRange`（解析侧下发的字符区间）对应。 */
  function paintFormulaHl(fb, key) {
    if (!fb) return;
    if (!key) {
      fb.style.background = ""; fb.style.color = ""; fb.style.textShadow = "";
      return;
    }
    const c = hlColor(key);
    const tint = hexToRgba(c.color, isDark() ? .2 : Math.min(0.6, c.alpha));
    // 底色**叠在页面底色上**：.fbox 必须仍然不透明，继续挡住框内尚未擦除的原字形
    fb.style.background = "linear-gradient(" + tint + "," + tint + "), var(--page-bg)";
    if (!isDark()) { fb.style.color = ""; fb.style.textShadow = ""; return; }
    // 暗夜：与正文 span(.rn.hl / hlPaint) 完全同一套荧光字 —— KaTeX 文本从 .fbox 继承 color
    const rgb = hex2rgb(c.color) || [255, 214, 74];
    const [h, s] = rgb2hsl(rgb[0], rgb[1], rgb[2]);
    fb.style.color = hsl2hex(h, Math.min(1, s * 1.1 + .15), .76);
    fb.style.textShadow = "0 0 8px " + hexToRgba(c.color, .95) +
                          ", 0 0 2px " + hexToRgba(c.color, .9);
  }
  function paintFormulaMarks() {
    for (const id in itemById) {
      const it = itemById[id];
      for (const box of it.formulaBoxes || []) {
        const rg = box._copyRange;
        if (!rg && !box._selectRef) continue;
        // 选句/高亮一律跟随**锚点句子**（`_selectRef`，见 attachFormulaBoxes）。
        // ⚠️ 不要再按 `_copyRange` 与句子区间相交判断：分离式公式（∑ 的上下限、
        // 分式分母）的占位常落在别的段落块里，按区间相交会把别的公式框一起染色。
        const anchorRef = box._selectRef;
        let color = null, selected = false;
        if (anchorRef) {
          const key = sentKey(anchorRef.item, anchorRef.si);
          const c = (anno.highlights || {})[key];
          if (c) color = c.color || "yellow";
          if (sel && sel.sents && sel.sents.some(r =>
              r.item === anchorRef.item && r.si === anchorRef.si)) selected = true;
        } else if (rg) {
          // 没有锚点（极旧数据 / 公式框缺 bbox）：退回 copy 区间相交的老口径
          for (const [key, c] of Object.entries(anno.highlights || {})) {
            const p = keyParts(key);
            if (p.id !== it.id || p.si < 0 || p.si >= it.sents.length) continue;
            const sr = sentRange(it, p.si);
            if (sr.a < rg.end && sr.b > rg.start) {
              color = (c && c.color) || "yellow";
              break;
            }
          }
          if (sel && sel.sents) {
            for (const ref of sel.sents) {
              if (ref.item !== it) continue;
              const sr = sentRange(it, ref.si);
              if (sr.a < rg.end && sr.b > rg.start) { selected = true; break; }
            }
          }
        }
        paintFormulaHl(box, color);
        box.classList.toggle("formula-sel",
          selected || !!(sel && sel.formulaBox === box));
      }
    }
  }

  /* 逐行横向压缩：实现在 r2-fit.js（装配见上方 R2Fit） */

  /* 视觉锚点 / 滚动位置还原：实现在 r2-view.js（装配见上方 R2View） */

  function render(preAnchor) {
    if (!DOC) return;              // 文档还没到（缺 ?id= / 数据未加载）：别让缩放之类把这里点到抛错
    // 首次进入还没按 last_pos 定位过时，**预先量到的锚点不可信**：正文刚建好、屏幕还停在
    // 0，量出来的是“页首”。KaTeX 加载完那次重排（init 里 render(pageAnchor())）就踩过这个坑
    // —— 它会把刚还原好的位置又拽回文首。所以定位过一次之后才认 preAnchor。
    const useLast = !posRestored && posValid(META && META.last_pos);
    const anchor = useLast ? null : (preAnchor || pageAnchor());   // 首次渲染（还没有页面）为 null → 走“上次读到哪一页”
    const keepSel = sel ? sel.sents.map(r => ({ id: r.item.id, si: r.si })) : null;  // 重排后保留选中
    // 重排(缩放/窗口变化/批注栏开关)后保留已展开的译文卡，别让用户重新点一遍
    const keepTrans = Object.keys(itemById).filter(id => !itemById[id].tsec.hidden);
    const t = computePageTarget();
    curPageW = t.pageW;            // 先记下，避免 relayoutAll() 重复触发 render
    applyNoteChrome(t);

    pagesEl.textContent = "";
    pages.length = 0;
    Object.keys(itemById).forEach(k => delete itemById[k]);
    ordSeq = 0;
    sel = null; hideBar();

    // 三条渲染分支：
    //   · surya / unlimited_ocr：块级数据（bbox + 文本/html），各自流式渲染；
    //   · 本地解析方案（parser=pymupdf）：逐行几何 + 页面级公式框，走 buildTextItem；
    //     旧 doc.json（parser=pymupdf）同样是这套结构，一并兼容。
    const surya = DOC.parser === "surya";
    const unlimited = DOC.parser === "unlimited_ocr";
    DOC.pages.forEach((pg, pi) => {
      const S = t.pageW / pg.w;
      const pageEl = el("div", "page");
      pageEl.style.width = pg.w * S + "px";
      pageEl.style.height = pg.h * S + "px";
      pageEl.appendChild(el("div", "pagenum", "Page " + (pi + 1)));
      // `formula_boxes` 必须留在渲染状态里：attachFormulaBoxes 会按它把 .fbox 与
      // 文字块的 copy 区间接上（选句高亮、复制还原 LaTeX、基线对齐都依赖这一步）。
      const pgObj = {
        el: pageEl, w: pg.w * S, h: pg.h * S, items: [],
        formula_boxes: pg.formula_boxes || [],
      };

      for (const im of pg.images || []) {
        const div = el("div", "img");
        Object.assign(div.style, {
          left: im.x * S + "px", top: im.y * S + "px",
          width: im.w * S + "px", height: im.h * S + "px",
        });
        const img = el("img");
        const imageUrl = "/api/doc/" + docId + "/img/" + im.file;
        img.src = im.cache_key ? imageUrl + "?v=" + encodeURIComponent(im.cache_key) : imageUrl;
        img.alt = im.class_name || "Figure";
        div.appendChild(img);
        pageEl.appendChild(div);
        pgObj.items.push({
          kind: "img", el: div, x: im.x * S, y: im.y * S,
          w: im.w * S, h: im.h * S,
          surya, syShift: surya ? 0 : undefined,
          unlimited, ulShift: unlimited ? 0 : undefined,
        });
      }

      for (const t of pg.texts || []) {
        const item = surya ? buildSuryaItem(t, S, pageEl, pi)
                   : unlimited ? buildUnlimitedItem(t, S, pageEl, pi)
                   : buildTextItem(t, S, pageEl, pi);
        if (item) pgObj.items.push(item);
      }

      // 本地解析方案：页面级公式框（KaTeX / 裁图兜底）
      buildFormulaBoxes(pg, pageEl, S, pi);

      pagesEl.appendChild(pageEl);
      pages.push(pgObj);
    });

    // 套用标注并重画
    for (const id in itemById) { const it = itemById[id]; setAnnoRanges(it); rebuildItem(it); }
    for (const page of pages) attachFormulaBoxes(page, page.el);   // 公式框 → 文字块段落/字符区间
    finalizeDisplayMath();     // 公式覆盖层已挂到文档上 → 量真实高度并回填块高
    finalizeInlineMath();      // 旧「行内公式覆盖层」接口（本地方案用页面级 .fbox）
    finalizeFormulaBoxes();    // 页面级公式框：按框尺寸等比缩放 KaTeX
    finalizeSuryaBlocks();     // surya 块：量流式内容的真实高度 → 回填 contentH / 卡片位置
    settleSuryaVertical();     // surya 块：算内容纵向避让量 syShift（relayoutPage 应用）
    finalizeUnlimitedBlocks(); // unlimited-ocr 块：同上的收尾（独立一套）
    settleUnlimitedVertical(); // unlimited-ocr 块：算内容纵向避让量 ulShift
    finalizeLocalBlocks();     // 本地解析方案：按基线摆行 + 压缩行宽 + 回填表格高度
    // 字体晚到会改变字体的 ascent/descent（行内基线）与 KaTeX 的缩放：就绪后再摆一次。
    // finalizeLocalBlocks 是幂等的（top 绝对值、公式先回中再量）。
    if (document.fonts && document.fonts.ready && !_fontsHook) {
      _fontsHook = true;
      document.fonts.ready.then(() => {
        if (!DOC) return;
        finalizeLocalBlocks();
        paintFormulaMarks();
      }).catch(() => {});
    }
    const paraMode = !selTransList.length;      // 有“所选句译文”时不展开整段译文(两者互斥)
    if (showZhOn && paraMode) openAllTrans();
    if (paraMode) {
      for (const id of keepTrans) { const it = findItem(id); if (it && zh[id]) openTrans(it); }
    }
    renderAllNotes();          // 笔记：默认贴页面右侧批注栏(窗口不够宽时回到句子下方)
    renderSelTrans();          // “所选句译文”卡片(可多张；重排后保留)
    if (keepSel) {
      const refs = keepSel.map(k => ({ item: findItem(k.id), si: k.si })).filter(r => r.item);
      if (refs.length) setSelection(refs, true);
    }
    relayoutAll();
    refreshZoomLabel();
    requestAnimationFrame(() => {
      if (anchor) {
        restoreAnchor(anchor);      // 重排：停在原来那处内容上，不跳页
      } else if (!restoreLastPos(false)) {
        // 没有精确位置（老论文）：退回“上次读到的最远一页”。
        // 连页码都没有（从没滚过）且已读完的，才在 init() 里落到文末 AI 面板。
        if (META && META.last_page) {
          // META.last_page 存的是“页码”(从 1 开始)，scrollToPage 收的是“下标”，
          // 这里必须减 1 并夹紧，否则读到最后一页时 pages[n] 为 undefined 会抛异常，
          // 连带后面的 updatePageInd() 也不执行(页码指示器一直停在 “-”)。
          scrollToPage(clamp(META.last_page - 1, 0, Math.max(0, pages.length - 1)), "instant");
        }
      }
      if (sel) placeBar(true);
      updatePageInd();
    });
  }

  /* ============ 公式渲染：KaTeX（代码已拆到 r2-math.js） ============
     这里只做「装配」：把跨模块要共享的东西交出去，再把模块接口接回本作用域。
       · docId / findItem / itemById 是稳定引用，装配时取一次即可；
       · DOC 必须用 getter：换论文时它会被整体替换，模块里要每次取最新值；
       · katexReady 是模块内部的 let（加载完才置 true），只能按函数取。
     原来的 KATEX_* 常量、_katexP / _katexReady、各类对齐与宽度缓存都随模块走了。 */
  const {
    ensureKatex, docNeedsMath, renderInlineMath, renderKnownLatex,
    inlineMathOf, _mathIdxAt, _rightAfterMath,
    finalizeInlineMath, _inlineMathRoom, finalizeDisplayMath, formulaBodyWidth, mathSizePx,
    katexRender, mathTexOf, mathBlockOf, formulaBaseSize, _katexBoxes,
    buildFormulaBoxes, finalizeFormulaBoxes, refitFormulaBoxes,
    katexReady,
  } = R2Math({
    docId, findItem, itemById, fitItemLines,
    get DOC() { return DOC; },
  });

  /* 引用跳转（PDF 内部链接 → 参考文献卡片，见 r2-refs.js）：
     数据在 init() 里随其它接口一起拉取（/api/doc/{id}/refs），这里只做装配。
     模块缺失（浏览器缓存了旧脚本）时给一组空实现，不让阅读器整体挂掉。 */
  const refsUi = (typeof R2Refs === "function")
    ? R2Refs({ root: pagesEl })
    : { set() {}, markAll() {}, markItem() {} };

  /* ============ 块级渲染器：已拆到 js/items/ ============
     · items/item-unlimited.js  Unlimited-OCR（块级流式排版，DOM 类名 ul-*）
     · items/item-surya.js      Surya 2（块级流式排版，DOM 类名 sy-*）
     · items/item-local.js      PyMuPDF 逐行几何 + 页面级公式框基线对齐
     ⚠️ 装配顺序有依赖：local 复用 unlimited 的表格白名单（ulCleanNode）和 surya 的
     单元格公式识别（suryaTableMath），所以先 ul → surya → local。
     三个模块都只负责“造 DOM + 量高/避让”；重建（高亮/选中/笔记）仍走本文件的
     rebuildItem。 */
  const { buildUnlimitedItem, ulInlineMath, ulLineBreaks, ulCleanNode,
          finalizeUnlimitedBlocks, settleUnlimitedVertical } = R2ItemUl({
    el, splitSentences, bodyRunOf, mathBlockOf, mathSizePx, katexReady, katexRender,
    _katexBoxes, itemById, nextOrd, pages,
  });
  const { buildSuryaItem, suryaInlineMath, suryaLineBreaks, suryaTableMath,
          finalizeSuryaBlocks, settleSuryaVertical } = R2ItemSurya({
    el, splitSentences, bodyRunOf, mathBlockOf, mathSizePx, katexReady, katexRender,
    renderInlineMath, _katexBoxes, itemById, nextOrd, pages,
  });
  const { buildTextItem, finalizeLocalBlocks } = R2ItemLocal({
    el, FONT, splitSentences, bodyRunOf, katexReady, renderInlineMath,
    fitItemLines, refitFormulaBoxes, itemById, nextOrd, ulCleanNode, suryaTableMath,
  });

  /* ============ 高亮调色盘 + 浮动工具条 + “所选句译文”卡：已拆到 js/select/bar.js ============
     这里只保留装配。`sel` / `anno` / `selTransList` 是跨模块共享、且会被**整体替换**的
     可变状态 → getter + setter；其余是稳定引用（函数声明会提升，装配时取一次即可）。
     ⚠️ 装配出的 hideBar / placeBar / updateBar 也是上面 R2Selection 用到的那三个
     （它那边是转发调用），所以装配顺序不能颠倒。 */
  const { hlColor, applyHlColors, renderHmenuSwatches, bindHlColorInputs,
          hideBar, placeBar, updateBar, isTransOpen, renderSelTrans,
          clearSelTrans, enterParaTransMode, selGroups, selFragKey } = R2Bar({
    $, docId, itemById, findItem, noteOf,
    selCount, selAnchor, selItems, selKeys, clearSelection, refreshSelectionVisual,
    sentKey, sentRange, normTrans, copySentenceText, copyRangeText,
    needWrite, savePrefs, renderInlineMath, renderKnownLatex, vpW, mathTexOf,
    // 下面几个在“翻译”/“布局”段装配（在本段之后）——此刻还在 TDZ，只能转发
    relayoutAll: (...a) => relayoutAll(...a),
    onTranslateClick: (...a) => onTranslateClick(...a),
    syncExp: (...a) => syncExp(...a),
    selTransMathLatex: (...a) => selTransMathLatex(...a),
    closeAllTrans: (...a) => closeAllTrans(...a), openNoteEditor: (...a) => openNoteEditor(...a),
    get sel() { return sel; },
    get anno() { return anno; }, set anno(v) { anno = v; },
    get selTransList() { return selTransList; },
    set selTransList(v) { selTransList = v; },
    zh, noZh,
  });

  async function loadPrefs() {
    try {
      const p = await api("/api/prefs");
      applyHlColors(p && p.hl_colors);
      if (p && typeof p.show_zh === "boolean") {
        showZhOn = p.show_zh;
        $("chkShowZh").checked = p.show_zh;
      }
    } catch (e) { /* 用默认值 */ }
    // 清掉旧版本的浏览器本地值，避免“看起来还在用本地颜色”的误会
    try { localStorage.removeItem(HL_STORE_OLD); localStorage.removeItem(ZH_STORE_OLD); } catch (e) { /* ignore */ }
  }

  function savePrefs(patch) {
    api("/api/prefs", { method: "POST", json: patch })
      .catch(err => showNotice("偏好设置没能保存到服务端：" + err.message, true));
  }

  /* ============ 标注标记(高亮/下划线/选中) ============ */
  function setAnnoRanges(item) {
    const ranges = [];
    // 高亮(句子级)：颜色从调色盘取(支持 3 个自定义色)，用 inline 底色渲染
    for (const [key, c] of Object.entries(anno.highlights || {})) {
      const p = keyParts(key);
      if (p.id !== item.id || p.si < 0 || p.si >= item.sents.length) continue;
      const rg = sentRange(item, p.si);
      ranges.push({ a: rg.a, b: rg.b, cls: ["hl"], hl: c && c.color });
    }
    // 笔记句子加下划线
    const noteKeys = new Set();
    for (const x of anno.notes || []) noteKeys.add(x.block_id);
    for (const key of noteKeys) {
      const p = keyParts(key);
      if (p.id !== item.id || p.si < 0 || p.si >= item.sents.length) continue;
      const rg = sentRange(item, p.si);
      ranges.push({ a: rg.a, b: rg.b, cls: ["hasnote"] });
    }
    item.markerRanges = ranges;
  }

  /* 块内“主字号/主样式”：取该块里字符数最多的那个 run。
     用途：修正被 PyMuPDF 标错大小的正文片段——跨公式的 run 会整条带上标/下标字号
     （例：`t,n) is the value vector of formu-` 整条是 7pt 斜体），切走公式后
     剩下的正文就会明显变小。 */
  function bodyRunOf(linesData) {
    const cnt = new Map();
    for (const ld of linesData)
      for (const r of ld.runs) {
        const k = r.s + "|" + r.fam + "|" + (r.i ? 1 : 0) + "|" + (r.b ? 1 : 0);
        cnt.set(k, (cnt.get(k) || 0) + Math.max(1, (r.t || "").length));
      }
    let best = null, n = 0;
    for (const [k, v] of cnt) if (v > n) { n = v; best = k; }
    if (!best) return null;
    const p = best.split("|");
    return { s: parseFloat(p[0]), fam: p[1], i: p[2] === "1", b: p[3] === "1", up: false };
  }

  function rebuildItem(item) {
    if (item.kind !== "blk") return;
    const marks = item.markerRanges || [];
    const selRs = item.selRanges || [];
    const bodyS = item.bodyRun ? item.bodyRun.s : 0;   // 块内主字号，上标/小字修正都按它算
    for (const ld of item.linesData) {
      const elEl = ld.el;
      elEl.textContent = "";
      for (const run of ld.runs) {
        const gs = run.g, ge = run.g + run.t.length;
        const cuts = new Set([gs, ge]);
        const hit = [];
        for (const m of marks) {
          const a = Math.max(gs, m.a), b = Math.min(ge, m.b);
          if (a < b) { cuts.add(a); cuts.add(b); hit.push({ a, b, cls: m.cls, hl: m.hl }); }
        }
        for (const sr of selRs) {
          const a = Math.max(gs, sr.a), b = Math.min(ge, sr.b);
          if (a < b) { cuts.add(a); cuts.add(b); hit.push({ a, b, cls: ["sel"] }); }
        }
        // 行内公式的边界也切成独立 span，才能单独隐藏/测量
        for (const im of (item.inlineMath || [])) {
          const a = Math.max(gs, im.a), b = Math.min(ge, im.b);
          if (a < b) { cuts.add(a); cuts.add(b); }
        }
        const pts = [...cuts].sort((x, y) => x - y);
        for (let k = 0; k < pts.length - 1; k++) {
          const x = pts[k], y = pts[k + 1];
          if (y <= x) continue;
          const cls = new Set();
          let hl = null;
          for (const m of hit) {
            if (m.a <= x && y <= m.b) { m.cls.forEach(c => cls.add(c)); if (m.hl) hl = m.hl; }
          }
          const sp = el("span", "rn" + (cls.size ? " " + [...cls].join(" ") : ""));
          sp.dataset.g = x;
          styleRun(sp, run, bodyS);
          if (hl) { hlPaint(sp, hl); sp.dataset.hl = hl; }   // 公式框高亮按 data-hl 取同一色
          const piece = run.t.slice(x - gs, y - gs);
          if (hl) { hlPaint(sp, hl); sp.dataset.hl = hl; }   // 公式框高亮按 data-hl 取同一色
          // 公式占位：解析侧给的是“一个空格 + gw(pt)”，这里撑成原公式的宽度；
          // 字符透明化（KaTeX 覆盖层由 .fbox 绘制），但 span 仍参与选句。
          if (run.gap != null) {
            sp.classList.add("rngap");
            sp.dataset.box = String(run.gap);      // 公式框 id：item-local.js 的 _anchorFormulaX 按它找占位
            sp.style.display = "inline-block";
            sp.style.width = Math.max(1, (run.gw || 0) * (run.S || 1)) + "px";
            sp.style.color = "transparent";
          } else if (run.sp != null) {
            // 解析侧合并视觉行后补的“空隙”：按 PDF x 差撑宽，保证后面的公式/文字
            // 落在原位置（替代字体字宽不同，靠自然排版会逐步漂移）。
            sp.classList.add("rnsp");
            sp.style.display = "inline-block";
            sp.style.width = Math.max(0.5, (run.sp || 0) * (run.S || 1)) + "px";
            sp.style.color = "transparent";
          }
          const mi = _mathIdxAt(item.inlineMath, x, y);
          if (mi >= 0) {
            sp.classList.add("mspan");
            sp.dataset.mi = mi;
            // 只隐“已经真渲染出来”的那几条（item.mathDone），否则渲染失败的那条
            // 会连原字形一起藏掉 → 公式直接消失。
            if (item.mathDone && item.mathDone.has(mi)) sp.classList.add("mspan-hidden");
          }
          sp.textContent = piece;
          elEl.appendChild(sp);
        }
      }
    }
    renderInlineMath(item.el);          // 行内数学：\(…\) / $$…$$ 就地换成 KaTeX
    if (item.surya) {
      suryaInlineMath(item);            // Surya 块：.mspan 原字形 → 就地 KaTeX
      suryaLineBreaks(item);            // Surya 块：canonical 里的 '\n' → <br>（列表/表格行）
    } else if (item.unlimited) {
      ulInlineMath(item);               // Unlimited-OCR 块：.mspan 原字形 → 就地 KaTeX
      ulLineBreaks(item);               // Unlimited-OCR 块：canonical 里的 '\n' → <br>
    }
    refsUi.markItem(item);     // 引用跳转：按块 id + 字符区间重打 .cite 标记（幂等）
  }

  let relayoutTimer = null;
  window.addEventListener("resize", () => {
    if (sel) placeBar(true);
    // 视口宽度决定笔记卡是“贴右侧”还是“回到句子下方”，需要重新排版
    clearTimeout(relayoutTimer);
    relayoutTimer = setTimeout(relayoutAll, 150);
  });
  window.addEventListener("scroll", () => {
    posRestored = true;      // 位置已经落定（用户自己滚 / 我们刚还原过），后续重排按当前视口量锚点
    if (sel) placeBar(true); updatePageInd(); scheduleSave();
  }, { passive: true });

  /* ================= 翻译(段落级) + 译文卡：已拆到 js/select/translate.js =================
     `showZhOn` / `selTransList` 会被整体替换 → getter + setter；`zh` / `noZh` 是同一份容器
     （只增删，不替换）。`busy` / `cancelAll`（“翻译全文”进行中/取消）只在本模块用，已随
     模块内部持有。 */
  const { onTranslateClick, toggleTransOpen, translateItems, fillTrans, itemMathLatex,
          selTransMathLatex, openTrans, openAllTrans, closeAllTrans, syncExp,
          runAllTrans } = R2Trans({
    $, docId, itemById, pages, findItem, itemCopyText, normTrans,
    renderInlineMath, renderKnownLatex, loadSelTrans, savePrefs,
    updateBar, placeBar, renderSelTrans, enterParaTransMode, currentPageIndex: (...a) => currentPageIndex(...a),
    relayoutAll: (...a) => relayoutAll(...a),
    zh, noZh,
    get showZhOn() { return showZhOn; }, set showZhOn(v) { showZhOn = v; },
    get selTransList() { return selTransList; }, set selTransList(v) { selTransList = v; },
    get sel() { return sel; },
  });

  /* ================= 布局：已拆到 js/layout.js =================
     本模块是“枢纽”（笔记 / 目录 / AI 都会反向调用 relayoutAll、relayoutPage），所以
     放在最后装配：页面宽度、批注栏、卡片顶开、页面高度都由它算。
     · curPageW / noteSide / noteW / noteLeft 本段既读又写 → getter + setter；
     · notesCount / notesOfBlock / tocPanelOpen 来自更后面的 r2-notes、r2-nav 装配 →
       包一层转发；TOC_W 是常量，用 getter 延后取值。 */
  const { computePageTarget, applyNoteChrome, relayoutAll, relayoutPage } = R2Layout({
    $, CSS_W, ZOOM, ZOOM_OK, vpW, NOTE_GAP, pages,
    keyParts, lineOfChar, sentRange, render, paintFormulaMarks,
    get DOC() { return DOC; },
    get curPageW() { return curPageW; }, set curPageW(v) { curPageW = v; },
    get noteSide() { return noteSide; }, set noteSide(v) { noteSide = v; },
    get noteW() { return noteW; }, set noteW(v) { noteW = v; },
    get noteLeft() { return noteLeft; }, set noteLeft(v) { noteLeft = v; },
    get editingKey() { return editingKey; },
    get showNotesOn() { return showNotesOn; },
    notesCount: (...a) => notesCount(...a),
    notesOfBlock: (...a) => notesOfBlock(...a),
    tocPanelOpen: (...a) => tocPanelOpen(...a),
    get TOC_W() { return TOC_W; },
  });

  /* ================= 句子笔记 / 全部笔记抽屉：已拆到 r2-notes.js =================
     这里只做装配。$ / itemById / inkCache / docId 等 const、以及 26 个函数声明
     装配时取一次即可；下面这些要特殊处理：
     · anno / editingKey / noteDraft / showNotesOn —— 段内既读又写（anno 甚至会整体
       重新赋值），所以 getter + setter 都要给；
     · sel / DOC —— 段内只读，给 getter；
     · canWrite / renderTocAi 声明在**本段之后**（共享权限段 / AI 段装配处），
       装配这一刻还在 TDZ，所以包一层转发，等真正调用时再去读。 */
  const { notesOfBlock, notesCount, renderAllNotes, openNoteEditor, refreshSideList } = R2Notes({
    $, itemById, docId, toScrollDelta, inkCache,
    keyParts, relayoutAll, placeBar, updateBar, relayoutPage, needWrite, findItem,
    sentKey, sentenceText, noteOf, selCount, selAnchor, isSelRef, pageOfItem,
    render, renderHmenuSwatches, setAnnoRanges, rebuildItem, selectSentence, syncExp,
    canWrite: (...a) => canWrite(...a),
    renderTocAi: (...a) => renderTocAi(...a),
    get anno() { return anno; }, set anno(v) { anno = v; },
    get editingKey() { return editingKey; }, set editingKey(v) { editingKey = v; },
    get noteDraft() { return noteDraft; }, set noteDraft(v) { noteDraft = v; },
    get showNotesOn() { return showNotesOn; }, set showNotesOn(v) { showNotesOn = v; },
    get sel() { return sel; },
    get DOC() { return DOC; },
    get myName() { return myName; },      // 共享论文里把“我写的笔记”标出来
  });

  /* ================= 滚动/页码/目录/进度/缩放：已拆到 r2-nav.js =================
     这里只做装配。稳定引用（$ / pages / docId / 各坐标换算 / ZOOM / 渲染函数）
     装配时取一次即可；下面这几个要特殊处理：
     · META 用 getter —— 换论文会整体替换，而且段内要改它的属性（META.progress = …）；
     · zoomIdx 用 getter + setter —— 段内会赋值（换缩放档位）；
     · canWrite / maybeAutoSummarize 声明在**本段之后**（共享权限段 / AI 段装配处），
       装配这一刻还在 TDZ，所以包一层转发，等真正调用时再去读；
     · saveTimer 已随模块搬走（外部没有引用），原来那行 `let saveTimer = null;` 已删。 */
  const { currentPageIndex, updatePageInd, scrollToPage, TOC_W, tocPanelOpen,
          scheduleSave, updateFinishBtn, refreshZoomLabel } = R2Nav({
    $, pages, docId, CSS_W, vpH, ZOOMS, pagesEl, ZOOM, GZ, toScrollDelta,
    relayoutAll, applyZoom, pageAnchor, render,
    canWrite: (...a) => canWrite(...a),
    maybeAutoSummarize: (...a) => maybeAutoSummarize(...a),
    get META() { return META; },
    get zoomIdx() { return zoomIdx; },
    set zoomIdx(v) { zoomIdx = v; },
  });

  /* ================= 文末 · AI 辅助阅读：已拆到 r2-ai.js =================
     这里只做装配：把跨模块共享的东西交出去，再把模块接口接回本作用域。
     · $ / docId / findItem / keyParts / pageOfItem / scrolled / toScrollTop / needWrite /
       anchorProbe 都是稳定引用（函数声明会提升），装配时取一次即可；
     · META / anno 用 getter —— 换论文 / 重拉批注时会整体替换，模块里要每次取最新值；
     · canWrite 声明在**本段之后**（共享权限那一段），装配这一刻还在 TDZ，
       所以包一层转发，等真正调用时再去读它。
     · renderInlineMath / katexReady / ensureKatex 来自 r2-math（本段之前已装配）——
       AI 气泡与整理条目按 Markdown 渲染（r2-md.js）后，里面的 $…$ / \(…\) 数学
       交给它们就地换成 KaTeX（没加载过则懒加载）。
     原来那段里 AI 自己的状态（aiSummary / aiChat / aiBusyWhat / aiEdit / …）
     随模块一起搬走了，不再占用本文件的作用域。 */
  const { maybeAutoSummarize, syncAiButtons, jumpToAi, renderTocAi, loadAi } = R2Ai({
    $, docId, findItem, keyParts, pageOfItem, scrolled, toScrollTop, needWrite, anchorProbe,
    canWrite: (...a) => canWrite(...a),
    renderInlineMath, katexReady, ensureKatex,
    get META() { return META; },
    get anno() { return anno; },
  });

  /* ================= 初始化 ================= */
  bindHlColorInputs();     // 先绑定取色器（不依赖数据），颜色稍后按账号加载
  renderHmenuSwatches();   // 先用默认色渲染 6 个色块

  /* ================= 共享权限 =================
     `owner` / `write` / `read` 由后端在 /progress 里给出。
     「只读」只是把写入口藏起来，服务端每个写接口也会再抦一次（403）。 */
  let perm = "owner";
  let docOwner = "";
  let myName = "";         // 当前登录账号：共享论文里用来把“我写的笔记”区分出来
  const canWrite = () => perm === "owner" || perm === "write";

  function shareNoteText() {
    return "这篇论文是 " + (docOwner || "别人") + " 共享给你的（"
      + (perm === "write" ? "可写" : "只读") + "）：" + (perm === "write"
        ? "你可以和对方一起改笔记、高亮与 AI 整理。"
        : "你可以看笔记/高亮/译文、自己翻译、记自己的阅读进度，但不能改笔记。");
  }

  function applyPerm() {
    const badge = $("shareBadge");
    if (badge) {
      if (perm === "owner") {
        badge.className = "share-badge";
        const n = (META && META.shared_count) || 0;
        badge.hidden = !n;
        badge.textContent = n ? "已共享给 " + n + " 人" : "";
      } else {
        badge.hidden = false;
        badge.className = "share-badge" + (perm === "write" ? " write" : "");
        badge.textContent = "来自 " + (docOwner || "别人") + " · "
          + (perm === "write" ? "可写共享" : "只读共享");
      }
    }
    const btn = $("btnShare");
    if (btn) btn.hidden = perm !== "owner";
    markTitleEditable();        // 只读共享时标题也不可点改
    syncAiButtons();            // 只读时把 AI 那边的按钮也置灰
  }

  function needWrite(tip) {
    if (canWrite()) return true;
    toast(tip || "这是只读共享的论文，不能修改（可让作者把权限改成「可写」）", 3600);
    return false;
  }

  /* ================= 论文标题（点一下就能改） =================
     标题在服务端存了两份（记录 + 版式文档），改名接口一次写全，所以首页列表
     刷新后就是新名字。这里只做「就地编辑」：Enter 保存 / Esc 取消 / 失焦保存。 */
  function setupDocTitle() {
    const h = $("docTitle");
    if (!h || h._titleEdit) return;
    h._titleEdit = true;
    h.addEventListener("click", () => editDocTitle());
    h.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); editDocTitle(); }
    });
  }
  /* 有写权限才把标题标成“可点”的样子（只读者连光标/提示都不给） */
  function markTitleEditable() {
    const h = $("docTitle");
    if (!h) return;
    const ok = canWrite();
    h.classList.toggle("editable", ok);
    h.title = ok ? "点击重命名（Enter 保存 / Esc 取消）" : "";
    h.tabIndex = ok ? 0 : -1;
  }
  function editDocTitle() {
    const h = $("docTitle");
    if (!h || h._editing) return;
    if (!needWrite("这是只读共享的论文，不能改名（可让作者把权限改成「可写」）")) return;
    const cur = (DOC && DOC.title) || h.textContent || "";
    h._editing = true;
    h.textContent = "";
    const inp = document.createElement("input");
    inp.type = "text";
    inp.className = "doc-title-edit";
    inp.value = cur;
    inp.maxLength = 200;
    h.appendChild(inp);
    inp.focus();
    inp.select();
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      h._editing = false;
      const val = String(inp.value || "").replace(/\s+/g, " ").trim();
      h.textContent = cur;                       // 先恢复原名，失败时不用再回滚
      if (!save || !val || val === cur) { markTitleEditable(); return; }
      h.textContent = val;                       // 乐观更新，失败再退回
      try {
        await api("/api/doc/" + docId + "/title", { method: "POST", json: { title: val } });
        if (DOC) DOC.title = val;
        document.title = val;
        toast("已重命名");
      } catch (err) {
        h.textContent = cur;
        showNotice("重命名失败：" + err.message, true);
      }
      markTitleEditable();
    };
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); finish(false); }
      e.stopPropagation();                       // 别让页面级快捷键（缩放手感）抢键
    });
    inp.addEventListener("click", (e) => e.stopPropagation());
    inp.addEventListener("blur", () => finish(true));
  }

  async function openShare() {
    if (!needWrite("只有论文所有者能管理共享")) return;
    openShareModal(docId, DOC.title || "", async () => {
      try {
        META = await api("/api/doc/" + docId + "/progress");
        applyPerm();
      } catch (e) { /* ignore */ }
    });
  }
  $("btnShare").addEventListener("click", openShare);

  /* 用原始 PDF 原地重解析版式：解析器升级（换 OCR 后端 / 补公式 / 调 DPI）后，
     库里存的还是旧版式，这里重新解析一遍 —— 笔记/高亮/译文/进度都不动。 */
  const btnReparse = $("btnReparse");
  const reparseFile = $("reparseFile");
  if (btnReparse && reparseFile) {
    btnReparse.addEventListener("click", () => {
      if (!needWrite("只有论文所有者能重新解析版式")) return;
      reparseFile.value = "";
      reparseFile.click();
    });
    reparseFile.addEventListener("change", async () => {
      const f = reparseFile.files && reparseFile.files[0];
      if (!f) return;
      const old = btnReparse.textContent;
      btnReparse.disabled = true;
      btnReparse.textContent = "解析中…";
      showNotice("正在用「" + f.name + "」重新解析版式，请稍候（页数多时需要一会儿）…", false);
      try {
        const fd = new FormData();
        fd.append("file", f);
        const r = await api("/api/doc/" + docId + "/reparse", { method: "POST", body: fd });
        const info = r.parser_info || {};
        const lb = r.latex_blocks || [0, 0];
        toast("版式已重新解析（" + parserLabel(r.parser) + "）");
        showNotice("重解析完成：后端 " + parserLabel(r.parser) + "，页数 " + r.pages[0]
          + "→" + r.pages[1] + "，文字块改动 " + r.changed + " 处"
          // 公式：本流程把公式字形擦成等长空格（页面上那段地方就是空白）。
          // `formula_chars` = 被擦掉的字形数，`formula_spans` = 擦除的 run 段数。
          + (info.formula_chars
            ? ("，擦除公式字形 " + info.formula_chars + " 字 / "
               + (info.formula_spans || 0) + " 段")
            : "")
          + (lb[1] ? ("，带 latex 的块 " + lb[0] + "→" + lb[1]) : "")
          + "；笔记与译文都保留。正在刷新…", false);
        setTimeout(() => location.reload(), 1200);
        setTimeout(() => location.reload(), 1200);
      } catch (err) {
        showNotice("重新解析失败：" + err.message, true);
      } finally {
        btnReparse.disabled = false;
        btnReparse.textContent = old;
      }
    });
  }

  /* “所选句译文”卡存在服务端（按目标语言分开存）：拉下来映射成前端结构 */
  async function loadSelTrans() {
    const cards = await api("/api/doc/" + docId + "/seltrans").catch(() => []);
    return (cards || []).map(c => ({
      key: c.key, itemId: c.item_id || String(c.key || "").split("#")[0],
      siFrom: c.si_from || 0, text: c.text || "",
    })).filter(c => c.key && c.text);
  }

  async function init() {
    if (!docId) { showNotice("缺少论文 id", true); return; }
    await loadPrefs();             // 自定义高亮色 / 显示译文开关：随账号存在服务端
    renderHmenuSwatches();
    try {
      let refsData = null;
      [META, DOC, anno, refsData] = await Promise.all([
        api("/api/doc/" + docId + "/progress"),
        api("/api/doc/" + docId),
        api("/api/doc/" + docId + "/anno"),
        // 引用跳转数据（PDF 内部链接 → 参考文献条目）：旧数据/无链接时为空映射，静默
        api("/api/doc/" + docId + "/refs").catch(() => null),
      ]);
      refsUi.set(refsData);
      perm = META.perm || "owner";
      docOwner = META.owner || "";
      myName = ((await authStatus()) || {}).username || "";   // 笔记作者标识用
      const zhMap = await api("/api/doc/" + docId + "/zh_all").catch(() => ({}));
      Object.assign(zh, zhMap);
      // “所选句译文”卡是存在服务端的：重新登录/刷新后原样恢复（片段译文不在 zh 里，靠它回来）
      selTransList = await loadSelTrans();
      // 两种译文互斥：恢复出来的“所选句译文”会占坑，就把“显示译文”勾去掉，
      // 让勾选状态和实际显示一致（服务端存的偏好不动，清掉卡片后再勾就行）
      if (selTransList.length) { $("chkShowZh").checked = false; showZhOn = false; }
      $("docTitle").textContent = DOC.title;
      document.title = DOC.title;
      setupDocTitle();              // 顶栏标题：点一下就地改名（同步回首页列表）
      updateFinishBtn();
      $("notesCount").textContent = (anno.notes || []).length;
      const cfg = await appConfig();
      applyPerm();
      const tips = [];
      if (perm !== "owner") tips.push(shareNoteText());
      if (!cfg.ready) {
        tips.push("⚠️ 未配置翻译服务：译文功能不可用。点右上角「⚙ 设置 → LLM 服务」填写服务地址与 API Key（也可在 config.json 里配），或安装 deep-translator 用免费兜底。");
      }
      // 解析后端就绪状态：本地解析方案/两条整页 OCR 链都是“全有或全无”，
      // 不满足条件时上传与重解析会失败，这里提前提示。
      const pr = cfg && cfg.parser;
      const localBackends = ["pymupdf", "auto", "detection_service_group"];
      if (pr && pr.backend === "surya" && pr.surya_ready !== true) {
        tips.push("⚠️ 解析后端现为 surya，但 Surya 2 还未就绪：" + (pr.surya_message || "") +
          "（上传 / 重解析会失败；点右上角「⚙ 设置 → 存储 / OCR」查看）");
      } else if (pr && pr.backend === "unlimited_ocr" && pr.unlimited_ocr_ready !== true) {
        tips.push("⚠️ 解析后端现为 unlimited_ocr，但 Unlimited-OCR 服务还未就绪：" +
          (pr.unlimited_ocr_message || "") +
          "（上传 / 重解析会失败；点右上角「⚙ 设置 → 存储 / OCR」查看）");
      } else if (pr && localBackends.indexOf(pr.backend) >= 0 && pr.local_ready !== true) {
        tips.push("⚠️ 解析后端现为「本地解析方案」，但本机还不具备条件：" +
          (pr.local_message || "缺少本地模型或依赖") +
          "（上传 / 重解析会失败；点右上角「⚙ 设置 → 存储 / OCR」查看）");
      }
      // 旧解析数据没有链接注释时，编号引用已尝试回退匹配；仍无映射才提示可重解析。
      if (refsData && refsData.scan === "no-links-data"
          && refsData.section_page !== null
          && !(refsData.links || []).length
          && ((refsData.counts || {}).entries || 0) > 0) {
        tips.push("📎 这篇论文的版式数据还没有采集引用链接（旧版数据）：在顶栏点「🔄 重解析」选择同一份 PDF 后，可尝试启用正文引用跳转。");
      }
      showNotice(tips.join("　"), !!tips.length);
      // 公式用 KaTeX 渲染：脚本懒加载，先画一次(公式先按 PDF 原文字显示)，
      // 加载完再按当前阅读位置重画一遍把公式换成真渲染；加载失败就保持原文字。
      if (docNeedsMath(DOC) && !katexReady()) {
        ensureKatex().then(ok => { if (ok) render(pageAnchor()); });
      }
      render();
      // 笔记已内嵌在正文里（render 时自动展开），这里只把“全部笔记”总览的口径刷新一下，
      // 不再自动弹右侧面板——面板只在你主动点顶栏「📝 笔记」时打开。
      refreshSideList();
      // 文末的 AI 面板：先拿回上次的整理/对话；若之前已读到 95% 且还没整理，这里补一次
      await loadAi();
      maybeAutoSummarize();
      // 回到上次离开时的位置：`META.last_pos` 记的是精确位置（页内比例 / 距文末多远），
      // 所以“读完的文章”不再一律甩到文末 AI 面板 —— 你停在哪儿就回哪儿。
      // 文末面板的高度只有 loadAi() 之后才定型，故这里只补处理“停在 AI 面板”那种；
      // 没有 last_pos 的老论文按 render() 里还原的“最远一页”即可，
      // 只有在既没位置数据、也从没滑过页面时，读完的才落到文末面板。
      if (!restoreLastPos(true) && !META.last_page && META.status === "done") jumpToAi(true);
    } catch (err) {
      showNotice("加载失败：" + err.message, true);
    }
  }
  applyZoom();      // 首屏就把整体缩放套上（默认 100%，等价于不变）
  init();
})();
