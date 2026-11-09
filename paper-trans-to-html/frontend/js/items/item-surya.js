/* ============ Surya 2 的块级渲染：从 reader2.js 拆出来的模块 ============
   （与 PyMuPDF 的“逐行绝对定位”是两套）
   surya 后端下发的版面数据是**块级**的：每块只有 label / bbox / html（块内没有行几何）。
   所以这里不做逐行还原，而是：
     · `.blk` 仍按 bbox 绝对定位（阅读顺序与版面关系不变）；
     · 块内文字**正常流式排版**：字号/行高由解析侧估算（`t.size` / `t.line_h`，单位 pt），
       标题加粗放大、图注/脚注小一号（CSS `.sy-*`），长段落自动换行；
     · 行内公式：解析侧给了精确的字符区间（`t.math`，含 LaTeX 源码）—— 原字形
       就地换成 KaTeX（suryaInlineMath），正文围绕公式自然重排；
     · 独立公式（kind="formula" + latex）：KaTeX 块渲染（与 PyMuPDF 公式块同一套收尾）；
     · 表格（kind="table"）：白名单渲染 Surya 的 `<table>` HTML，不参与句子选中。
   文本块的内容仍由通用 `rebuildItem` 生成 —— 选中/高亮/笔记重建后样式与公式都不丢。

   装配（reader2.js，R2Math 之后）：
     const { buildSuryaItem, suryaInlineMath, suryaLineBreaks, suryaTableMath,
             finalizeSuryaBlocks, settleSuryaVertical } = R2ItemSurya({ … });
   `suryaTableMath` 还要交给 item-local.js（本地解析方案的表格单元格公式同一套识别）。
   ============================================================================ */
