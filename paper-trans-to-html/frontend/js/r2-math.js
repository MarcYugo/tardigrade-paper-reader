/* ============ 公式渲染（KaTeX）：从 reader2.js 拆出来的独立模块 ============
   这块代码原来是 reader2.js 里的连续一段，**按原样搬过来**（缩进都没动，方便对照），
   只有一处改动：原来直接读外层闭包的 `DOC`，现在通过 `env` 取（旧渲染路径需要；
   本地 PyMuPDF 路径移除后，实际只用到 `env.findItem`）。

   装配（reader2.js 底部）：
     const { renderInlineMath, … } = R2Math({ docId, findItem, itemById,
                                              get DOC() { return DOC; } });
   入参只放**跨模块共享**的东西，其余（KATEX_* 常量、_katexP/_katexReady、
   KATEX_EM、各种缓存）都跟着本文件走，不再污染 reader2.js 的作用域。

   对外接口在文件末尾的 return —— 其中 _katexReady 是 let（加载完会置 true），
   不能按值导出，故给的是 katexReady()。

   当前三套渲染共用本模块：
     · 本地解析方案：`buildFormulaBoxes` + `finalizeFormulaBoxes` 按页面级
       `pages[].formula_boxes` 渲染 `.fbox`（KaTeX，失败时用解析侧裁图兜底）；
     · Surya / Unlimited-OCR：`mathBlockOf` + `finalizeDisplayMath` 渲染独立公式块，
       块内行内公式由 reader2.js 的 suryaInlineMath / ulInlineMath 就地换 KaTeX；
     · 通用：`renderInlineMath` 把文本里的 `\\(…\\)` / `$…$` 就地换 KaTeX。
   `inlineMathOf` / `finalizeInlineMath` 是早期「行内公式覆盖层」的兼容接口，
   本地方案不再使用（公式统一由页面级 `.fbox` 绘制）。
   ============================================================================ */
