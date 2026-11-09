/* ============ 本地解析方案（PyMuPDF + local_parse_pipeline）的逐行渲染：从 reader2.js
   拆出来的模块 ============
   解析侧下发的是**逐行几何**：每个文字块 `t.lines[]` 都有自己的 bbox，行内
   `runs[]` 带字号 / 字体类别 / 粗斜体 / 颜色；公式字形被替换成一个占位空格
   （`run.gap` + `run.gw`），真公式由页面级 `.fbox` 覆盖层渲染。
   所以这里按 PDF 行框绝对定位每一行，行内文字正常排；行宽由 fitLineEl 压缩适配。
   表格块（`kind="table"`）渲染白名单 HTML，不参与句子选中。

   装配（reader2.js，R2Math 之后；依赖 unlimited / surya 两个渲染模块的表格工具）：
     const { buildTextItem, finalizeLocalBlocks } = R2ItemLocal({ … });
     · `ulCleanNode`   —— item-unlimited.js：表格 HTML 白名单（本地方案直接复用）
     · `suryaTableMath`—— item-surya.js：表格单元格里的纯文本 LaTeX → KaTeX
     · `fitItemLines`  —— r2-fit.js：按行框宽度横向压缩
     · `refitFormulaBoxes` —— r2-math.js：公式框宽度变了要重新等比缩放 KaTeX
   ============================================================================ */
