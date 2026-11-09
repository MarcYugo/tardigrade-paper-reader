/* ============ Unlimited-OCR 的块级渲染：从 reader2.js 拆出来的模块 ============
   unlimited_ocr 后端（backend/pdf_parser/backend/backend_unlimited_ocr.py）下发的版面数据是
   **块级**的（每块只有 bbox / kind / 文本，块内没有行几何）。思路与 Surya 那套相同
   （绝对定位的 .blk + 块内流式排版），但代码**刻意独立成一整套**（buildUnlimitedItem /
   ulInlineMath / ulTableEl / finalizeUnlimitedBlocks / settleUnlimitedLayout，
   DOM 类名 ul-*），两边互不引用、可各自演化：
     · `.blk.ul` 按 bbox 绝对定位；块内文字正常流式排版，字号/行高由解析侧估算
       （t.size / t.line_h，单位 pt），标题加粗放大、图注/脚注/页眉页脚小一号
       （CSS .ul-*），长段落自动换行；
     · 行内公式：解析侧给了精确的字符区间（t.math，含 LaTeX 源码）—— 原字形
       就地换成 KaTeX（ulInlineMath），正文围绕公式自然重排；
     · 独立公式（kind="formula" + latex）：KaTeX 块渲染（与其它公式块同一套收尾）；
     · 表格（kind="table" + html）：白名单渲染 <table>，不参与句子选中。
   文本块内容仍由通用 `rebuildItem` 生成 —— 选中/高亮/笔记重建后样式与公式都不丢。

   装配（reader2.js，R2Math 之后）：
     const { buildUnlimitedItem, ulInlineMath, ulLineBreaks, ulCleanNode,
             finalizeUnlimitedBlocks, settleUnlimitedLayout } = R2ItemUl({ … });
   `pages` 是共享的稳定数组（settleUnlimitedLayout 按页遍历）；`nextOrd` 是
   reader2 里的文字块序号分配器（render 时归零）。
   ============================================================================ */