window.R2ItemSurya = function (env) {
  "use strict";
  const { el, splitSentences, bodyRunOf, mathBlockOf, mathSizePx,
          katexReady, katexRender, renderInlineMath, _katexBoxes,
          itemById, nextOrd, pages } = env;

  function buildSuryaItem(t, S, pageEl, pi) {
    const bx = t.x * S, by = t.y * S;
    const blk = el("div", "blk sy");
    if (t.kind) blk.classList.add("sy-" + t.kind);
    if (t.kind === "heading") blk.classList.add("sy-l" + (t.level || 2));
    Object.assign(blk.style, { left: bx + "px", top: by + "px", width: t.w * S + "px" });
    blk.dataset.id = t.id;
    if (t.label) blk.dataset.label = t.label;
    // 对齐方式：解析侧下发 t.align（新数据）；旧数据缺字段时按同一规则本地兜底 ——
    // 正文类（text/list/caption/footnote）两端对齐（CSS `[data-align="justify"]`），
    // 让行右缘像原 PDF（LaTeX justified 排版）一样齐平到栏边界；标题/页眉页脚等
    // 保持左对齐（单行，两端对齐无意义）。末行不拉伸，与原 PDF 一致。
    blk.dataset.align = (t.align === "justify" || t.align === "left"
                         || t.align === "center" || t.align === "right")
      ? t.align
      : ((t.kind === "text" || t.kind === "list" ||
          t.kind === "caption" || t.kind === "footnote") ? "justify" : "left");

    const size = t.size > 0 ? t.size : 10;        // 解析侧估的字号（pt）
    blk.style.fontSize = size * S + "px";
    // 竖排文本（旋转 90° 的页眉/水印，如 arXiv 侧边戳）：Surya 不给旋转信息，用
    // “又高又窄 + 短文本”判 —— 内容整体旋转、沿条带自下而上排（见 CSS .sy-vertical）
    if (t.h > t.w * 3 && (t.text || "").length >= 2 && (t.text || "").length <= 80) {
      blk.classList.add("sy-vertical");
    }

    const mb = mathBlockOf(t);                     // kind="formula" + latex → KaTeX 块
    const linesData = [];
    let contentH = t.h * S;        // 初值=bbox 高；挂到文档后由 finalizeSuryaBlocks 回填实测高
    if (mb) {
      const box = el("div", "mathbox");
      box.dataset.latex = mb.latex;
      box.style.fontSize = mathSizePx(size * S) + "px";
      if (katexReady() && katexRender(box, mb.latex)) {
        // 正常路径
      } else {
        box.textContent = mb.latex;                // KaTeX 未就绪/LaTeX 有问题 → 源码兜底
        box.classList.add("failed");
      }
      box._blk = blk;
      box._contentH = contentH;
      box._origW = t.w * S;                        // 公式主体宽 = Surya 给的外框宽
      if (box._hasTag) box.style.width = "100%";
      blk.appendChild(box);
      _katexBoxes.add(box);
    } else if (t.kind === "table") {
      const tb = suryaTableEl(t.html, size * S);
      const title = suryaTableTitle(t);
      if (title) blk.appendChild(title);
      blk.appendChild(tb || el("div", "ln syln", t.text || ""));
    } else {
      // 文本块：唯一一条“流式行”。类名要含 ln（行内公式的让位机制按 .ln 找行），
      // `ld.free` 让 fitLineEl 跳过（这里的宽度是排版自然宽，不是 PDF 行框）。
      const lineEl = el("div", "ln syln");
      const lh = t.line_h > 0 ? t.line_h : size * 1.42;
      lineEl.style.lineHeight = lh * S + "px";
      blk.appendChild(lineEl);
      const ln = (t.lines && t.lines[0]) || { runs: [] };
      const ld = {
        ln, dy: 0, free: true, gaps: [],
        // 补上 run 级的字号/字体（后端 run 只有 t/b/i/up；styleRun 与公式基准字号都按 r.s 算）
        runs: (ln.runs || []).map(r => Object.assign({ s: size, fam: "serif" }, r, { S, g: 0 })),
        el: lineEl, text: "", gs: 0,
      };
      ld.text = ld.runs.map(r => r.t).join("");
      linesData.push(ld);
    }
    blk.style.height = contentH + "px";

    // canonical：行文本原样拼接，行末一个 '\n'（与 PyMuPDF 路径同口径）
    let g = 0;
    for (const ld of linesData) {
      ld.gs = g;
      for (const r of ld.runs) { r.g = g; g += r.t.length; }
      g += 1; // '\n'
    }
    const item = {
      kind: "blk", id: t.id, pi, el: blk, text: t.text || "",
      x: bx, y: by, w: t.w * S, h: t.h * S, contentH,
      latex: (mb && mb.latex) || "",
      linesData, canonical: "", sents: [],
      markerRanges: [], selRanges: [], shift: 0, expH: 0,
      ord: nextOrd(), surya: true,
      syHeading: t.kind === "heading",      // 标题：finalizeSuryaBlocks 里按需放宽不折行
    };
    item.canonical = linesData.map(ld => ld.text + "\n").join("");
    item.bodyRun = bodyRunOf(linesData)
      || { s: size, fam: "serif", i: false, b: false, up: false };
    // 行内公式：解析侧给的是 text 里的**精确字符区间**（比旧路径的“HTML 对齐”稳）。
    // 渲染交给 suryaInlineMath：`.mspan` 原字形就地换成 KaTeX（见 rebuildItem 末尾）；
    // 公式块不重复渲染（会叠重影）。
    item.inlineMath = (!mb && linesData.length && t.math && t.math.length)
      ? suryaMathRanges(t, item.canonical) : null;
    item.sents = mb ? [] : splitSentences(item.canonical).map((sg, si) =>
      ({ si, s: sg.s, e: sg.e, key: item.id + "#" + si,
        text: item.canonical.slice(sg.s, sg.e).replace(/\n+/g, " ").trim() }));
    // 表格没有可切句的 .rn 结构：点它不该弹“翻译/标注”工具条（点击分支按这个清选中）
    if (t.kind === "table") item.noSelect = true;

    // 译文 + 内嵌笔记展开区（与 buildTextItem 同一套约定）
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

  /* Surya 块的行内数学区间（解析侧给的 text 偏移）→ 覆盖层用的 {a,b,latex}。
     越界/空的丢掉；区间相对 `t.text`，canonical 只在行末多一个 '\n'，不影响行内区间。 */
  function suryaMathRanges(t, canonical) {
    const out = [];
    for (const m of (t.math || [])) {
      const a = m && m.a | 0, b = m && m.b | 0;
      const latex = String((m && m.latex) || "").trim();
      if (!latex || !(b > a) || a < 0 || b > canonical.length) continue;
      out.push({ a, b, latex });
    }
    return out.length ? out : null;
  }

  /* Surya 块的行内公式：把 `.mspan` 原字形**就地换成 KaTeX**。
     不走 .mathbox.inline 覆盖层 —— 那套是给“逐行绝对定位、行宽固定”的 PyMuPDF 版面
     设计的（覆盖层 + mspacer 让位）；流式排版里行会重排，覆盖层定位不稳（实测公式会
     压到隔壁词上）。就地渲染后正文围绕真公式自然重排，宽度与基线都交给浏览器。
     渲染失败（KaTeX 未就绪 / LaTeX 有错）就保持原字形 —— 至少可读，不会整段空白。
     `rebuildItem` 每次重建都会重新走一遍（幂等）。 */
  function suryaInlineMath(item) {
    const list = item.inlineMath;
    if (!list || !item.el) return;
    const byMi = new Map();
    for (const sp of item.el.querySelectorAll(".mspan[data-mi]")) {
      const mi = sp.dataset.mi;
      if (!byMi.has(mi)) byMi.set(mi, []);
      byMi.get(mi).push(sp);
    }
    if (!byMi.size) return;
    const S = (item.linesData[0] && item.linesData[0].runs[0] &&
               item.linesData[0].runs[0].S) || 1;
    const sizePt = (item.bodyRun && item.bodyRun.s) || 10;
    for (const [miKey, spans] of byMi) {
      const f = list[parseInt(miKey, 10)];
      if (!f) continue;
      const host = document.createElement("span");
      host.className = "katex-inline";
      host.dataset.mi = miKey;
      host.dataset.latex = f.latex;
      host.style.fontSize = mathSizePx(sizePt * S) + "px";
      let ok = false;
      try { ok = katexReady() && katexRender(host, f.latex, false); }
      catch (e) { ok = false; }
      if (!ok) continue;                       // 保底：原字形仍在（没被删）
      const first = spans[0];
      // 高亮/选中/笔记下划线是加在 span 上的：跟着公式一起搬到 KaTeX 宿主上
      for (const cls of ["hl", "hasnote", "sel"])
        if (first.classList.contains(cls)) host.classList.add(cls);
      if (first.dataset.hl) host.dataset.hl = first.dataset.hl;
      for (const st of ["background", "color", "textShadow"])
        if (first.style[st]) host.style[st] = first.style[st];
      first.parentNode.insertBefore(host, first);
      for (const sp of spans) sp.remove();     // 原字形不再占位（宽度由 KaTeX 决定）
    }
  }

  /* Surya 块的块内换行：canonical 里的 '\n'（`</li>`、`<br>` 等）在 HTML 流里会被
     折叠成空格，列表项/表格行会连成一行。把 `\n` 拆成 <br> —— 只动文本节点，
     span 上的 data-g / 高亮 class 都保持原样（选句/高亮仍按字符区间走）。
     KaTeX 子树跳过（MathML 文本节点里的换行不动它）。 */
  function suryaLineBreaks(item) {
    const line = item.el && item.el.querySelector(".syln");
    if (!line) return;
    const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT, null);
    const jobs = [];
    let node;
    while ((node = walker.nextNode()))
      if (node.nodeValue.indexOf("\n") >= 0) jobs.push(node);
    for (const tn of jobs) {
      if (tn.parentNode && tn.parentNode.closest &&
          tn.parentNode.closest(".katex-inline, .katex")) continue;
      const parts = tn.nodeValue.split("\n");
      const frag = document.createDocumentFragment();
      parts.forEach((p, i) => {
        if (i) frag.appendChild(document.createElement("br"));
        if (p) frag.appendChild(document.createTextNode(p));
      });
      tn.parentNode.replaceChild(frag, tn);
    }
  }

  /* Surya 的表格 HTML → 安全 DOM（白名单）。
     HTML 来自外部推理服务，绝不直接 innerHTML：只放行表格结构与基础行内标签，
     属性只留 colspan/rowspan（表格布局必需）。拿不到 <table> 时返回 null。 */
  const SY_TABLE_TAGS = { TABLE: 1, THEAD: 1, TBODY: 1, TFOOT: 1, TR: 1, TD: 1, TH: 1,
                          CAPTION: 1, B: 1, I: 1, EM: 1, STRONG: 1, SUP: 1, SUB: 1,
                          BR: 1, SPAN: 1 };
  function suryaTableTitle(t) {
    const html = String(t && t.html || "");
    const label = String(t && (t.label || t.raw_label) || "").trim();
    if (!label || /<caption\b/i.test(html)) return null;
    return el("div", "sytable-title", label);
  }
  function suryaTableEl(html, sizePx) {
    const s = String(html || "");
    if (!s || s.indexOf("<table") < 0) return null;
    let doc = null;
    try { doc = new DOMParser().parseFromString(s, "text/html"); }
    catch (e) { return null; }
    const src = doc && doc.body && doc.body.querySelector("table");
    if (!src) return null;
    const wrap = el("div", "sytable");
    wrap.appendChild(syCleanNode(src));
    suryaTableMath(wrap, sizePx || 0);   // 单元格里的公式（纯文本 LaTeX）→ KaTeX
    renderInlineMath(wrap);              // 单元格/caption 里 $…$ / \(…\) 包裹的片段
    return wrap;
  }
  function syCleanNode(node) {
    if (node.nodeType === 3) return document.createTextNode(node.nodeValue);
    if (node.nodeType !== 1) return document.createTextNode("");
    const tag = node.tagName.toUpperCase();
    if (!SY_TABLE_TAGS[tag]) {          // 不认识的标签：剥壳、保留里面的内容
      const frag = document.createDocumentFragment();
      for (const c of node.childNodes) frag.appendChild(syCleanNode(c));
      return frag;
    }
    const out = document.createElement(tag.toLowerCase());
    if (tag === "TD" || tag === "TH") {
      for (const name of ["colspan", "rowspan"]) {
        const v = parseInt(node.getAttribute(name), 10);
        if (v > 1 && v <= 100) out.setAttribute(name, String(v));
      }
    }
    for (const c of node.childNodes) out.appendChild(syCleanNode(c));
    return out;
  }

  /* Surya 表格单元格里的公式 → KaTeX。Surya 的表格 HTML 里公式是**纯文本 LaTeX**
     （`O(n^2 \cdot d)`、`1.4 \cdot 10^{20}`），没有 `<math>` 标签；不渲染的话看到
     的就是一串反斜杠源码。识别放在 suryaCellLatex（保守：拿不准就不动，渲染失败
     也保留原文）。只处理「整格都是公式」的单元格 —— 混合自然语言的格子不动
     （那些格里的 `$…$` / `\(…\)` 片段由 renderInlineMath 那一路处理）。
     KaTeX 未就绪时整体跳过：加载完成后 render() 会重跑，这里会再走一遍。 */
  function suryaTableMath(wrap, sizePx) {
    if (!katexReady()) return;
    for (const cell of wrap.querySelectorAll("td, th, caption")) {
      if (cell.querySelector(".katex")) continue;            // 已渲染过（幂等）
      const tns = [];
      const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT, null);
      let n;
      while ((n = walker.nextNode())) tns.push(n);
      if (!tns.length) continue;
      const latex = suryaCellLatex(tns.map(t => t.nodeValue).join(""));
      if (!latex) continue;
      const host = document.createElement("span");
      host.className = "katex-inline";
      host.dataset.latex = latex;
      if (sizePx > 0) host.style.fontSize = mathSizePx(sizePx) + "px";
      let ok = false;
      try { ok = katexRender(host, latex, false); } catch (e) { ok = false; }
      if (!ok) continue;                       // 渲染失败：原文照旧保留
      cell.textContent = "";
      cell.appendChild(host);
    }
  }

  /* 单元格文本 → LaTeX 源码（识别不出返回空串）。规则：
       ① 整格被 `$…$` / `\(…\)` / `\[…\]` 包裹 → 取内部；
       ② 整格含 LaTeX 命令（`\cdot`、`\log`、`\frac`…）→ 整格按 LaTeX 渲染；
       ③ 整格无空格、含 `^` / `_` 的纯 ASCII 上下标（`n^2`、`d_{model}`）→ 按 LaTeX 渲染。
     带空格的自然语言、超长文本都不能命中（保持原样）。 */
  function suryaCellLatex(s) {
    const t = String(s || "").trim();
    if (!t || t.length > 200) return "";
    const m = /^\$([^$]+)\$$/.exec(t)
           || /^\\\(([\s\S]+)\\\)$/.exec(t)
           || /^\\\[([\s\S]+)\\\]$/.exec(t);
    if (m) return m[1].trim();
    if (/\\[a-zA-Z]{2,}/.test(t)) return t;                 // \cdot / \log / \frac …
    if (!/\s/.test(t) && /[\^_]/.test(t) && /^[\x20-\x7E]+$/.test(t)) return t;
    return "";
  }

  /* Surya 块（流式排版）收尾：内容高度要等挂到文档上才量得准 —— 回填
     item.contentH / `.blk` 高度 / 译文卡位置（relayout 的避让都按 contentH 算）。
     **只放大不缩小**：文本量比原框多时按实际高度长出去（解析侧已按块框做过
     缩字适配，溢出很小）。公式块的高度由 finalizeDisplayMath 收尾（这里跳过）；
     表格（及 HTML 解析失败时的纯文本兜底）按**实际渲染高度**回填 —— Surya 的
     检测框高常小于表格真实高度（字体/内距差异），不回填的话下面的文字会按
     变矮的框算避让、压到表格上（见 settleSuryaVertical）。 */
  function finalizeSuryaBlocks(only) {
    for (const id in itemById) {
      const it = itemById[id];
      if (!it.surya || it.kind !== "blk") continue;
      if (only && !only.has(it.pi)) continue;
      if (it.syHeading && it.linesData.length) {
        // 标题不折行：量内容自然宽度，需要时把块放宽（最多到原框的 2.4 倍，长标题
        // 顶多折一行）。⚠️ CSS 的 `min(max-content, X)` 不合法（关键词不能进 min()），
        // 只能挂载后量。窄框标题（居中的 "Abstract" 之类）折行会压到下面正文上。
        const lineEl = it.linesData[0].el;
        const prevW = lineEl.style.width;
        lineEl.style.width = "max-content";     // 量单行自然宽（块级盒 max-content 有效）
        const natural = lineEl.offsetWidth;
        lineEl.style.width = prevW;
        if (natural > it.w + 1) {
          // +4px 余量：自然宽是整数，差零点几个像素就会在词中间折行（"Abstract" 实测）
          it.el.style.width = Math.min(natural + 4, it.w * 2.4).toFixed(1) + "px";
        }
      }
      let h = 0;
      for (const ld of it.linesData) h = Math.max(h, ld.el.offsetHeight + (ld.dy || 0));
      if (!it.linesData.length) {
        // 没有流式行的块 = 表格（含 HTML 解析失败的纯文本兜底）或公式 —— 按块内
        // **流内容**（`.sytable-title` + `.sytable`，含外边距）的实际总高量取；
        // 公式盒是绝对定位、卡片容器（.exp）也不占流，全被跳过 → 量不到就跳过
        // 整块（公式高度归 finalizeDisplayMath 管，别覆盖它算好的高度）。
        let found = false;
        for (const ch of it.el.children) {
          if (ch.classList.contains("exp")) continue;      // 卡片容器（绝对定位）
          const cs = getComputedStyle(ch);
          if (cs.position === "absolute") continue;        // 公式盒等不占流
          h += (parseFloat(cs.marginTop) || 0) + ch.offsetHeight
             + (parseFloat(cs.marginBottom) || 0);
          found = true;
        }
        if (!found) continue;
      }
      h = Math.max(h, it.h);
      it.contentH = h;
      it.el.style.height = h + "px";
      if (it.exp) it.exp.style.top = h + "px";
    }
  }

  /* Surya 块与图片的**纵向避让**：检测框纵向膨胀 + 行内 KaTeX 撑高行盒 + 表格按真实
     内容渲染（高度常超检测框），实测内容经常比块框高 —— “文本满框”时上一块的文字
     会与下一块（尤其是表格）叠在一起。这里在 finalizeSuryaBlocks 量完真实高度后，
     按「同栏、y 升序」逐块算一个**只下移**的位移量 `it.syShift`：本块顶不得高于同栏
     任一已排元素的内容底 + SY_VGAP（表格与图片也参与：图片压到文字/表格上的情况同样
     要躲开）。同栏 = x 区间重叠 ≥ 两者较窄宽的一半（双栏左右栏互不影响；跨栏块是
     下方两栏共同的前邻）；级联自然收敛 —— 按 y 序处理，前块用的都是避让后的底。
     ⚠️ 位移量每轮都**基于原始 it.y 重算**（且不写回数据）：缩放/窗口变化会
     整页重建，每次都以原始几何为基准，不会累积漂移；应用交给 relayoutPage
     的位移链（syShift 是它的起点，与“卡片顶开”量叠加，transform / 页面高度 /
     笔记卡位置因此都自动跟随）。 */
  const SY_VGAP = 2;      // px：避让后相邻块内容之间至少保留的空隙
  function settleSuryaVertical(only) {
    for (let pi = 0; pi < pages.length; pi++) {
      if (only && !only.has(pi)) continue;
      const page = pages[pi];
      const list = page.items.filter(it =>
        it.surya && !(it.kind === "blk" && it.el.classList.contains("sy-vertical")));
      list.sort((a, b) => (a.y - b.y) || (a.x - b.x));
      const settled = [];           // [{x, w, bottom}]：已排元素（bottom 含避让量）
      for (const it of list) {
        let need = -Infinity;
        const width = it.kind === "blk" ? Math.max(it.w, it.el.offsetWidth) : it.w;
        for (const s of settled) {
          const ov = Math.min(s.x + s.w, it.x + width) - Math.max(s.x, it.x);
          if (ov < Math.min(s.w, width) * 0.5) continue;       // 不同栏
          need = Math.max(need, s.bottom + SY_VGAP);
        }
        it.syShift = Math.max(0, need - it.y);      // 只下移，绝不上移
        const height = it.kind === "blk" ? Math.max(it.contentH || 0, it.el.offsetHeight)
                                         : it.h;
        settled.push({ x: it.x, w: width,
                       bottom: it.y + it.syShift + height });
      }
    }
  }

  /* ============ 对外接口 ============ */
  return {
    buildSuryaItem, suryaInlineMath, suryaLineBreaks, suryaTableMath,
    finalizeSuryaBlocks, settleSuryaVertical,
  };
};
