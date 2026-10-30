/* ============ 选中（句子级，可多句）：从 reader2.js 拆出来的模块 ============
   本模块管“选中”的全部交互：点/拖拽取句、公式框（页面级 .fbox / 行内 KaTeX）的
   句子锚定、选中态重绘（`.sel` 与 `formula-sel`），以及挂在 pagesEl / document
   上的鼠标事件。

   跨模块共享的东西全部走 `env`（装配见 reader2.js）：
     · sel       读 + 写（整体替换）→ 装配时用 getter + setter，改的还是主作用域的 `sel`
     · DOC       只读，换论文会整体替换 → getter
     · pages / pagesEl / itemById / GZ   稳定引用
     · findItem / sentOf / sentRange / sentKey / lineOfChar   r2-text 与主作用域
     · rebuildItem / setAnnoRanges / paintFormulaMarks        重建与重绘（主作用域）
     · placeBar / updateBar / hideBar                         工具条段（装配时还在 TDZ，转发调用）
   ============================================================================ */
window.R2Selection = function (env) {
  "use strict";

  /* ---- 选中：句子级(可多句) ---- */
  function selCount() { return env.sel ? env.sel.sents.length : 0; }
  function selAnchor() { return env.sel && env.sel.sents.length ? env.sel.sents[0] : null; }   // 工具条/笔记卡锚定的那一句
  function selItems() {                                                            // 去重后的文字块(保持顺序)
    const out = [];
    if (env.sel) for (const r of env.sel.sents) if (out.indexOf(r.item) < 0) out.push(r.item);
    return out;
  }
  function selKeys() { return env.sel ? env.sel.sents.map(r => env.sentKey(r.item, r.si)) : []; }
  function isSelRef(item, si) {
    return !!env.sel && env.sel.sents.some(r => r.item === item && r.si === si);
  }
  function ordOf(ref) { return ref.item.ord * 1e6 + ref.si; }
  // 阅读顺序里 a→b 之间(含两端)的**整句**，跨块、跨页都支持
  function refsBetween(a, b) {
    const lo = ordOf(a) <= ordOf(b) ? a : b;
    const hi = lo === a ? b : a;
    const out = [];
    for (const pg of env.pages) {
      for (const it of pg.items) {
        if (it.kind !== "blk" || !it.sents.length) continue;
        if (it.ord < lo.item.ord || it.ord > hi.item.ord) continue;
        const from = it === lo.item ? lo.si : 0;
        const to = it === hi.item ? hi.si : it.sents.length - 1;
        for (let si = from; si <= to; si++) out.push({ item: it, si });
      }
    }
    return out.length ? out : [{ item: lo.item, si: lo.si }];
  }
  function pageOfItem(item) {
    for (let pi = 0; pi < env.pages.length; pi++)
      if (env.pages[pi].items.indexOf(item) >= 0) return pi + 1;
    return "";
  }

  /* ================= 选中句子 ================= */
  /* 点在行内公式上：Surya / Unlimited-OCR 的行内公式被就地换成了 `.katex-inline`
     （KaTeX 子树，**没有 data-g**），点它时 caretInfo / refFromNode 拿不到字符下标，
     只能退回“按纵向位置估句子”——多句段落里这一退就退成第一句（实测：点第二句里的
     `Δ > 0`，选中的是第一句）。这里用解析侧下发的 `item.inlineMath[mi]`（按 canonical
     计的字符区间）折算出下标：起点取 a，拖拽终点取 b-1（停在公式末尾时仍属于同一句）。 */
  function mathCaret(node, isEnd) {
    const el = node && node.nodeType === 3 ? node.parentElement : node;
    const host = el && el.closest ? el.closest(".katex-inline[data-mi]") : null;
    if (!host) return null;
    const blkEl = host.closest(".blk");
    const item = blkEl && env.findItem(blkEl.dataset.id);
    if (!item) return null;
    const f = (item.inlineMath || [])[parseInt(host.dataset.mi, 10)];
    if (!f || !(f.b > f.a)) return null;
    return { item, idx: isEnd ? f.b - 1 : f.a };
  }

  function caretInfo() {
    const s = window.getSelection();
    if (!s || !s.anchorNode) return null;
    const math = mathCaret(s.anchorNode, false);
    if (math) return math;
    const node = s.anchorNode.nodeType === 3 ? s.anchorNode.parentElement : s.anchorNode;
    const gEl = node && node.closest ? node.closest("[data-g]") : null;
    if (!gEl) return null;
    const blkEl = gEl.closest(".blk");
    const item = blkEl && env.findItem(blkEl.dataset.id);
    if (!item) return null;
    let off = 0;
    if (s.anchorNode.nodeType === 3) off = s.anchorOffset || 0;
    return { item, idx: (parseInt(gEl.dataset.g, 10) || 0) + off };
  }

  function fallbackSentence(item, y) {
    // 依据点击的纵向位置，估算句子
    if (!item.sents.length) return 0;
    let best = 0, bestD = 1e9;
    for (let si = 0; si < item.sents.length; si++) {
      const r = approxSentenceRect(item, si);
      const d = y < r.top ? r.top - y : (y > r.bottom ? y - r.bottom : 0);
      if (d < bestD) { bestD = d; best = si; }
    }
    return best;
  }
  function approxSentenceRect(item, si) {
    const rg = env.sentRange(item, si);
    const l0 = item.linesData[env.lineOfChar(item, rg.a)].el;
    const l1 = item.linesData[env.lineOfChar(item, Math.max(rg.a, rg.b - 1))].el;
    const r0 = l0.getBoundingClientRect(), r1 = l1.getBoundingClientRect();
    return { top: r0.top, bottom: r1.bottom };
  }

  /* 点在「行与行之间的空隙 / 行尾空白 / 块框里没画字的地方」时 e.target 是 .page
     ——`.blk` 容器不吃指针事件（见 style.css 的说明），所以这里按几何位置找回最近的一行。
     入参是**布局坐标**（页内 px）；返回 { item, y }，离得太远(超出 HIT_SLOP)则 null。 */
  const HIT_SLOP = 12;                 // 容差：约一行内距（正文 12pt），超过就算点了空白
  function nearestLineAt(x, y) {
    let best = null, bestD = Infinity;
    for (const id in env.itemById) {
      const it = env.itemById[id];
      if (it.kind !== "blk" || !it.linesData || !it.linesData.length) continue;
      for (const ld of it.linesData) {
        const r = ld.el.getBoundingClientRect();
        if (!r.width) continue;
        // ⚠️ 两个方向都要在容差内。只给纵向容差的话，「横向恰好落在远处那一行上」
        // （比如页脚那块只有一两个字的块）会被当成最近的一行选中。
        if (x < r.left - HIT_SLOP || x > r.right + HIT_SLOP ||
            y < r.top - HIT_SLOP || y > r.bottom + HIT_SLOP) continue;
        const dx = Math.max(r.left - x, 0, x - r.right);       // 框内算 0
        const dy = Math.max(r.top - y, 0, y - r.bottom);
        const d = dx + dy * 2;                                 // 纵向更敏感：宁可换行也别换块
        if (d < bestD) { bestD = d; best = { item: it, y }; }
      }
    }
    return best;
  }

  /* 页面级公式框覆盖层（.fbox）的句子锚定。
     DOM 由 r2-math.js 的 `buildFormulaBoxes` 按 `pages[].formula_boxes` 创建；
     这里把解析侧下发的 `copy[]`（公式落在哪个文字块的哪个字符区间）接到
     块的 `formulaBoxes` 上 —— 选句高亮、复制还原 LaTeX、公式框跟随位移都用它。 */
  function attachFormulaBoxes(page, pageEl) {
    if (!env.DOC || env.DOC.parser !== "pymupdf") return;
    const metas = page.formula_boxes || [];
    if (!metas.length) return;
    const pi = env.pages.indexOf(page);
    const byId = new Map();
    pageEl.querySelectorAll(".fbox").forEach(fb => byId.set(String(fb.dataset.boxId), fb));
    for (const meta of metas) {
      const fb = byId.get(String(meta.id));
      if (!fb) continue;
      const copies = Array.isArray(meta.copy) ? meta.copy : [];
      const first = copies.find(c => c && c.id && c.end > c.start);
      let item = first ? env.findItem(first.id) : null;
      let a = Infinity, b = -Infinity;
      if (item) {
        for (const c of copies) {
          if (!c || c.id !== first.id || !(c.end > c.start)) continue;
          a = Math.min(a, c.start); b = Math.max(b, c.end);
        }
        if (b > a) {
          fb._item = item;
          fb._copyRange = { start: a, end: b };
          item.formulaBoxes = item.formulaBoxes || [];
          if (item.formulaBoxes.indexOf(fb) < 0) item.formulaBoxes.push(fb);
        } else {
          item = null;
        }
      }
      /* 选句锚点：
         ⚠️ `copy[]` 只说明“这个占位 run 落在某个块的某一行里”，**不代表那一行有正文**。
         分离式公式（∑ 的上下限、分式分母/分子）常被 PyMuPDF 归到别的段落的块里，
         整行只剩占位空格；照 copy 选句就会选中那条“宿主段落”——点一个公式，
         另一行/另一段的句子（以及同块里所有公式框）会一起被选中/描框。
         所以：copy 所在行**有正文**才信 copy；否则按公式框自己的 bbox 找最近的
         **有正文的行**（`nearestSentenceRef`），那才是公式视觉上所在的那一句。 */
      let ref = null;
      if (item && item.sents.length && b > a) {
        const ld = item.linesData[env.lineOfChar(item, a)];
        if (ld && /\S/.test(ld.text || "")) ref = { item, si: env.sentOf(item, a) };
      }
      if (!ref) ref = nearestSentenceRef(meta, pi);
      if (ref) fb._selectRef = ref;
    }
  }

  /* 公式框（PDF 坐标 bbox）→ 最近的一行**有正文**的文字 → 该行所在句。
     ⚠️ 只用 `ld.ln` 的 PDF 坐标：块的 `item.x/y/w/h` 已经乘过显示缩放 `S`，
     与公式 bbox（pt）混用会偏；整行只有占位的行不当锚点（见 attachFormulaBoxes）。 */
  function nearestSentenceRef(meta, pi) {
    const box = meta && meta.bbox;
    const pg = env.pages[pi];
    if (!box || box.length !== 4 || !pg) return null;
    const bx0 = Number(box[0]) || 0, by0 = Number(box[1]) || 0;
    const bx1 = Number(box[2]) || 0, by1 = Number(box[3]) || 0;
    const cy = (by0 + by1) / 2;
    let best = null, bestD = Infinity;
    for (const cand of pg.items) {
      if (cand.kind !== "blk" || !cand.sents.length) continue;
      for (const ld of cand.linesData || []) {
        const ln = ld.ln;
        if (!ln || !/\S/.test(ld.text || "")) continue;   // 空占位行不参与
        const lx = Number(ln.x) || 0, ly = Number(ln.y) || 0;
        const lw = Number(ln.w) || 0, lh = Number(ln.h) || 0;
        // ⚠️ 用**行中心线**的纵向距离，不用「框到行的空隙」：分式/上下限的 bbox
        // 会正好压在下一段首行的顶边上（实测 a/(1-r) 的底边 = 下一段首行 y），
        // 按空隙算会 0 距离抢走下一段；按中心线它差着大半行，能正确落在本行。
        const dy = Math.abs(cy - (ly + lh / 2));
        const dx = Math.max(lx - bx1, 0, bx0 - (lx + lw));
        const d = dy * 2 + dx;                             // 纵向更敏感
        if (d < bestD) { bestD = d; best = { item: cand, si: env.sentOf(cand, ld.gs) }; }
      }
    }
    return best;
  }

  function formulaSelectionTarget(target) {
    if (!target) return null;
    if (target._selectRef) return { host: target, ref: target._selectRef };
    const blk = target.closest(".blk");
    const item = blk && env.findItem(blk.dataset.id);
    if (!item) return null;
    if (target.matches(".mathbox.inline")) {
      const formula = item.inlineMath && item.inlineMath[parseInt(target.dataset.mi, 10)];
      if (formula && item.sents.length)
        return { host: target, ref: { item, si: env.sentOf(item, formula.a) }, inline: true };
    }
    if (item.sents.length)
      return { host: target, ref: { item, si: fallbackSentence(item, item.y + item.h / 2) } };
    const page = env.pages[item.pi];
    if (!page) return null;
    const r = target.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    let nearest = null, nearestD = Infinity;
    for (const candidate of page.items) {
      if (candidate.kind !== "blk" || !candidate.sents.length) continue;
      for (const line of candidate.linesData) {
        const lr = line.el.getBoundingClientRect();
        const dx = Math.max(lr.left - cx, 0, cx - lr.right);
        const dy = Math.max(lr.top - cy, 0, cy - lr.bottom);
        const d = dx + dy * 2;
        if (d < nearestD) {
          nearestD = d;
          nearest = { item: candidate, si: env.sentOf(candidate, line.gs) };
        }
      }
    }
    return nearest ? { host: target, ref: nearest } : null;
  }

  /* ---- 应用/清除选中高亮（可多句、跨块跨页） ---- */
  function applySelRanges() {
    document.querySelectorAll(".fbox.formula-sel").forEach(box => box.classList.remove("formula-sel"));
    for (const id in env.itemById) {
      const it = env.itemById[id];
      if (it.selRanges && it.selRanges.length) { it.selRanges = []; env.rebuildItem(it); }
    }
    if (!env.sel) return;
    const byItem = new Map();
    if (!env.sel.formulaOnly) {
      for (const r of env.sel.sents) {
        if (!byItem.has(r.item)) byItem.set(r.item, []);
        byItem.get(r.item).push(env.sentRange(r.item, r.si));
      }
    }
    for (const [it, ranges] of byItem) { it.selRanges = ranges; env.rebuildItem(it); }
    if (env.sel.formulaBox && env.sel.formulaBox.classList.contains("fbox"))
      env.sel.formulaBox.classList.add("formula-sel");
    /* 公式框标亮只认**选句锚点**（`_selectRef`）。旧写法按 `_copyRange` 与句子区间
       相交判断：分离式公式的占位落在别的段落块里时，点一个公式会把另一行的公式框
       也描上蓝框（`p3s1` 的尾部空行就是这类“孤儿占位”）。 */
    for (const id in env.itemById) {
      for (const box of env.itemById[id].formulaBoxes || []) {
        const anchor = box._selectRef;
        if (anchor && env.sel.sents.some(r => r.item === anchor.item && r.si === anchor.si))
          box.classList.add("formula-sel");
      }
    }
  }
  function setSelection(refs, silent, formulaBox) {
    if (!refs || !refs.length) { clearSelection(); return; }
    const formulaOnly = !!formulaBox && !formulaBox.matches(".mathbox.inline") &&
      formulaBox.dataset.cls !== "InlineFormula";
    env.sel = { sents: refs.slice(), formulaBox: formulaBox || null, formulaOnly };
    applySelRanges();
    if (!silent) { env.updateBar(); env.placeBar(); }
  }
  function clearSelection() {
    env.sel = null;
    applySelRanges();
    env.hideBar();
  }
  // 重新套用标注(高亮/笔记下划线)并保留选中高亮
  function refreshSelectionVisual() {
    for (const it of selItems()) { env.setAnnoRanges(it); env.rebuildItem(it); }
    env.paintFormulaMarks();        // 高亮刚改过：公式框也要跟着变（新高亮/取消高亮）
  }
  function selectSentence(item, si) {
    if (!item) return;
    setSelection([{ item, si }]);
  }

  /* ---- 鼠标拖拽多选：拖过连续句子 → 整段（句子级）选中 ---- */
  function refFromNode(node, offset, isEnd) {
    if (!node) return null;
    const el = node.nodeType === 3 ? node.parentElement : node;
    if (!el || !el.closest) return null;
    const blk = el.closest(".blk");
    if (!blk || !env.pagesEl.contains(blk)) return null;
    const item = env.findItem(blk.dataset.id);
    if (!item) return null;
    let g = null;
    const span = el.closest(".rn");
    if (span && span.dataset.g != null) g = parseInt(span.dataset.g, 10) + (offset || 0);
    else {
      // 落在行内公式上（.katex-inline 没有 data-g）→ 用公式的字符区间定位
      const math = mathCaret(node, isEnd);
      if (math) g = math.idx;
    }
    if (g == null) return null;
    return { item, si: env.sentOf(item, isEnd ? Math.max(0, g - 1) : g) };
  }
  function refsFromNativeSelection() {
    const s = window.getSelection();
    if (!s || s.isCollapsed || !s.rangeCount) return null;
    const r = s.getRangeAt(0);
    const a = refFromNode(r.startContainer, r.startOffset, false);
    const b = refFromNode(r.endContainer, r.endOffset, true);
    if (!a || !b) return null;
    return refsBetween(a, b);
  }
  /* 按点取字符位置：浏览器自带的「坐标 → 最近的字符」。一次只看一个点，
     不受拖拽路径影响（原生选区会被途中的空白重置，见 mouseup 的说明）。
     入参是**视觉坐标**(clientX/clientY)，与 elementFromPoint 同口径。 */
  function caretAtPoint(cx, cy) {
    if (document.caretRangeFromPoint) {
      const r = document.caretRangeFromPoint(cx, cy);
      return r ? { node: r.startContainer, off: r.startOffset } : null;
    }
    if (document.caretPositionFromPoint) {          // Firefox
      const p = document.caretPositionFromPoint(cx, cy);
      return p ? { node: p.offsetNode, off: p.offset } : null;
    }
    return null;
  }
  function refAtPoint(cx, cy, isEnd) {
    const c = caretAtPoint(cx, cy);
    const r = c && refFromNode(c.node, c.off, isEnd);
    if (r) return r;
    // 没命中到字符（点在空白/行尾外）→ 按几何位置找最近的一行
    const near = nearestLineAt(cx / env.GZ(), cy / env.GZ());
    return near ? { item: near.item, si: fallbackSentence(near.item, near.y) } : null;
  }
  let dragging = false, formulaSelecting = false, dragFrom = null, dragAppliedAt = 0;
  env.pagesEl.addEventListener("mousedown", e => {
    if (e.button !== 0) return;
    if (e.target.closest(".fbox, .mathbox.inline, .blk.has-math .mathbox")) {
      formulaSelecting = true;
      dragging = false;
      dragFrom = null;
      return;
    }
    formulaSelecting = false;
    dragging = true;
    // 拖拽起点：mouseup 时按几何位置自己算范围（不信任原生选区，见下）
    dragFrom = e.target.closest(".img, .pagenum") ? null : { x: e.clientX, y: e.clientY };
  });
  document.addEventListener("mouseup", e => {
    if (formulaSelecting) {
      formulaSelecting = false;
      return;
    }
    if (!dragging) return;
    dragging = false;
    const from = dragFrom; dragFrom = null;
    const moved = !!from && Math.abs(e.clientX - from.x) + Math.abs(e.clientY - from.y) > 3;
    // ⚠️ 不能只信原生选区：拖拽途中一旦指针扫过「没有文字的空白」（行间空隙、公式空位、
    // 相邻块的空白框），浏览器会把原生选区的锚点重置到那一块 —— 起点所在的块就丢了
    // （复现：从 p1b45 拖到 p1b47，最后只选中 p1b47）。所以拖动过就按两端的**坐标**
    // 各取一句，再取阅读顺序里两者之间的整句区间。
    let refs = null;
    if (moved && !(e.target && e.target.closest && e.target.closest(".img, .pagenum"))) {
      const a = refAtPoint(from.x, from.y, false);
      const b = refAtPoint(e.clientX, e.clientY, true);
      if (a && b) refs = refsBetween(a, b);
    }
    if (!refs) refs = refsFromNativeSelection();   // 没拖动 / 取不到端点 → 退回原生选区
    if (!refs) return;                             // 没拖出句子范围 → 交给 click 当单句选中
    setSelection(refs);                            // 首尾 → 吸附到整句
    dragAppliedAt = Date.now();
    const s = window.getSelection(); if (s && s.removeAllRanges) s.removeAllRanges();
  });

  env.pagesEl.addEventListener("click", e => {
    if (Date.now() - dragAppliedAt < 350) return;   // 刚用拖拽选完，别被 click 收成单句
    const formula = e.target.closest(".fbox, .mathbox.inline, .blk.has-math .mathbox");
    if (formula) {
      const target = formulaSelectionTarget(formula);
      if (target) setSelection([target.ref], false, target.host);
      else clearSelection();
      return;
    }
    // 图 / 页码不是文字：点它们照旧清掉选中
    if (e.target.closest(".img, .pagenum")) { clearSelection(); return; }
    const blk = e.target.closest(".blk");
    let item = null, hitY = e.clientY / env.GZ();       // hitY 是布局坐标(见文件顶部坐标说明)
    if (blk) {
      item = env.findItem(blk.dataset.id);
    } else {
      // 块框里没画字的地方（行间空隙、行尾空白、公式空位）target 是 .page：
      // 按几何位置找回最近的一行继续选，别平白把已有的选中清掉。
      const near = nearestLineAt(e.clientX / env.GZ(), hitY);
      if (near) { item = near.item; hitY = near.y; }
    }
    if (!item || item.noSelect) { clearSelection(); return; }   // noSelect：表格等无句子块
    const ci = caretInfo();
    const si = (ci && ci.item === item) ? env.sentOf(item, ci.idx) : fallbackSentence(item, hitY);
    // Shift+点击：从当前选区首句扩展到点中的这句
    if (e.shiftKey && selCount()) setSelection(refsBetween(selAnchor(), { item, si }));
    else selectSentence(item, si);
    const s = window.getSelection(); if (s && s.removeAllRanges) s.removeAllRanges();
  });

  /* ============ 对外接口 ============ */
  return {
    selCount, selAnchor, selItems, selKeys, isSelRef, refsBetween, pageOfItem,
    setSelection, clearSelection, selectSentence, refreshSelectionVisual,
    attachFormulaBoxes, applySelRanges,
  };
};