window.R2ItemUl = function (env) {
  "use strict";
  const { el, splitSentences, bodyRunOf, mathBlockOf, mathSizePx,
          katexReady, katexRender, _katexBoxes, itemById, nextOrd, pages } = env;

  /* 旧数据兼容：老 doc.json 里定界符没摘（文本还留着 `$ x(t) \\in \\mathbb{R}^N $`），
     解析侧没下发 `t.math`，公式就只能看见 LaTeX 源码。这里按与后端
     `backend/backend_unlimited_ocr._text_with_math` 同样的判据**就地补一遍**：摘掉定界符、
     把字符区间写回 `t.math`，后面 ulInlineMath 就会把它换成 KaTeX。
     · 老数据可能**部分**提取过（旧判据漏掉 `$A$` 这类单字母），所以即使 `t.math`
       非空也要再扫一遍，并把老区间按同一份「旧下标 → 新下标」映射搬过来；
     · 幂等：文本里没有可摘的定界符时原样返回；公式块与表格不碰。 */
  const UL_DOLLAR_MAX = 200;
  const UL_MATH_HINT = /[\\_^{}]/;
  // 与后端 `_looks_inline_math` 同口径：含 LaTeX 命令字符 / 单个变量名 /
  // 纯数字（含 `[29]` 这种引文）/「ASCII + 运算符且没有 3 个以上连续字母」的短式子
  const UL_ALLOWED = /^[A-Za-z0-9\s\\{}_^()\[\]+=*/<>|,.'’\-]*$/;
  const UL_LONG_WORD = /[A-Za-z]{3,}/;
  const UL_NUMERIC = /^[\[(]?[\d\s.,\-–]+[\])]?$/;
  function ulLooksInlineMath(inner) {
    const core = String(inner || "").trim();
    if (!core || core.length > UL_DOLLAR_MAX) return false;
    if (UL_MATH_HINT.test(core)) return true;
    if (/^[A-Za-z]$/.test(core)) return true;
    if (UL_NUMERIC.test(core)) return true;
    if (!UL_ALLOWED.test(core) || UL_LONG_WORD.test(core)) return false;
    if (!/[A-Za-z]/.test(core)) return false;
    return /[=()\[\]+*/<>-]/.test(core);
  }
  function ulNormalizeMath(t) {
    if (!t || t.kind === "formula" || t.kind === "table") return t;
    const src = String(t.text || "");
    if (!src || (src.indexOf("$") < 0 && src.indexOf("\\(") < 0
                 && src.indexOf("\\[") < 0)) return t;
    // ⚠️ 偏移必须按**字符数**累计（后端 `_record_math` 是逐字符 extend）。
    //    早先这里拿「片段个数」当偏移 —— 多字符片段一出现区间就整体错位
    //    （表现：公式渲染出半截、正文字符被吃掉）。
    const parts = [];
    const ranges = [];
    const map = new Array(src.length);   // 旧下标 → 新下标（只记原样保留下来的字符）
    let pos = 0;
    const emitMath = (s, e, openStart, closeEnd) => {
      const raw = src.slice(s, e);
      const core = raw.trim();
      if (core) {
        const lead = raw.length - raw.trimStart().length;
        const a = pos;
        for (let k = 0; k < core.length; k++) map[s + lead + k] = pos + k;
        parts.push(core);
        pos += core.length;
        ranges.push({ a, b: pos, latex: core });
      }
      for (let k = openStart; k < s; k++) map[k] = undefined;        // 开定界符
      for (let k = e; k < closeEnd; k++) map[k] = undefined;         // 首尾空白 + 闭定界符
      return core.length;
    };
    let i = 0;
    while (i < src.length) {
      const ch = src[i];
      if (ch === "\\" && i + 1 < src.length
          && (src[i + 1] === "(" || src[i + 1] === "[")) {
        const close = src[i + 1] === "(" ? "\\)" : "\\]";
        const j = src.indexOf(close, i + 2);
        if (j >= 0) { emitMath(i + 2, j, i, j + 2); i = j + 2; continue; }
      } else if (ch === "$") {
        if (src.startsWith("$$", i)) {
          const j = src.indexOf("$$", i + 2);
          if (j >= 0) { emitMath(i + 2, j, i, j + 2); i = j + 2; continue; }
        } else {
          const j = src.indexOf("$", i + 1);
          const inner = j >= 0 ? src.slice(i + 1, j) : "";
          // 定界符内侧允许空格（在线回包的写法）
          if (j >= 0 && inner.indexOf("\n") < 0 && ulLooksInlineMath(inner)) {
            emitMath(i + 1, j, i, j + 1); i = j + 1; continue;
          }
        }
      }
      map[i] = pos;
      parts.push(ch);
      pos += 1;
      i += 1;
    }
    if (!ranges.length) return t;                  // 没摘到东西：文本别动
    const text = parts.join("");
    // 老的 math 区间（解析侧已摘过定界符的那批）按映射搬过来
    const remapped = [];
    for (const m of (Array.isArray(t.math) ? t.math : [])) {
      const a = map[m.a], b1 = map[m.b - 1];
      if (a === undefined || b1 === undefined) continue;
      if (b1 + 1 - a !== m.b - m.a) continue;      // 长度对不上：宁可不搬
      remapped.push({ a, b: b1 + 1, latex: m.latex });
    }
    t.text = text;
    t.math = remapped.concat(ranges).sort((x, y) => x.a - y.a);
    const ln = t.lines && t.lines[0];
    if (ln) ln.runs = ln.runs && ln.runs.length
      ? [Object.assign({}, ln.runs[0], { t: text })] : [{ t: text }];
    return t;
  }

  function ulTableMath(table) {
    if (!table || !katexReady()) return;
    const walker = document.createTreeWalker(table, NodeFilter.SHOW_TEXT, null);
    const nodes = [];
    let node;
    while ((node = walker.nextNode())) {
      if (node.nodeValue && (node.nodeValue.indexOf("$") >= 0
          || node.nodeValue.indexOf("\\(") >= 0)) nodes.push(node);
    }
    const delim = /\\\(([^]*?)\\\)|\$\$([^]*?)\$\$|\$([^$\n]{1,200}?)\$/g;
    for (const tn of nodes) {
      const source = tn.nodeValue || "";
      const parts = [];
      let last = 0, match;
      delim.lastIndex = 0;
      while ((match = delim.exec(source))) {
        const latex = (match[1] != null ? match[1]
          : (match[2] != null ? match[2] : match[3])).trim();
        if (!latex || (match[3] != null && !ulLooksInlineMath(latex))) continue;
        if (match.index > last) parts.push(document.createTextNode(source.slice(last, match.index)));
        const rendered = document.createElement("span");
        rendered.className = "katex-inline";
        if (katexRender(rendered, latex, false)) parts.push(rendered);
        else parts.push(document.createTextNode(match[0]));
        last = match.index + match[0].length;
      }
      if (last === 0) continue;
      if (last < source.length) parts.push(document.createTextNode(source.slice(last)));
      const frag = document.createDocumentFragment();
      parts.forEach(part => frag.appendChild(part));
      tn.parentNode.replaceChild(frag, tn);
    }
  }

  function buildUnlimitedItem(t, S, pageEl, pi) {
    t = ulNormalizeMath(t);                        // 旧数据：就地补 math 区间
    const bx = t.x * S, by = t.y * S;
    const blk = el("div", "blk ul");
    if (t.kind) blk.classList.add("ul-" + t.kind);
    if (t.kind === "heading") blk.classList.add("ul-l" + (t.level || 2));
    Object.assign(blk.style, { left: bx + "px", top: by + "px", width: t.w * S + "px" });
    blk.dataset.id = t.id;
    // 对齐方式：解析侧下发 t.align；旧/缺字段时按同一规则本地兜底 ——
    // 正文类（text/reference/caption/footnote）两端对齐（CSS [data-align="justify"]），
    // 行右缘像原 PDF（LaTeX justified 排版）一样齐平到栏边界；标题/页眉页脚保持左对齐。
    blk.dataset.align = (t.align === "justify" || t.align === "left"
                         || t.align === "center" || t.align === "right")
      ? t.align
      : ((t.kind === "text" || t.kind === "reference" ||
          t.kind === "caption" || t.kind === "footnote") ? "justify" : "left");

    const size = t.size > 0 ? t.size : 10;        // 解析侧估的字号（pt）
    blk.style.fontSize = size * S + "px";
    // 竖排文本（旋转 90° 的页眉/水印）：模型不给旋转信息，用“又高又窄 + 短文本”判
    // —— 内容整体旋转、沿条带自下而上排（见 CSS .ul-vertical）
    if (t.h > t.w * 3 && (t.text || "").length >= 2 && (t.text || "").length <= 80) {
      blk.classList.add("ul-vertical");
    }

    const mb = mathBlockOf(t);                     // kind="formula" + latex → KaTeX 块
    const linesData = [];
    let contentH = t.h * S;        // 初值=bbox 高；挂到文档后由 finalizeUnlimitedBlocks 回填实测高
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
      box._origW = t.w * S;                        // 公式主体宽 = OCR 给的外框宽
      if (box._hasTag) box.style.width = "100%";
      blk.appendChild(box);
      _katexBoxes.add(box);
    } else if (t.kind === "table") {
      const tb = ulTableEl(t.html);
      if (tb) {
        ulTableMath(tb);
        blk.appendChild(tb);
      } else {
        blk.appendChild(el("div", "ln ulln", t.text || ""));
      }
    } else {
      // 文本块：唯一一条“流式行”。类名要含 ln（行内公式的让位机制按 .ln 找行），
      // `ld.free` 让 fitLineEl 跳过（这里的宽度是排版自然宽，不是 PDF 行框）。
      const lineEl = el("div", "ln ulln");
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
      ord: nextOrd(), unlimited: true,
      ulHeading: t.kind === "heading",   // 标题：finalizeUnlimitedBlocks 里按需放宽不折行
    };
    item.canonical = linesData.map(ld => ld.text + "\n").join("");
    item.bodyRun = bodyRunOf(linesData)
      || { s: size, fam: "serif", i: false, b: false, up: false };
    // 行内公式：解析侧给的是 text 里的**精确字符区间**（比按文本定界符猜稳）。
    // 渲染交给 ulInlineMath：`.mspan` 原字形就地换成 KaTeX（见 rebuildItem 末尾）；
    // 公式块不重复渲染（会叠重影）。
    item.inlineMath = (!mb && linesData.length && t.math && t.math.length)
      ? ulMathRanges(t, item.canonical) : null;
    item.sents = mb ? [] : splitSentences(item.canonical).map((sg, si) =>
      ({ si, s: sg.s, e: sg.e, key: item.id + "#" + si,
        text: item.canonical.slice(sg.s, sg.e).replace(/\n+/g, " ").trim() }));
    // 表格没有可切句的 .rn 结构：点它不该弹“翻译/标注”工具条（点击分支按这个清选中）
    if (t.kind === "table") item.noSelect = true;

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

  /* Unlimited-OCR 块的行内数学区间（解析侧给的 text 偏移）→ 覆盖层用的 {a,b,latex}。
     越界/空的丢掉；区间相对 `t.text`，canonical 只在行末多一个 '\n'，不影响行内区间。 */
  function ulMathRanges(t, canonical) {
    const out = [];
    for (const m of (t.math || [])) {
      const a = m && m.a | 0, b = m && m.b | 0;
      const latex = String((m && m.latex) || "").trim();
      if (!latex || !(b > a) || a < 0 || b > canonical.length) continue;
      out.push({ a, b, latex });
    }
    return out.length ? out : null;
  }

  /* Unlimited-OCR 块的行内公式：把 `.mspan` 原字形**就地换成 KaTeX**。
     不走 .mathbox.inline 覆盖层（那套是给“逐行绝对定位、行宽固定”的 PyMuPDF 版面
     设计的）；流式排版里行会重排，就地渲染后正文围绕真公式自然重排，宽度与基线
     都交给浏览器。渲染失败（KaTeX 未就绪 / LaTeX 有错）就保持原字形 —— 至少可读，
     不会整段空白。`rebuildItem` 每次重建都会重新走一遍（幂等）。 */
  function ulInlineMath(item) {
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

  /* Unlimited-OCR 块的块内换行：canonical 里的 '\n'（作者信息换行、列表项等）在
     HTML 流里会被折叠成空格，不还原就连成一行。把 `\n` 拆成 <br> —— 只动文本节点，
     span 上的 data-g / 高亮 class 都保持原样（选句/高亮仍按字符区间走）。
     KaTeX 子树跳过（MathML 文本节点里的换行不动它）。 */
  function ulLineBreaks(item) {
    const line = item.el && item.el.querySelector(".ulln");
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

  /* Unlimited-OCR 的表格 HTML → 安全 DOM（白名单）。
     HTML 来自外部推理服务，绝不直接 innerHTML：只放行表格结构与基础行内标签，
     属性只留 colspan/rowspan（表格布局必需）。拿不到 <table> 时返回 null。 */
  const UL_TABLE_TAGS = { TABLE: 1, THEAD: 1, TBODY: 1, TFOOT: 1, TR: 1, TD: 1, TH: 1,
                          CAPTION: 1, B: 1, I: 1, EM: 1, STRONG: 1, SUP: 1, SUB: 1,
                          BR: 1, SPAN: 1 };
  function ulTableEl(html) {
    const s = String(html || "");
    if (!s || s.indexOf("<table") < 0) return null;
    let doc = null;
    try { doc = new DOMParser().parseFromString(s, "text/html"); }
    catch (e) { return null; }
    const src = doc && doc.body && doc.body.querySelector("table");
    if (!src) return null;
    const wrap = el("div", "ultable");
    wrap.appendChild(ulCleanNode(src));
    return wrap;
  }
  function ulCleanNode(node) {
    if (node.nodeType === 3) return document.createTextNode(node.nodeValue);
    if (node.nodeType !== 1) return document.createTextNode("");
    const tag = node.tagName.toUpperCase();
    if (!UL_TABLE_TAGS[tag]) {          // 不认识的标签：剥壳、保留里面的内容
      const frag = document.createDocumentFragment();
      for (const c of node.childNodes) frag.appendChild(ulCleanNode(c));
      return frag;
    }
    const out = document.createElement(tag.toLowerCase());
    if (tag === "TD" || tag === "TH") {
      for (const name of ["colspan", "rowspan"]) {
        const v = parseInt(node.getAttribute(name), 10);
        if (v > 1 && v <= 100) out.setAttribute(name, String(v));
      }
    }
    for (const c of node.childNodes) out.appendChild(ulCleanNode(c));
    return out;
  }

  /* Unlimited-OCR 块（流式排版）收尾：内容高度要等挂到文档上才量得准 —— 回填
     item.contentH / `.blk` 高度 / 译文卡位置（relayout 的避让都按 contentH 算）。
     **只放大不缩小**：文本量比原框多时按实际高度长出去（解析侧已按块框做过
     缩字适配，溢出很小）。表格和公式按实际渲染高度量取，避免内容溢出 OCR 框后
     与后续块重叠。 */
  function finalizeUnlimitedBlocks(only) {
    for (const id in itemById) {
      const it = itemById[id];
      if (!it.unlimited || it.kind !== "blk") continue;
      if (only && !only.has(it.pi)) continue;
      if (it.ulHeading) {
        // 标题优先保持单行并适度扩宽，但不能越过页面右边缘；超长时由块内文本自动换行。
        const lineEl = it.linesData[0].el;
        const prevW = lineEl.style.width;
        lineEl.style.width = "max-content";     // 量单行自然宽（块级盒 max-content 有效）
        const natural = lineEl.offsetWidth;
        lineEl.style.width = prevW;
        const pageWidth = it.el.parentElement.clientWidth;
        const maxWidth = Math.max(1, pageWidth - it.x);
        const preferredWidth = natural > it.w + 1
          ? Math.min(natural + 4, it.w * 2.4)  // +4px 避免宽度取整导致意外折行
          : it.w;
        it.el.style.width = Math.min(preferredWidth, maxWidth).toFixed(1) + "px";
      }
      let h = 0;
      for (const ld of it.linesData) h = Math.max(h, ld.el.offsetHeight + (ld.dy || 0));
      if (!it.linesData.length) {
        const content = it.el.querySelector(".ultable, .mathbox, .ulln");
        if (content) h = content.offsetHeight;
      }
      h = Math.max(h, it.h);
      it.contentH = h;
      it.el.style.height = h + "px";
      if (it.exp) it.exp.style.top = h + "px";
    }
  }

  /* Unlimited-OCR 内容块与图片的二维碰撞避让：先尝试向下、向左或向右移动当前项，
     选择位移最小且不越出页面的方向。图片与文字/表格都参与，避免内容重叠。
     ⚠️ 位移量每轮都**基于原始 it.y 重算**（且不写回数据）：缩放/窗口变化会
     整页重建，每次都以原始几何为基准，不会累积漂移；应用交给 relayoutPage 的
     位移链（ulShift / ulXShift 是它的起点，与“卡片顶开”量叠加）。与 Surya 的 syShift 各自
     独立（字段、类名都不共用），互不干涉。 */
  const UL_GAP = 2;       // px：避让后相邻内容之间至少保留的空隙
  // OCR may emit equation numbers as independent formula blocks at the paragraph's original y.
  function unlimitedEquationTagMatches(page) {
    const items = page.items.filter(it => it.unlimited && it.kind === "blk");
    const tags = items.filter(it => /^\(\d+\)$/.test(it.latex.trim()));
    const formulas = items.filter(it => it.latex && !/^\(\d+\)$/.test(it.latex.trim()));
    const textBlocks = items.filter(it => it.el.classList.contains("ul-text")
      && it.w >= page.w * 0.4);
    const claimed = new Set();
    const matches = [];
    for (const tag of tags) {
      const candidates = [];
      for (const formula of formulas) {
        const formulaCenterX = formula.x + formula.w / 2;
        const formulaCenterY = formula.y + (formula.contentH || formula.h) / 2;
        for (const block of textBlocks) {
          if (formulaCenterX < block.x || formulaCenterX > block.x + block.w) continue;
          if (Math.abs(formulaCenterX - (block.x + block.w / 2)) > block.w * 0.15) continue;
          const columnRight = block.x + block.w;
          if (page.w - columnRight < tag.w * 0.5) continue;
          const tagRight = tag.x + tag.w;
          if (Math.abs(tagRight - columnRight) > page.w * 0.1) continue;
          const distance = Math.abs(tag.y + tag.h / 2 - formulaCenterY);
          if (distance > page.w * 0.18 || claimed.has(formula)) continue;
          candidates.push({ formula, block, distance });
        }
      }
      candidates.sort((a, b) => a.distance - b.distance);
      const match = candidates[0];
      if (!match) continue;
      claimed.add(match.formula);
      matches.push({ tag, ...match });
    }
    return matches;
  }

  function settleUnlimitedLayout(only) {
    for (let pi = 0; pi < pages.length; pi++) {
      if (only && !only.has(pi)) continue;
      const page = pages[pi];
      const equationTags = unlimitedEquationTagMatches(page);
      const equationTagSet = new Set(equationTags.map(match => match.tag));
      const list = page.items.filter(it => it.unlimited &&
        !(it.kind === "blk" && (it.el.classList.contains("ul-vertical")
          || equationTagSet.has(it))));
      list.sort((a, b) => (a.y - b.y) || (a.x - b.x));
      const settled = [];
      for (const it of list) {
        const width = it.kind === "blk" ? Math.max(it.w, it.el.offsetWidth) : it.w;
        const height = it.kind === "blk" ? Math.max(it.contentH || 0, it.el.offsetHeight)
                                         : it.h;
        const overlaps = (x, y, other) =>
          x < other.x + other.w && x + width > other.x &&
          y < other.y + other.h && y + height > other.y;
        const place = direction => {
          let x = it.x, y = it.y;
          for (let tries = 0; tries <= settled.length; tries++) {
            const hit = settled.find(other => overlaps(x, y, other));
            if (!hit) return { x, y };
            if (direction === "down") {
              y = hit.y + hit.h + UL_GAP;
            } else if (direction === "right") {
              x = hit.x + hit.w + UL_GAP;
              if (x + width > page.w) return null;
            } else {
              x = hit.x - width - UL_GAP;
              if (x < 0) return null;
            }
          }
          return null;
        };
        const candidates = [
          place("down"),
          place("left"),
          place("right"),
        ].filter(Boolean);
        candidates.sort((a, b) =>
          (Math.abs(a.x - it.x) + a.y - it.y) -
          (Math.abs(b.x - it.x) + b.y - it.y));
        const pos = candidates[0] || { x: it.x, y: it.y };
        it.ulXShift = pos.x - it.x;
        it.ulShift = pos.y - it.y;
        settled.push({ x: pos.x, y: pos.y, w: width, h: height });
      }
      // Apply after collision settlement so a moved equation and its tag stay together.
      for (const { tag, formula, block } of equationTags) {
        const columnRight = block.x + (block.ulXShift || 0) + block.w;
        const formulaY = formula.y + (formula.ulShift || 0);
        tag.ulXShift = columnRight - tag.w - tag.x;
        tag.ulShift = formulaY + (formula.contentH || formula.h) / 2 - tag.h / 2 - tag.y;
      }
    }
  }

  /* ============ 对外接口 ============ */
  return {
    buildUnlimitedItem, ulInlineMath, ulLineBreaks, ulTableEl, ulCleanNode,
    finalizeUnlimitedBlocks, settleUnlimitedLayout,
  };
};