window.R2ItemLocal = function (env) {
  "use strict";
  const { el, FONT, splitSentences, bodyRunOf, katexReady, renderInlineMath,
          fitItemLines, refitFormulaBoxes, itemById, nextOrd,
          ulCleanNode, suryaTableMath } = env;

  /* 表格：白名单渲染（复用 unlimited-ocr 的清洗器），单元格里的公式交给 surya 那套识别 */
  function localTableEl(html, sizePx) {
    const s = String(html || "");
    if (!s || s.indexOf("<table") < 0) return null;
    let doc = null;
    try { doc = new DOMParser().parseFromString(s, "text/html"); }
    catch (e) { return null; }
    const src = doc && doc.body && doc.body.querySelector("table");
    if (!src) return null;
    const wrap = el("div", "lctable");
    wrap.appendChild(ulCleanNode(src));       // 复用 unlimited-ocr 的表格白名单
    if (katexReady()) suryaTableMath(wrap, sizePx || 0);
    renderInlineMath(wrap);
    return wrap;
  }

  function buildTextItem(t, S, pageEl, pi) {
    const bx = t.x * S, by = t.y * S;
    const blk = el("div", "blk lc");
    if (t.kind) blk.classList.add("lc-" + t.kind);
    blk.dataset.id = t.id;
    const baseSize = (t.size > 0 ? t.size : 10) * S;
    Object.assign(blk.style, {
      left: bx + "px", top: by + "px",
      width: t.w * S + "px", height: t.h * S + "px",
      fontSize: baseSize + "px",          // 表格单元格/未带字号的内容继承它
    });

    const linesData = [];
    const contentH = t.h * S;
    if (t.kind === "table") {
      const wrap = localTableEl(t.html, baseSize);
      blk.appendChild(wrap || el("div", "ln lcln", t.text || ""));
    } else {
      for (const ln of t.lines || []) {
        const lineEl = el("div", "ln lcln");
        /* 旋转（竖排/倒排）行：`x/y/w/h` 是**旋转后**的轴对齐外框（很窄很高），真正的
           行盒是 `rw × rh`、未旋转时的左上角在 (ox, oy)，再整体 rotate(rot)。
           不这样摆的话，`.ln` 宽度只有字高（实测 1.5px）、文字横着溢出压住正文。
           见解析侧 `pdf_parser/text_layer.py::_line_rot`。 */
        const rot = Number(ln.rot) || 0;
        const lwPt = rot ? (Number(ln.rw) || 1) : ln.w;
        const lhPt = rot ? (Number(ln.rh) || 1) : ln.h;
        const lxPt = rot ? (Number(ln.ox) - t.x) : (ln.x - t.x);
        const lyPt = rot ? (Number(ln.oy) - t.y) : (ln.y - t.y);
        Object.assign(lineEl.style, {
          left: Math.max(0, lxPt * S) + "px",
          top: Math.max(0, lyPt * S) + "px",
          width: Math.max(1, lwPt * S) + "px",
          height: Math.max(1, lhPt * S) + "px",
          // 行高固定 1：行内 span 自带字号，按行框高度撑 line-height 会把基线压偏
          // （带行内公式的行，PyMuPDF 的 bbox 高度远大于正文字高）。
          lineHeight: "1",
        });
        if (rot) {
          lineEl.style.transformOrigin = "0 0";
          lineEl.style.transform = "rotate(" + rot + "deg)";
        }
        blk.appendChild(lineEl);
        const runs = (ln.runs || []).map(r => {
          const run = Object.assign({
            s: 10, fam: "serif", b: false, i: false, up: false, c: "#000000",
          }, r);
          run.t = String(run.t == null ? "" : run.t);
          run.S = S;
          run.g = 0;
          return run;
        });
        const ld = {
          ln, dy: 0, free: false, gaps: [], runs,
          el: lineEl, text: "", gs: 0, S,
          base: Number(ln.base) || 0,      // PDF 基线（pt）：后端按视觉行合并后一定有
          rot,                             // 旋转角（0 = 普通横排）
          rwPx: Math.max(1, lwPt * S),     // 旋转行的「行宽」= 未旋转时的宽度（像素）
        };
        ld.text = runs.map(r => r.t).join("");
        lineEl._ld = ld;                  // 公式按几何找行时用（见 _alignPageFormulas）
        linesData.push(ld);
      }
    }
    blk.style.height = contentH + "px";

    // canonical：行文本原样拼接，行末一个 '\n'（与其它渲染分支同口径）
    let g = 0;
    for (const ld of linesData) {
      ld.gs = g;
      for (const r of ld.runs) { r.g = g; g += r.t.length; }
      g += 1;
    }
    const item = {
      kind: "blk", id: t.id, pi, el: blk, text: t.text || "",
      x: bx, y: by, w: t.w * S, h: t.h * S, contentH,
      latex: "", linesData, canonical: "", sents: [],
      markerRanges: [], selRanges: [], shift: 0, expH: 0,
      ord: nextOrd(), local: true, table: t.kind === "table",
      baseSize,
      formulaBoxes: [],
    };
    item.canonical = linesData.map(ld => ld.text + "\n").join("");
    item.bodyRun = bodyRunOf(linesData)
      || { s: 10, fam: "serif", i: false, b: false, up: false };
    item.sents = t.kind === "table" ? [] : splitSentences(item.canonical).map((sg, si) => ({
      si, s: sg.s, e: sg.e, key: item.id + "#" + si,
      text: item.canonical.slice(sg.s, sg.e).replace(/\n+/g, " ").trim(),
    }));
    if (t.kind === "table") item.noSelect = true;   // 表格没有可切句的 .rn 结构

    // 译文 + 内嵌笔记展开区（与其它渲染分支同一套约定）
    const exp = el("div", "exp");
    exp.style.top = contentH + "px";
    const tsec = el("div", "card-in tsec");
    tsec.hidden = true;
    exp.appendChild(tsec);
    const stbox = el("div", "stbox");
    stbox.hidden = true;
    exp.appendChild(stbox);
    const nbox = el("div", "nbox");
    nbox.hidden = true;
    nbox.addEventListener("click", e => e.stopPropagation());
    stbox.addEventListener("click", e => e.stopPropagation());
    exp.appendChild(nbox);
    blk.appendChild(exp);
    item.exp = exp; item.tsec = tsec; item.stbox = stbox; item.nbox = nbox;

    pageEl.appendChild(blk);
    itemById[t.id] = item;
    return item;
  }

  /* ============ 本地解析方案的基线对齐 ============
     `.ln` 原来按 PDF 行框**顶边**绝对定位，但行框顶部会被行内公式的大字形（∑、分式、
     上下标）抬得很高：正文在行框里按 CSS 基线排，就会比 PDF 的实际基线高出一大截，
     于是同一排的文字和公式上下错位，相邻两行也会互相压字（latex_sample 第 2 页实测
     差 5~9px）。这里统一改成**按基线定位**：
       · 新数据：后端 `lines[].base` / `formula_boxes[].base` 直接给 span origin（pt）；
       · 旧 doc.json（没有 base）：该行挂了行内公式就用公式渲染后的基线当行基线，
         否则用「行框底边 − 字体下伸」估算（实测中位误差 0.16pt）；
       · 行内公式（.fbox）再按同一条基线上下微调，保证文字与公式共享基线。 */
  let _metricCtx = null;
  function _fontDescentPx(px, fam) {
    if (!(px > 0)) return 0;
    try {
      if (!_metricCtx) _metricCtx = document.createElement("canvas").getContext("2d");
      _metricCtx.font = px + "px " + (FONT[fam] || FONT.serif);
      const d = Number(_metricCtx.measureText("Hg").fontBoundingBoxDescent);
      if (isFinite(d) && d > 0) return d;
    } catch (e) { /* 老浏览器没有字体度量：退回比例估算 */ }
    return px * 0.22;
  }
  /* 行内元素的 CSS 基线相对 `.ln` 顶边的距离：零高 inline-block 的顶边就落在基线上。
     ⚠️ `getBoundingClientRect()` 在本页是**布局坐标**（<html> 的 zoom 不进 rect），
     所以这里**不**能再除 GZ —— 除了会把偏移缩小，缩放后行内公式的基线会越缩越偏。 */
  function _cssBaselineOffset(elEl) {
    if (!elEl || !elEl.isConnected) return 0;
    const probe = document.createElement("span");
    probe.style.cssText = "display:inline-block;width:0;height:0;padding:0;margin:0;border:0;";
    elEl.insertBefore(probe, elEl.firstChild);
    const off = probe.getBoundingClientRect().top - elEl.getBoundingClientRect().top;
    probe.remove();
    return off;
  }
  /* KaTeX 公式自身的基线 ⇒ 相对 `.fbox` 顶边的距离（px，已含缩放）。 */
  function _formulaBaselineLocal(fb) {
    const base = fb && fb.querySelector(".katex-html .base");
    if (!base) return null;
    const probe = document.createElement("span");
    probe.style.cssText = "display:inline-block;width:0;height:0;padding:0;margin:0;border:0;";
    base.appendChild(probe);
    const top = probe.getBoundingClientRect().top;
    const ftop = fb.getBoundingClientRect().top;
    probe.remove();
    return top - ftop;                  // rect 是布局坐标（见上）
  }
  function _setFormulaBaseline(fb, targetLocal) {
    const fit = fb && fb.firstElementChild;
    if (!fit || !fit.classList.contains("katex-fit")) return;
    if (!fit.querySelector(".katex")) return;          // 裁图兜底不参与对齐
    fit.style.top = "50%";                              // 先回中，免得偏移量累加
    const cur = _formulaBaselineLocal(fb);
    if (cur == null) return;
    const d = targetLocal - cur;
    fit.style.top = Math.abs(d) > 0.05
      ? "calc(50% + " + d.toFixed(2) + "px)" : "50%";
  }
  /* 字符位置 → 所在行（公式的 copy 区间起点落在哪一行）。 */
  function _lineOfPos(item, pos) {
    let last = null;
    for (const ld of item.linesData || []) {
      const end = ld.gs + (ld.text || "").length;
      if (pos >= ld.gs && pos < end) return ld;
      if (pos >= ld.gs) last = ld;
    }
    return last;
  }
  /* 没有 base、也没有行内公式时的基线估算（px，页面坐标）。 */
  function _lineFallbackBasePx(ld) {
    const ln = ld.ln || {};
    const S = ld.S || 1;
    let body = null, size = 0;
    for (const r of ld.runs || []) {
      if (r.gap != null) continue;
      if ((r.s || 0) > size) { size = r.s || 0; body = r; }
    }
    if (!(size > 0)) size = Number(ld.runs && ld.runs[0] && ld.runs[0].s) || 10;
    const fam = (body && body.fam) || "serif";
    return ((Number(ln.y) || 0) + (Number(ln.h) || 0)) * S
         - _fontDescentPx(size * S, fam);
  }
  /* 行的目标基线（页面 px）：后端合并视觉行后直接给 `base`；旧数据用缓存的
     公式基线（若量过）或行框底边估算。 */
  function _lineTargetBasePx(ld) {
    if (Number(ld.base) > 0) return Number(ld.base) * (ld.S || 1);
    if (Number(ld._targetBase) > 0) return Number(ld._targetBase);
    return _lineFallbackBasePx(ld);
  }

  /* 正文行只按自己的基线摆；公式不参与决定文字位置（避免互相带偏）。
     旧 doc.json（没有 `base`）没有视觉行合并数据：带行内公式的行用**公式当前渲染
     基线**兜底一次（量完锁在 `_targetBase`，重排不会漂移）；其余行按行框底边估算。 */
  function _placeLocalLines(it) {
    const pageEl = it.el.parentElement;
    const pageTop = pageEl ? pageEl.getBoundingClientRect().top : 0;
    const fbsAt = new Map();
    for (const fb of it.formulaBoxes || []) {
      if (!fb.isConnected || fb.dataset.cls !== "InlineFormula") continue;
      const rg = fb._copyRange;
      if (!rg) continue;
      const ld = _lineOfPos(it, rg.start);
      if (!ld) continue;
      if (!fbsAt.has(ld)) fbsAt.set(ld, []);
      fbsAt.get(ld).push(fb);
    }

    for (const ld of it.linesData || []) {
      // 旋转行：位置已经由 `ox/oy` + `rotate()` 固定，不按（水平的）基线再摆一次
      if (ld.rot) continue;
      let basePx = Number(ld.base) > 0 ? Number(ld.base) * (ld.S || 1) : 0;
      const fbs = fbsAt.get(ld) || [];
      if (!basePx && fbs.length) {
        if (!(Number(ld._targetBase) > 0)) {
          const ys = [];
          for (const fb of fbs) {
            const local = _formulaBaselineLocal(fb);
            if (local != null)
              ys.push((fb.getBoundingClientRect().top - pageTop) + local);
          }
          if (ys.length) {
            ys.sort((a, b) => a - b);
            ld._targetBase = ys[Math.floor(ys.length / 2)];
          }
        }
        basePx = Number(ld._targetBase) || 0;
      }
      if (!(basePx > 0)) basePx = _lineFallbackBasePx(ld);
      const off = _cssBaselineOffset(ld.el);
      ld.el.style.top = (basePx - it.y - off).toFixed(2) + "px";
    }
  }

  /* 页面级：把每条行内公式按**几何位置**贴到同一视觉行的正文基线上。
     对应旧版 v11.3 的 `_lineBaseline` + `_katexBaseline` 那一套：
       ① 行基线由正文行自己量（这里是 probe 探针，后端 `base` 已给出精确值）；
       ② 公式 KaTeX 的基线用 `.katex-html .base` 里的零高探针量；
       ③ 两者差多少就把 `.katex-fit` 平移多少 —— 不靠字体度量反推。
     找行用「纵向中心距 × 10 + 横向间距」，公式在公式-only 块/被合并到别的
     块时也能认出它属于哪条正文行。 */
  function _alignPageFormulas(pageEl) {
    if (!pageEl) return;
    const pageTop = pageEl.getBoundingClientRect().top;
    const lines = [];
    const byLd = new Map();
    for (const ln of pageEl.querySelectorAll(".blk.lc .ln.lcln")) {
      const ld = ln._ld;
      if (!ld || !(ln.textContent || "").trim()) continue;
      if (ld.rot) continue;                            // 旋转行不参与公式基线对齐
      const r = ln.getBoundingClientRect();
      if (!r.height) continue;
      const info = { ld, r };
      lines.push(info);
      byLd.set(ld, info);
    }
    if (!lines.length) return;
    for (const fb of pageEl.querySelectorAll('.fbox[data-cls="InlineFormula"]')) {
      if (!fb.querySelector(".katex")) continue;        // 裁图兜底不参与对齐
      const fr = fb.getBoundingClientRect();
      const fcy = (fr.top + fr.bottom) / 2;
      const fcx = (fr.left + fr.right) / 2;
      let best = null;
      // 优先用解析侧 copy 区间指向的那一行（合并视觉行之后就是整行本身）；
      // 区间落在公式-only 行/被合并掉的块上时，再按几何找同一视觉行的正文行。
      const rg = fb._copyRange;
      if (rg && fb._item) {
        const ld = _lineOfPos(fb._item, rg.start);
        if (ld && byLd.has(ld)) best = byLd.get(ld);
      }
      if (!best) {
        let bestScore = Infinity;
        for (const info of lines) {
          const r = info.r;
          const dy = Math.abs((r.top + r.bottom) / 2 - fcy);
          const dx = fcx < r.left ? r.left - fcx : (fcx > r.right ? fcx - r.right : 0);
          const score = dy * 10 + dx;
          if (score < bestScore) { bestScore = score; best = info; }
        }
      }
      if (!best) continue;
      const basePx = _lineTargetBasePx(best.ld);
      _setFormulaBaseline(fb, basePx - (fr.top - pageTop));
    }
  }

  /* 把行内公式框的水平位置/宽度锚到该行真实渲染出来的 `.rngap` 占位块上。
     为什么不能直接用 PDF bbox：行内文字用的是替代字体（Georgia），各段实际
     宽度与 PDF 不会一模一样；`.rngap` 之前的位置因此会逐步漂移，而 `.fbox`
     还钉在 PDF 绝对坐标上 —— 公式就会压住后面几个字符（实测 “I_n” 盖住了
     “is” 的 i，“n×n” 盖住了 “identity”）。锚到占位块后，文字、空隙、公式共享
     同一套渲染宽度，无论字体宽窄都不会重叠。
     ⚠️ 整行只有占位、没有正文时不锚：那种行（公式从行首开始 / 旧数据把占位放进
     公式碎片行）的占位左边就是公式自己的位置，解析侧 bbox 才是对的；按占位锚反而
     会把它推到碎片行的起点上。 */
  function _anchorFormulaX(it) {
    const pageEl = it.el.parentElement;
    if (!pageEl) return;
    const pageRect = pageEl.getBoundingClientRect();
    for (const fb of it.formulaBoxes || []) {
      if (!fb.isConnected || fb.dataset.cls !== "InlineFormula") continue;
      const rg = fb._copyRange;
      if (!rg) continue;
      const ld = _lineOfPos(it, rg.start);
      if (!ld) continue;
      if (!(ld.el.textContent || "").replace(/\s+/g, "").length) continue;
      const bid = String(fb.dataset.boxId);
      let x0 = Infinity, x1 = -Infinity;
      for (const sp of ld.el.querySelectorAll('.rngap[data-box="' + bid + '"]')) {
        const rr = sp.getBoundingClientRect();
        x0 = Math.min(x0, rr.left); x1 = Math.max(x1, rr.right);
      }
      if (!(x1 > x0)) continue;
      fb.style.left = (x0 - pageRect.left).toFixed(2) + "px";
      fb.style.width = (x1 - x0).toFixed(2) + "px";
    }
  }

  /* 本地解析方案的收尾：按基线摆好正文行，再把公式对齐到同一视觉行，最后回填块高。
     `only` = 本轮刚挂上的页号集合（Set）：逐页加载时只处理新页，不把已经排好的页
     重新量一遍（每次量都会触发一次布局，页多了会很卡）；不传 = 全部（整体重排）。 */
  function finalizeLocalBlocks(only) {
    const items = [];
    for (const id in itemById) {
      const it = itemById[id];
      if (!it.local || it.kind !== "blk" || !it.el.isConnected) continue;
      if (only && !only.has(it.pi)) continue;
      items.push(it);
    }
    if (!items.length) return;

    const pageEls = new Set();
    // ① 正文行：只按自己的基线定位；行宽压缩后再把公式横向锚到占位块
    for (const it of items) {
      _placeLocalLines(it);
      fitItemLines(it);
      _anchorFormulaX(it);
      if (it.el.parentElement) pageEls.add(it.el.parentElement);
    }
    // ② 公式框宽度变了 → 重新等比缩放 KaTeX；再按缩放后的真实基线对齐到正文行
    refitFormulaBoxes(only);
    for (const pageEl of pageEls) _alignPageFormulas(pageEl);

    for (const it of items) {
      let h = it.h;
      for (const ld of it.linesData || []) {
        if (ld.rot) continue;                          // 旋转行不参与块高（盒是斜的）
        const top = parseFloat(ld.el.style.top) || 0;
        const lh = parseFloat(ld.el.style.height) || 0;
        h = Math.max(h, top + lh);
      }
      if (it.table) {
        const tb = it.el.querySelector(".lctable, .lcln");
        if (tb) {
          /* 表格字号 = 解析侧从 PDF 文字层量到的实际字号（`t.size`）。SLANet 的结构/
             行距跟原表不完全一致时，整张表仍可能比 PDF 表格框高 —— 按框高等比缩
             字号（不低于原字号的 0.7 倍），否则会压住后面的正文（本地方案没有
             块高增长后的向下避让）。只改 .lctable 的字号，译文卡不受影响。 */
          const avail = Math.max(1, it.h);
          const base0 = it.baseSize || 0;
          for (let i = 0; i < 3 && base0 > 0 && tb.offsetHeight > avail + 1; i++) {
            const next = Math.max(base0 * 0.7, it.baseSize * Math.max(0.5, avail / tb.offsetHeight));
            if (next >= it.baseSize - 0.1) break;
            it.baseSize = next;
            tb.style.fontSize = next.toFixed(2) + "px";
          }
          h = Math.max(h, tb.offsetHeight + 2);
        }
      }
      it.contentH = h;
      it.el.style.height = h + "px";
      if (it.exp) it.exp.style.top = h + "px";
    }
  }

  /* ============ 对外接口 ============ */
  return { buildTextItem, finalizeLocalBlocks };
};