window.R2Math = function (env) {
  "use strict";
  const { findItem, docId } = env;   // 稳定引用，装配时取一次即可（DOC 要用 getter）

  const KATEX_JS = "vendor/katex/katex.min.js";
  const KATEX_CSS = "vendor/katex/katex.min.css";
  const MATH_DELIM_RE = /\\\(|\\\[|\$/;     // 文本里是否含数学定界符(\(、\[、$、$$)
  let _katexP = null;
  let _katexReady = false;      // 组件是否已加载完(未就绪时公式回退原文字)

  function _loadScript(src) {
    return new Promise(res => {
      const s = document.createElement("script");
      s.src = src; s.async = true;
      s.onload = () => res(true); s.onerror = () => res(false);
      document.head.appendChild(s);
    });
  }
  /* 只加载项目内置的 KaTeX；加载失败就把公式当普通文字显示(不至于变成空白)。
     **必须等字体就绪再开渲**：字体没到位时量出来的公式宽度是后备字体的，
     宽度适配会算错，等字体一换公式就攞出框外被裁掉。 */
  function ensureKatex() {
    if (_katexP) return _katexP;
    _katexP = Promise.all([_loadCss(KATEX_CSS), _loadScript(KATEX_JS)])
      .then(([, ok]) => {
        if (!ok || !window.katex || typeof window.katex.render !== "function") return false;
        return _katexFontsReady().then(() => { _katexReady = true; return true; });
      })
      .catch(() => false);
    return _katexP;
  }
  function _loadCss(href) {
    return new Promise(res => {
      if (document.querySelector("link[data-katex-css]")) return res(true);
      const link = document.createElement("link");
      link.rel = "stylesheet"; link.href = href;
      link.setAttribute("data-katex-css", "1");
      link.onload = () => res(true); link.onerror = () => res(false);
      document.head.appendChild(link);
    });
  }
  function _katexFontsReady() {
    if (!document.fonts || !document.fonts.load) return Promise.resolve();
    const fams = ["KaTeX_Main", "KaTeX_Math", "KaTeX_AMS", "KaTeX_Caligraphic",
                  "KaTeX_Size1", "KaTeX_Size2", "KaTeX_Size3", "KaTeX_Size4"];
    const jobs = fams.map(f => {
      try { return document.fonts.load('1em "' + f + '"'); } catch (e) { return null; }
    }).filter(Boolean);
    return Promise.all(jobs).catch(() => {});
  }
  /* 文档里有没有需要数学排版的东西：独立公式(latex)、Surya 块的行内数学区间/
     表格 <math>，或文本里的数学定界符。 */
  function docNeedsMath(doc) {
    for (const pg of ((doc && doc.pages) || [])) {
      // 本地解析方案：页面级公式框（LaTeX + 裁图）
      if ((pg.formula_boxes || []).some(b => b && b.latex)) return true;
      for (const t of (pg.texts || [])) {
        if (t.latex) return true;                         // 公式块（含 Surya kind="formula"）
        if (t.math && t.math.length) return true;         // Surya 块：解析侧标好的行内区间
        if (t.html && t.html.indexOf("<math") >= 0) return true;   // Surya 表格里的 <math>
        // Surya 表格：单元格里的公式是**纯文本 LaTeX**（`O(n^2 \cdot d)`），没有 <math>
        // 标签 —— 检测反斜杠命令以便把 KaTeX 加载起来（误报只是多加载一次，无副作用）。
        if (t.html && t.html.indexOf("<table") >= 0 && /\\[a-zA-Z]{2,}/.test(t.html)) return true;
        if (MATH_DELIM_RE.test(t.text || "")) return true;
      }
    }
    return false;
  }
  /* 行内数学：把文本里的 `\(…\)` / `$$…$$` 原地换成一个 KaTeX 元素。
     KaTeX 是同步的，所以直接扫文本节点、就地替换即可，不用排队也不用重排。
     `opt.loose`：**译文卡专用** —— 那里的 `$…$` 是我们让模型按 `$latex$` 输出的，
     不再走「必须像数学」那条防货币启发式，否则 `$O(n/k)$`、`$k=n$` 这种不含
     `\ _ ^ { }` 的公式会原样留在卡片里（同一张卡里有的渲染、有的不渲染）。 */
  function renderInlineMath(el, opt) {
    if (!_katexReady || !el) return;
    const loose = !!(opt && opt.loose);
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
    const jobs = [];
    let node;
    while ((node = walker.nextNode())) {
      const s = node.nodeValue || "";
      if (s.indexOf("\\(") < 0 && s.indexOf("$") < 0) continue;
      jobs.push(node);
    }
    for (const tn of jobs) {
      // `.md code / .md pre`：Markdown 容器（AI 面板）里的行内代码 / 代码块不换 KaTeX，
      // 保持原样显示（普通正文里没有这两个类，行为不变）。
      if (tn.parentNode && tn.parentNode.closest
          && tn.parentNode.closest(".mathbox, .katex, .md code, .md pre")) continue;
      const parts = _splitInlineMath(tn.nodeValue || "", loose);
      // ⚠️ 不能只按长度判断：**整段就是一个公式**时 parts 只有一个元素(而且是个对象)，
      // 那也是要渲染的 —— 注入出来的 `\(…\)` 常常单独占一个 run(样式与前后文不同，
      // 合并不了)，按长度跳过就会把 `\(T^t\)` 原样显示成一串反斜杠。
      // 真正的「没有公式」是返回 `[原文]` 这一个**字符串**。
      if (parts.length < 2 && typeof parts[0] === "string") continue;
      const frag = document.createDocumentFragment();
      for (const p of parts) {
        if (typeof p === "string") { frag.appendChild(document.createTextNode(p)); continue; }
        const span = document.createElement("span");
        try {
          if (!katexRender(span, p.latex, false)) {
            frag.appendChild(document.createTextNode(p.raw));
            continue;
          }
        }
        catch (e) { frag.appendChild(document.createTextNode(p.raw)); continue; }
        frag.appendChild(span);
      }
      tn.parentNode.replaceChild(frag, tn);
    }
  }
  /* 按定界符切成 ['文字', {latex,raw}, '文字', …]。
     支持 `\(…\)`、`$$…$$`，以及 **单 `$…$`**（OCR 出来的文本里常见这种写法）。
     单 `$` 容易和货币符号撞车，所以正文里额外要求内容「像数学」：首尾不接空格、
     且含 \ _ ^ { } 中的一个，否则不当公式处理（原样留作文字）；
     `loose`（译文卡）只保留「首尾不接空格」这一条。 */
  function _splitInlineMath(s, loose) {
    const re = /\\\(([^]*?)\\\)|\$\$([^]*?)\$\$|\$([^$\n]{1,120}?)\$/g;
    const out = []; let last = 0, m;
    while ((m = re.exec(s))) {
      let latex = (m[1] != null ? m[1] : (m[2] != null ? m[2] : m[3]));
      if (m[3] != null && (/^\s|\s$/.test(latex) || (!loose && !/[\\_^{}]/.test(latex)))) continue;
      if (m.index > last) out.push(s.slice(last, m.index));
      out.push({ latex: latex.trim(), raw: m[0] });
      last = m.index + m[0].length;
    }
    if (!out.length) return [s];
    if (last < s.length) out.push(s.slice(last));
    return out;
  }

  /* ---------- 译文卡里的“裸 LaTeX”兜底渲染 ----------
     译文由模型生成，**不一定**按约定把公式包进 `$…$`（实测常见：直接写
     `ax^2 + bx + c = 0`、`\Delta > 0`、`\frac{-b \pm \sqrt{b^2 - 4ac}}{2a}`），
     历史缓存里的译文更是如此。这类裸片段若不做处理，卡片里就是一串反斜杠源码。
     这里**不做“猜哪里是公式”**（那会把正文词汇吃成公式），而是拿**已知公式**去匹配：
     `list` 就是对应原文块解析侧下发的 `item.inlineMath[].latex`（与正文里渲染的完全同一串）。
     空白差异（多/少/没有空格）容忍，其余字符逐个字面匹配；命中才换 KaTeX，
     渲染失败保留原文 —— 宁可漏渲染，也不改错文字。 */

  /* latex → 正则片段：空白折叠成 `\s*`，其余按字面转义 */
  function _latexToRe(latex) {
    let out = "";
    for (const ch of String(latex)) {
      if (/\s/.test(ch)) {
        if (!out.endsWith("\\s*")) out += "\\s*";     // 连续空白的合并成一个
        continue;
      }
      out += ch.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");   // `^ _ { } \ + ( )` 等全部字面化
    }
    return out;
  }

  function renderKnownLatex(el, list) {
    if (!_katexReady || !el || !list || !list.length) return;
    const pats = [];
    const seen = new Set();
    for (const raw of list) {
      const latex = String(raw == null ? "" : raw).trim();
      if (latex.length < 2 || seen.has(latex)) continue;   // 单个字符太容易误命中
      seen.add(latex);
      const body = _latexToRe(latex);
      if (!body) continue;
      pats.push({ latex, re: new RegExp(body, "g") });
    }
    if (!pats.length) return;
    // 长公式优先：`\Delta > 0` 是 `\Delta > 0.5` 的前缀时，别让短的先把长的切碎
    pats.sort((a, b) => b.latex.length - a.latex.length);
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
    const jobs = [];
    let node;
    while ((node = walker.nextNode())) {
      const s = node.nodeValue || "";
      if (s.length < 2) continue;
      // KaTeX 子树（含 MathML 的 annotation，里面就是 `\textstyle <latex>` 原文）、
      // 独立公式盒、Markdown 代码块：一律不动，否则会把已渲染的公式再啃一遍。
      if (node.parentNode && node.parentNode.closest
          && node.parentNode.closest(".katex, .mathbox, .md code, .md pre")) continue;
      jobs.push(node);
    }
    for (const tn of jobs) _replaceKnownLatex(tn, pats);
  }

  function _replaceKnownLatex(tn, pats) {
    const src = tn.nodeValue || "";
    const hits = [];
    for (const p of pats) {
      p.re.lastIndex = 0;
      let m;
      while ((m = p.re.exec(src))) {
        if (p.re.lastIndex <= m.index) p.re.lastIndex = m.index + 1;   // 空匹配防死循环
        if (!m[0].trim()) continue;
        hits.push({ s: m.index, e: m.index + m[0].length, latex: p.latex });
      }
    }
    if (!hits.length) return;
    // 起点升序、同起点时长的优先；再去掉互相重叠的那些（取先到的）
    hits.sort((a, b) => a.s - b.s || (b.e - b.s) - (a.e - a.s));
    const pick = [];
    let end = -1;
    for (const h of hits) { if (h.s >= end) { pick.push(h); end = h.e; } }
    const frag = document.createDocumentFragment();
    let cur = 0;
    for (const h of pick) {
      if (h.s > cur) frag.appendChild(document.createTextNode(src.slice(cur, h.s)));
      const span = document.createElement("span");
      let ok = false;
      try { ok = katexRender(span, h.latex, false); } catch (e) { ok = false; }
      if (ok) frag.appendChild(span);
      else frag.appendChild(document.createTextNode(src.slice(h.s, h.e)));   // 失败保持原文
      cur = h.e;
    }
    if (cur < src.length) frag.appendChild(document.createTextNode(src.slice(cur)));
    tn.parentNode.replaceChild(frag, tn);
  }

  /* ============ 行内公式的旧“HTML 对齐”路径（已停用） ============
     这条路径把块 HTML 里的 <math> 对齐回 canonical 字符区间，供本地 PyMuPDF
     逐行渲染使用；随本地解析链路一起移除。Surya / Unlimited-OCR 用的是解析侧
     下发的精确字符区间（`t.math`，见 reader2.js 的 suryaMathRanges / ulMathRanges）。
     下面保留函数名 / 签名（空壳），方便之后替换。 */
  function _normText(str) {
    return { s: "", idx: [] };
  }
  function _plainMath(latex) {
    return "";
  }
  function _countChars(s) {
    return new Map();
  }
  function _bagRatio(latex, seg) {
    return 0;
  }
  function _htmlMathParts(html) {
    return null;
  }
  function _unesc(s) {
    return "";
  }
  function _findLit(cn, lit, from) {
    return null;
  }
  function _consumeLit(cn, lit, from) {
    return -1;
  }
  function _alignInlineMath(canon, parts) {
    return null;
  }
  /* 该块的行内公式区间（旧数据兼容）—— 已停用，恒为 null。 */
  function inlineMathOf(t, canon) {
    return null;
  }
  /* x..y 这个子区间落在哪条行内公式里？返回序号，不在任何公式里返回 -1 */
  function _mathIdxAt(list, x, y) {
    if (!list) return -1;
    for (let i = 0; i < list.length; i++) {
      const m = list[i];
      if (m.a <= x && y <= m.b) return i;
    }
    return -1;
  }
  /* 行内公式覆盖层（旧“行内公式让位”实现）已随本地 PyMuPDF 渲染链路移除。
     下面这些函数只保留名字 / 签名（空壳），方便之后替换。
     Surya / Unlimited-OCR 的行内公式走 `suryaInlineMath` / `ulInlineMath`
     （就地替换成 KaTeX，不需要覆盖层与让位）。 */
  function _rightAfterMath(list, gs, ge, x) {
    return false;
  }
  function _mathBaseSize(item, m) {
    return 0;
  }
  function _lineBaseline(lnEl) {
    return null;
  }
  function _katexBaseline(box) {
    return null;
  }
  function finalizeInlineMath() {
    return;
  }
  function _inlineMathNaturalW(box) {
    return 0;
  }
  function _lnScaleX(ln) {
    return 1;
  }
  function _shiftRoomLine(ld, delta) {
    return;
  }
  function _shiftRoomBox(box, dx) {
    return;
  }
  function _resetInlineRoom(item) {
    return;
  }
  function _inlineMathRoom(item) {
    return;
  }

  /* 独立公式的收尾：等块挂到文档上量出真实高度，再回填块高(KaTeX 内容本身是同步渲好的)。 */
  const _katexBoxes = new Set();      // 本轮待收尾的公式覆盖层
  const _mathFit = new Set();         // 已渲染的公式：字体/缩放变化后需要重新适配
  function finalizeDisplayMath() {
    if (!_katexBoxes.size) return;
    for (const box of [..._katexBoxes]) {
      _katexBoxes.delete(box);
      const blk = box._blk;
      if (!blk || !box.isConnected) { box.remove(); continue; }
      // KaTeX 渲染成功 → 有 .katex；失败时盒子里放的是 LaTeX 源码文本，也要留着
      // (公式块已经不渲染原字形了，丢掉这个盒子 = 这一块彻底空白)。
      if (!box.querySelector(".katex") && !box.textContent.trim()) { box.remove(); continue; }
      blk.classList.add("has-math");
      _mathFit.add(box);
      sizeMathBox(box);
    }
    // 兜底：万一首次渲染赶在字体之前，字体一到位就重新量一次(幂等，重复跑无副作用)
    if (document.fonts && document.fonts.ready)
      document.fonts.ready.then(refitMathBoxes).catch(() => {});
  }
  function refitMathBoxes() {
    for (const box of _mathFit) {
      if (!box.isConnected) { _mathFit.delete(box); continue; }
      sizeMathBox(box);
    }
  }
  function sizeMathBox(box) {
    // 兜底展示的 LaTeX 源码不做宽度适配(那会把它缩成一团小字)
    if (!box.classList.contains("failed")) fitMathBox(box);
    const blk = box._blk;
    if (!blk) return;
    const h = Math.max(box._contentH || 0, box.offsetHeight);
    blk.style.height = h + "px";
    const exp = blk.querySelector(".exp");
    if (exp) exp.style.top = h + "px";
  }

  /* 公式的“主体宽度”(pt) —— 早期逐行公式块渲染的兼容接口，恒为 0。
     本地解析方案直接用 `formula_boxes[].bbox_norm` 定宽，不再需要它。 */
  function formulaBodyWidth(t) {
    return 0;
  }

  /* 很多解析结果把公式编号**写进了 LaTeX**（`…\qquad(1)` / `…\quad(1)`），
     PDF 里这个编号却是贴栏右排的。直接渲染会让内容多出一大截，宽度适配就把整条公式算宽了。
     → 把尾部编号摘出来改交给 KaTeX 的 `\tag`：它贴右排放，且**不参与内容宽度**，
     这样“内容宽 vs 主体宽”才是同一个口径。
     守卫：编号前必须有明确的空白命令(\qquad/\quad/\hspace/三连空格…)，
     免得把 `f(x)=g(1)` 结尾的真括号当成编号摘掉。 */
  const _TAIL_TAG_RE = /(?:\\(?:qquad|quad|hspace\*?(?:\{[^}]*\})?|,|;|:|!)|\s{3,})+\s*[（(]\s*(\d+[a-z]?)\s*[)）]\s*$/;
  function splitTailTag(latex) {
    const m = _TAIL_TAG_RE.exec(latex);
    if (!m) return { latex, tag: "" };
    const head = latex.slice(0, m.index).trim();
    return head ? { latex: head, tag: m[1] } : { latex, tag: "" };
  }

  /* KaTeX 内部把字号放大 1.21em（`.katex{font-size:1.21em}`）。那个系数是给“典型网页正文字体”
     配的：本项目里的“正文字号”是 PDF 的 pt 换算值，正文又用 Georgia 顶替原字体，
     两头折算下来必须再压 1/1.15 —— 不然公式的字高比正文大一截（实测 x-height +13%、
     cap +18%），看着就是“公式的字比正文大一号”。压回去之后实测 x-height −2%、cap +3%。
     顺带的好处：宽度也缩回 15%，KaTeX 同字号比原文宽 20~40% 的问题大半出在这个系数上。 */
  const KATEX_EM = 1.15;
  function mathSizePx(basePx) { return Math.max(1, basePx / KATEX_EM); }

  /* 行内公式统一按**行内排版**（`\textstyle`）渲染。

     为什么不能只靠 KaTeX 的 `displayMode:false`：
       · 解析后端（texify / OCR 模型）有时会在 LaTeX 开头自带 `\displaystyle`，
         KaTeX 会照单全收 —— 求和/积分的上下限又被搬到上下方、分式也放大，
         与 PDF 里行内公式的排版（上下限在右侧）不符；
       · 行内公式的盒子是按 PDF 原框尺寸缩放/避让的，display 样式更容易溢出，
         最后被 `sizeFormulaBox` 压小，和旁边的正文对不上。
     所以这里**先剥掉开头的样式开关、再补一个 `\textstyle`**，保证行内一定走正文样式。
     （`katexRender(..., false)` 的所有调用点 —— 页面级 `.fbox` 行内公式、
       `renderInlineMath`、Surya/Unlimited-OCR 的 `.mspan` —— 都会经过这里。） */
  function inlineTextStyle(latex) {
    let source = String(latex || "").trim();
    let prev;
    do {                                   // 可能连续出现 \displaystyle \textstyle …
      prev = source;
      source = source.replace(/^\\(?:display|text)style\b\s*/, "");
    } while (source !== prev);
    return "\\textstyle " + source;
  }

  const KATEX_OPTS = { displayMode: true, throwOnError: true, strict: "ignore", trust: false };
  /* 渲染公式；成功返回 true。LaTeX 有问题就返回 false，调用方回退显示 PDF 原文字。
     display=false 时按行内样式渲染（上下限排在右上/右下）。 */
  function katexRender(box, latex, display) {
    const opts = (display === false) ? Object.assign({}, KATEX_OPTS, { displayMode: false }) : KATEX_OPTS;
    const source = display === false ? inlineTextStyle(latex) : latex;
    const tt = display === false ? { latex: source, tag: "" } : splitTailTag(source);
    if (tt.tag) {
      try {
        window.katex.render(tt.latex + "\\tag{" + tt.tag + "}", box, opts);
        box._hasTag = true;              // 调用方把覆盖层拉满块宽 → 编号贴在原框右边缘
        return true;
      } catch (e) {
        box.textContent = "";            // 清掉半成品，退回原样渲染
      }
    }
    try {
      window.katex.render(source, box, opts);
      // KaTeX 的 `\tag` 是**绝对定位**（right:0）：调用方必须把盒子拉满块宽，
      // 编号才会贴在原框右边缘；否则（shrink-to-fit 的盒子里）编号会直接压在
      // 公式内容上。整页 OCR 后端（Surya 2 / Unlimited-OCR）的模型就可能自带
      // `\tag{n}` —— 上面 splitTailTag 那条路只认 `\\qquad(1)` 形态，这里兜住。
      if (display !== false && /\\tag\b/.test(source)) box._hasTag = true;
      return true;
    }
    catch (e2) { box.textContent = ""; return false; }
  }

  /* 宽度适配：KaTeX 的字距/字体与原文排版不同，**同一字号**下常比原框宽 20~30%，
     而公式块的外框就是 PDF 里这条公式的原始外框 → 不处理就会被块边界裁掉一截。
     按主体宽度等比缩放(下限 0.6)，让公式的占位比例贴近 PDF。
     _basePx 记着基准字号，每次都从基准重算 → 反复调用不会越缩越小。 */
  function fitMathBox(box, tolPx, floor) {
    const base = box._basePx || (box._basePx = parseFloat(box.style.fontSize) || 0);
    if (!base) return;
    box.style.fontSize = base + "px";
    // 可用宽度 = 原文里这条公式的**主体**宽度(不含贴右的编号)；拿不到就退回块宽。
    // 注意别用 box.clientWidth：.mathbox 是 shrink-to-fit，内容窄时它也窄，不是块宽。
    const avail = box._origW || (box._blk ? box._blk.clientWidth : 0);
    if (!avail) return;
    // 内容宽度：带编号的盒子被拉成 100% 宽(为了让 \tag 贴右)，这时 scrollWidth 给的是
    // 盒子宽度而非内容宽度 → 临时切成 max-content 量一次(max-width:100% 仍会兜住超宽)。
    const w0 = box.style.width;
    box.style.width = "max-content";
    const need = box.scrollWidth;
    box.style.width = w0;
    if (need <= avail + (tolPx || 1)) return;
    // 两段式：优先只缩到 floor（行内 0.85，字号基本统一），
    // 缩到 floor 后残留溢出还超过容差，才继续缩到完全装下（下限 0.6）——
    // 否则那几条特别宽的公式会明显压住后面的文字。
    const kFit = avail / need;
    const kSoft = Math.max(kFit, floor || 0.6);
    const over = need * kSoft - avail;
    const k = (over > (tolPx || 1)) ? Math.max(0.6, kFit) : kSoft;
    if (k < 0.995) box.style.fontSize = (base * k) + "px";
  }

  /* ============ 复制公式：给 LaTeX 源码，而不是排版后的显示内容 ============
     一个公式块在 DOM 里有三份“内容”：隐藏的 PDF 原文字行(.ln)、KaTeX 的 MathML 注解、
     KaTeX 的 HTML 渲染结果。直接复制会把三份都带上(字形还会重复)，且拿到的不是源码。
     这里接管 copy 事件：克隆一份选区，把里面的公式元素一律换回 LaTeX，再自己序列化成文本。
     KaTeX 会把源码存进 <annotation encoding="application/x-tex">，所以正文里内联的公式
     (\(…\) / $$…$$ / $…$) 也能一并还原成 `$…$`。
     显示公式块给的是**裸 LaTeX**（即解析出来的那个 `latex` 字段），方便直接贴到别处用。 */
  document.addEventListener("copy", onCopyMath);

  function onCopyMath(ev) {
    if (!ev.clipboardData) return;                     // 老浏览器：不插手
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return;
    const range = sel.getRangeAt(0);

    // ① 整段选区都在同一个公式块里 → 直接给它的 LaTeX
    const sb = closestMathBlk(range.startContainer);
    const eb = closestMathBlk(range.endContainer);
    if (sb && sb === eb) {
      const it = findItem(sb.dataset.id);
      if (it && it.latex) { writeClipboard(ev, it.latex); return; }
    }
    const startFormula = closestFormula(range.startContainer);
    const endFormula = closestFormula(range.endContainer);
    if (startFormula && startFormula === endFormula && startFormula.dataset.latex) {
      writeClipboard(ev, formulaSource(startFormula));
      return;
    }

    // ② 跨块选区：克隆后把公式逐个换回源码(没碰到公式就不插手，走浏览器默认行为)
    const holder = range.cloneContents();
    const formulaHosts = holder.querySelectorAll(".fbox, .mathbox.inline, .blk.has-math .mathbox");
    formulaHosts.forEach(box => {
      if (!box.dataset.latex) return;
      box.replaceWith(document.createTextNode(formulaSource(box)));
    });
    const katexEls = holder.querySelectorAll(".katex");
    const mathBlks = holder.querySelectorAll(".blk.has-math");
    if (!formulaHosts.length && !katexEls.length && !mathBlks.length) return;
    mathBlks.forEach(b => b.querySelectorAll(".ln").forEach(n => n.remove()));  // 去掉隐藏的原文字行
    katexEls.forEach(k => {
      const blk = k.closest(".blk.has-math");            // 整块公式：优先用解析出来的 latex，
      const it = blk ? findItem(blk.dataset.id) : null;  // 口径和“单选一个公式块”保持一致
      k.replaceWith(document.createTextNode(
        (it && it.latex) ? it.latex : katexSource(k)));
    });
    const text = fragToText(holder)
      .replace(/[ \t]+\n/g, "\n").replace(/\n{2,}/g, "\n").trim();
    if (text) writeClipboard(ev, text);
  }

  function writeClipboard(ev, text) {
    ev.clipboardData.setData("text/plain", text);
    ev.preventDefault();
  }
  function closestMathBlk(node) {
    const el = node && (node.nodeType === 1 ? node : node.parentElement);
    return (el && el.closest) ? el.closest(".blk.has-math") : null;
  }
  function closestFormula(node) {
    const el = node && (node.nodeType === 1 ? node : node.parentElement);
    return el && el.closest
      ? el.closest(".fbox, .mathbox.inline, .blk.has-math .mathbox")
      : null;
  }
  function formulaSource(host) {
    const latex = host.dataset.latex || "";
    return host.matches('.fbox[data-cls="InlineFormula"], .mathbox.inline')
      ? "$" + latex + "$" : latex;
  }
  /* 从 KaTeX 的标注节点里取回源码；显示公式给裸 LaTeX，行内公式用 $…$ 包起来
     (行内混在正文里，不包就分不清哪段是公式)。 */
  function katexSource(katexEl) {
    const a = katexEl.querySelector('annotation[encoding="application/x-tex"]');
    const tex = ((a && a.textContent) || "").trim();
    if (!tex) return katexEl.textContent || "";
    return katexEl.closest(".katex-display") ? tex : "$" + tex + "$";
  }
  /* 公式块的“句子文本”是 PDF/OCR 抽出来的字形串(可能乱码)，复制时一律换成 LaTeX */
  function mathTexOf(item) { return (item && item.latex) ? item.latex : ""; }

  const _BLOCK_TAG = /^(div|p|li|h[1-6]|tr|table|section|article|blockquote)$/;
  /* 把克隆出来的片段序列化成文本：块级元素前后加换行，手感和浏览器默认复制一致 */
  function fragToText(root) {
    let out = "";
    (function walk(n) {
      for (const c of n.childNodes) {
        if (c.nodeType === 3) { out += c.nodeValue; continue; }
        if (c.nodeType !== 1) continue;
        const tag = c.tagName.toLowerCase();
        if (tag === "br") { out += "\n"; continue; }
        const block = _BLOCK_TAG.test(tag);
        if (block) out += "\n";
        walk(c);
        if (block) out += "\n";
      }
    })(root);
    return out;
  }

  /* 该文字块用什么渲染？返回 {latex} 或 null(按普通文字渲染)。
     公式块（`kind="formula"` + `latex`）：整页 OCR 后端（Surya 2 / Unlimited-OCR）的
     独立公式走这里；早期 OCR 链路（已删除）内联进正文的公式也是同一形状，
     旧 doc.json 仍能渲染。只认「整块就是公式」的块；正文里内联的
     `\(…\)` / `$…$` 数学交给 renderInlineMath。 */
  function mathBlockOf(t) {
    if (!t || t.kind !== "formula") return null;
    const latex = typeof t.latex === "string" ? t.latex.trim() : "";
    return latex ? { latex } : null;
  }

  /* 页面正文基准字号（本地公式框定尺的兼容接口）—— 取页面首个文字块的字号。 */
  function pageBodySize(pi) {
    const doc = env.DOC;
    const pg = doc && doc.pages && doc.pages[pi];
    const item = pg && (pg.texts || []).find(t => t && t.kind !== "table");
    return (item && Number(item.size)) || 10;
  }

  /* 公式的主体字号 —— 解析侧给了 `formula_boxes[].size` 就用它，否则用正文基准。 */
  function formulaBaseSize(linesData, pi, lines) {
    const first = lines && lines[0];
    return (first && Number(first.size)) || pageBodySize(pi) || 10;
  }

  /* ============ 页面级公式框覆盖层（本地解析方案） ============
     解析侧 `pages[].formula_boxes[]` 给出 `bbox_norm`（中心 xywh / 页面尺寸）与
     `latex`；这里在页面上放一个 `.fbox`，内部 `.katex-fit` 居中渲染 KaTeX，
     并按框的宽高等比缩放（字体/字距与原文不同，不缩就会被框裁掉）。
     KaTeX 未就绪或渲染失败时：有裁图就用裁图（与 PDF 原位同框），
     连裁图也没有就显示 LaTeX 源码（`.failed`）—— 至少不会整块空白。
     公式框的“属于哪个文字块/哪个字符区间”由 reader2.js::attachFormulaBoxes 接上。 */
  const _fboxSet = new Set();

  function _fboxRect(meta, pg, S) {
    const norm = meta && meta.bbox_norm;
    if (norm && norm.length === 4) {
      const cx = Number(norm[0]) || 0, cy = Number(norm[1]) || 0;
      const w = Number(norm[2]) || 0, h = Number(norm[3]) || 0;
      if (w > 0 && h > 0) {
        return {
          left: (cx * pg.w - w * pg.w / 2) * S,
          top: (cy * pg.h - h * pg.h / 2) * S,
          width: w * pg.w * S,
          height: h * pg.h * S,
        };
      }
    }
    const box = meta && meta.bbox;
    if (!box || box.length !== 4) return null;
    return {
      left: (Number(box[0]) || 0) * S,
      top: (Number(box[1]) || 0) * S,
      width: Math.max(1, (Number(box[2]) - Number(box[0])) * S),
      height: Math.max(1, (Number(box[3]) - Number(box[1])) * S),
    };
  }

  function buildFormulaBoxes(pg, pageEl, S, pi) {
    const doc = env.DOC;
    if (!doc || doc.parser !== "pymupdf") return 0;
    const metas = pg.formula_boxes || [];
    let count = 0;
    for (const meta of metas) {
      const rect = _fboxRect(meta, pg, S);
      if (!rect || !(rect.width > 0) || !(rect.height > 0)) continue;
      const latex = String(meta.latex || "").trim();
      const fb = document.createElement("div");
      fb.className = "fbox";
      fb.dataset.boxId = String(meta.id);
      fb.dataset.cls = meta.cls || (meta.role === "inline"
        ? "InlineFormula" : "DisplayedFormulaBlock");
      fb.dataset.latex = latex;
      Object.assign(fb.style, {
        left: rect.left + "px", top: rect.top + "px",
        width: rect.width + "px", height: rect.height + "px",
      });

      const fit = document.createElement("div");
      fit.className = "katex-fit";
      const base = Math.max(6, Number(meta.size) || 10) * S;
      fit.style.fontSize = mathSizePx(base) + "px";
      fit._basePx = parseFloat(fit.style.fontSize) || base;
      let ok = false;
      if (latex && _katexReady) {
        try { ok = katexRender(fit, latex, meta.role !== "inline"); }
        catch (e) { ok = false; }
      }
      if (ok) {
        fb.appendChild(fit);
      } else if (meta.file) {
        // KaTeX 未就绪/渲染失败：用解析时裁下的公式图兜底（与 PDF 原位同框）
        const img = document.createElement("img");
        img.alt = latex || "formula";
        img.loading = "lazy";
        const url = "/api/doc/" + docId + "/img/" + meta.file;
        img.src = meta.cache_key ? url + "?v=" + encodeURIComponent(meta.cache_key) : url;
        fb.appendChild(img);
      } else if (latex) {
        fit.textContent = latex;
        fb.classList.add("failed");
        fb.appendChild(fit);
      } else {
        continue;                       // 既无 LaTeX 又无裁图：不产生空盒子
      }

      pageEl.appendChild(fb);
      _fboxSet.add(fb);
      count++;
    }
    return count;
  }

  function sizeFormulaBox(fb) {
    if (!fb || !fb.isConnected) return;
    const fit = fb.firstElementChild;
    if (!fit || !fit.classList.contains("katex-fit")) return;
    if (!fit.querySelector(".katex")) return;      // 裁图/失败源码不参与缩放
    const base = fit._basePx || parseFloat(fit.style.fontSize) || 0;
    if (base) fit.style.fontSize = base + "px";
    fit.style.transform = "translate(-50%, -50%)";
    const availW = fb.clientWidth, availH = fb.clientHeight;
    if (!(availW > 0) || !(availH > 0)) return;
    const needW = fit.scrollWidth || fit.offsetWidth;
    const needH = fit.scrollHeight || fit.offsetHeight;
    if (!(needW > 0) || !(needH > 0)) return;
    // 行内公式的框按 PDF **墨迹**裁，框高就是字形高度；KaTeX 的行盒还带 ascent/
    // descent 留白，通常比墨迹高 15~30%。严格按高度缩会把行内公式压得比正文小一号
    // （正文补回正常字号后尤其明显）。这里允许行内公式溢出框 15%（PDF 里行内公式
    // 本来就会伸出正文行框），主要按宽度定尺；显示公式仍严格塞进框内。
    const inline = fb.dataset.cls === "InlineFormula";
    const hCap = inline ? 1.15 : 1;
    let k = Math.min(1, availW / (needW * 1.02), (availH * hCap) / (needH * 1.02));
    k = Math.max(0.55, k);
    fit.style.transform = "translate(-50%, -50%) scale(" + k.toFixed(4) + ")";
  }

  function finalizeFormulaBoxes() {
    if (!_fboxSet.size) return;
    for (const fb of [..._fboxSet]) {
      if (!fb.isConnected) { _fboxSet.delete(fb); continue; }
      sizeFormulaBox(fb);
    }
    // 字体晚到会让第一次量的宽度偏小/偏大：字体就绪后再量一次（幂等）
    if (document.fonts && document.fonts.ready)
      document.fonts.ready.then(refitFormulaBoxes).catch(() => {});
  }

  function refitFormulaBoxes() {
    for (const fb of [..._fboxSet]) {
      if (!fb.isConnected) { _fboxSet.delete(fb); continue; }
      sizeFormulaBox(fb);
    }
  }

  function fitInlineFormulaHeight(host) {
    if (host) sizeFormulaBox(host.closest ? host.closest(".fbox") : null);
  }

  function fitFormulaBox(inner) {
    const fb = inner && inner.closest ? inner.closest(".fbox") : null;
    if (fb) sizeFormulaBox(fb);
  }

  /* ============ 对外接口 ============ */
  return {
    ensureKatex, docNeedsMath, renderInlineMath, renderKnownLatex,
    inlineMathOf, _mathIdxAt, _rightAfterMath,
    finalizeInlineMath, _inlineMathRoom, finalizeDisplayMath, formulaBodyWidth, mathSizePx,
    katexRender, mathTexOf, mathBlockOf, formulaBaseSize, _katexBoxes,
    buildFormulaBoxes, finalizeFormulaBoxes, refitFormulaBoxes,
    katexReady: () => _katexReady,      // let，加载完会置 true → 必须用函数按需取
  };
};
