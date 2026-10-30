/* 引用跳转（PDF 内部链接 → 参考文献卡片）
   ────────────────────────────────────────────────────────────────
   数据来自 `GET /api/doc/{id}/refs`（后端 backend/refs.py 算好，零 LLM 成本）：
     · entries[i] = { i, n, page, blocks[块id…], text, uri?, label? }   参考文献条目
       （编号制 n 为编号；作者-年份制 n=null，label 是 `[Author, Year]`）
     · links[]    = { page, block, start, end, token, refs[条目下标], nums }
       start/end 是**块 canonical 文本**里的字符区间（与句子/高亮/笔记同口径）。
   本模块做两件事：
     1) 把正文里对应的 `[12]` 字符区间包成 `.cite` 标记。块会被反复重建（选句/高亮/
        缩放都会走 reader2.rebuildItem），所以标记做成**幂等**：每次重建后重打一遍；
        ⚠️ 一条引用常常跨多个 `[data-g]` span（解析侧按颜色切 run：链接色的引用是
        单独一段；`[` / `12` / `]` 也可能落在不同字体段）—— 同一行内连续的几段会被
        收进**同一个** `.cite`，于是一组引用（多作者 / 多编号）点击、悬停都是一个整体；
     2) 点击 `.cite` → 弹一张卡片：条目原文 + 「定位原文」（滚到参考文献那条并闪烁）
        + 「复制」+ 条目带外链时加「打开链接」。
   交互上有一处必须抢：点击发生在**捕获阶段**（root 上）并 stopPropagation ——
   否则会被阅读器的“点句子选句”吃掉（工具条会弹出来）。拖拽多选不受影响
   （只接管 click；mousedown 仅 preventDefault 掉“从引用标记上按下”的文本选择）。
   卡片是 `position: fixed` 的单例浮层：点空白 / Esc / 在**页面上**滚动（“定位原文”引发
   的那次除外）自动收起；但**卡片内部**的滚轮、触控板、拖滚动条一律不算 —— 条目多时
   要能往下看完（只有点卡片外的区域才关）。亮暗两套配色走 CSS 变量（见 style.css 的
   .refpop 一段）。
   用法：const ui = R2Refs({ root: pagesEl }); ui.set(data); ui.markAll() / ui.markItem(item);
*/
window.R2Refs = function (deps) {
  "use strict";
  const root = (deps && deps.root) || document;
  let data = null;               // 最近一次 set() 的数据（无效数据 = null）
  let byBlock = new Map();       // 块 id → [锚点, …]（按 start 升序）
  let popEl = null;              // 复用的弹层（懒建）
  let popHover = false;          // 鼠标是否正停在卡片里（在里面操作时绝不收卡片）
  let lastJumpAt = 0;            // 「定位原文」引发的滚动：短窗口内不收卡片

  /* ================= 数据 ================= */
  function set(d) {
    data = d && d.ok && Array.isArray(d.links) ? d : null;
    byBlock = new Map();
    if (data) {
      for (const l of data.links) {
        if (!l || !l.block) continue;
        let arr = byBlock.get(l.block);
        if (!arr) byBlock.set(l.block, arr = []);
        arr.push(l);
      }
      for (const arr of byBlock.values())
        arr.sort((a, b) => (a.start | 0) - (b.start | 0));
    }
    hide();
  }

  function findBlockEl(bid) {
    if (!root.querySelector) return null;
    const s = String(bid).replace(/["\\]/g, "\\$&");
    return root.querySelector('.blk[data-id="' + s + '"]');
  }

  /* ================= 打标记 ================= */
  /* 摘掉某块里已打的 .cite（幂等的前半步）。只把内层节点搬回原位、不碰其它结构；
     normalize 只对父级做，避免动到 KaTeX 子树里精心分开的文本节点。 */
  function clearBlock(blockEl) {
    const chips = blockEl.querySelectorAll(".cite");
    for (const chip of chips) {
      const parent = chip.parentNode;
      if (!parent) continue;
      // 搬回去之前先把 `absorb()` 抹掉的 inline 字色还原（否则重打失败时正文会掉色）
      for (const n of chip.querySelectorAll("[data-cite-c]")) {
        n.style.color = n.dataset.citeC;
        delete n.dataset.citeC;
      }
      while (chip.firstChild) parent.insertBefore(chip.firstChild, chip);
      parent.removeChild(chip);
      if (parent.normalize) parent.normalize();
    }
  }

  /* 在某个 `[data-g]` 的 span 里把 [a, b)（相对该 span 的字符下标）切出来，
     返回承载这一段的**新 span**（标签/class/style/data-* 照抄原 span，只改 data-g）。
     原 span 会被换成「左残留 / 中间 / 右残留」至多三个同款 span，所以每个 span 的
     文本长度仍然等于它 `[data-g, 下一个 span 的 g)` 的跨度 —— 后面所有按区间
     走 span 的逻辑（含下一次重打）都还能用。 */
  function cutSpan(sp, a, b) {
    const txt = sp.textContent || "";
    const g0 = parseInt(sp.dataset.g, 10);
    if (isNaN(g0) || b <= a || b > txt.length) return null;
    const parts = [];
    const mk = (from, to) => {
      const node = sp.cloneNode(false);       // 只复制标签/属性，不带子节点
      node.textContent = txt.slice(from, to);
      if (node.dataset) node.dataset.g = String(g0 + from);
      return node;
    };
    if (a > 0) parts.push(mk(0, a));
    const mid = mk(a, b);
    parts.push(mid);
    if (b < txt.length) parts.push(mk(b, txt.length));
    const parent = sp.parentNode;
    for (const node of parts) parent.insertBefore(node, sp);
    parent.removeChild(sp);
    return mid;
  }

  function newChip(anchor) {
    const chip = document.createElement("span");
    chip.className = "cite";
    chip.dataset.refs = (anchor.refs || []).join(",");
    chip.dataset.token = anchor.token || "";
    chip.title = "点击查看 " + (anchor.token || "参考文献");
    return chip;
  }

  /* 搬进 chip 的节点不再自带字色：配色交给 `.cite`（含 hover 反白）统管，
     否则 span 上的 inline color 会压住 `.cite` 的颜色，整组引用看起来像“没标上”。
     ⚠️ 公式占位（`.rngap` / `.rnsp`）靠透明字色藏起原字形，必须保留。
     原色暂存在 `data-cite-c` 上，`clearBlock()` 会照原样还原 → 幂等、不永久改色。 */
  function absorb(node) {
    const cls = node.classList;
    if (cls && (cls.contains("rngap") || cls.contains("rnsp"))) return;
    const own = node.style && node.style.color;
    if (own) {
      node.dataset.citeC = own;
      node.style.color = "";
    }
  }

  /* 把块内 [start, end) 区间包成 .cite。
     `[data-g]` span 的 canonical 区间 = [g, 下一个 span 的 g)（被 KaTeX 替换掉的
     span 不在 DOM 里，它那段区间也照跳不误）。

     ⚠️ **同一行内连续的几段共用一个 chip**：解析侧现在按颜色切 run（链接色的引用
     单独一段），一条引用常常跨 2~3 个 span（作者-年份的 `(A, 2020; B, 2021)`、
     编号制的 `[` / `12` / `]` 还可能落在不同字体段）。逐个 span 各包一个 chip 会让
     一组引用看起来“碎成一堆”，悬停/点击也只作用在碎块上。这里把整段连续区间里的
     span（必要时切成子段）一起搬进同一个 `.cite`，于是**一组引用 = 一个整体**。
     只有“洞”（换行 / 公式被 KaTeX 替掉）才断开：后端已按行切开，正常走不到 ——
     真遇上就分段标（决不把标记接到不连续的地方）。 */
  function wrapRange(blockEl, start, end, anchor) {
    const spans = [...blockEl.querySelectorAll("[data-g]")];
    let chip = null;
    let cur = start;
    for (let k = 0; k < spans.length && cur < end; k++) {
      const sp = spans[k];
      const g = parseInt(sp.dataset.g, 10);
      if (isNaN(g)) continue;
      const len = (sp.textContent || "").length;
      if (!len) continue;
      const next = k + 1 < spans.length
        ? parseInt(spans[k + 1].dataset.g, 10) : Infinity;
      if (cur < g) { chip = null; cur = g; }   // 中间有洞 → 另起一个 chip
      if (cur >= next) continue;               // 这个 span 在区间之前
      const from = cur - g;
      const to = Math.min(end, next, g + len) - g;
      if (to <= from) { cur = g + to; continue; }
      let node = sp;
      if (!(from === 0 && to === len)) {
        node = cutSpan(sp, from, to);
        if (!node) { chip = null; cur = g + to; continue; }
      }
      if (!chip) {
        chip = newChip(anchor);
        node.parentNode.insertBefore(chip, node);
      }
      absorb(node);
      chip.appendChild(node);                  // 文档序不变（逐个往后搬）
      cur = g + to;
    }
    return cur >= end;
  }

  function markBlockEl(blockEl, bid) {
    const list = byBlock.get(bid);
    if (!list || !list.length) return;
    clearBlock(blockEl);
    for (const a of list) {
      const s = a.start | 0, e = a.end | 0;
      if (!(e > s)) continue;
      wrapRange(blockEl, s, e, a);
    }
  }

  /* reader2.rebuildItem 末尾调用：块重建后重打一遍标记（幂等） */
  function markItem(item) {
    if (!data || !item || !item.el) return;
    markBlockEl(item.el, item.id);
  }

  /* 数据晚于渲染到达时（一般不会：init 里在 render 之前 set）：全量补一遍 */
  function markAll() {
    if (!data) return;
    for (const bid of byBlock.keys()) {
      const el = findBlockEl(bid);
      if (el) markBlockEl(el, bid);
    }
  }

  /* ================= 弹层 ================= */
  function pop() {
    if (!popEl) {
      popEl = document.createElement("div");
      popEl.className = "refpop";
      popEl.hidden = true;
      document.body.appendChild(popEl);
      popEl.addEventListener("click", onPopClick);
      // 鼠标在卡片里时不让它被收：条目多时要能慢慢滚着看（滚动条/滚轮/选中文字），
      // 也防住“页面重排引起的那次 scroll”把卡片偷走
      popEl.addEventListener("mouseenter", () => { popHover = true; });
      popEl.addEventListener("mouseleave", () => { popHover = false; });
    }
    return popEl;
  }
  function hide() {
    popHover = false;
    if (popEl) popEl.hidden = true;
  }

  function onPopClick(e) {
    const t = e.target;
    const jump = t.closest ? t.closest("[data-jump]") : null;
    if (jump) {
      const entry = data && data.entries && data.entries[parseInt(jump.dataset.jump, 10)];
      if (entry) jumpTo(entry);
      return;
    }
    const copy = t.closest ? t.closest("[data-copy]") : null;
    if (copy) {
      const entry = data && data.entries && data.entries[parseInt(copy.dataset.copy, 10)];
      if (entry && navigator.clipboard) {
        navigator.clipboard.writeText(entry.text || "").then(
          () => { if (window.toast) toast("已复制该条参考文献"); },
          () => {});
      }
      return;
    }
    if (t.closest && t.closest(".rp-x")) hide();     // ✕ 关闭按钮
  }

  function jumpTo(entry) {
    let first = null;
    for (const bid of (entry.blocks || [])) {
      const el = findBlockEl(bid);
      if (!el) continue;
      if (!first) first = el;
      el.classList.add("ref-flash");
      setTimeout(() => el.classList.remove("ref-flash"), 1700);
    }
    if (first) {
      lastJumpAt = Date.now();
      first.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  function mk(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }

  /* 条目标签：编号条目 `[n]`；作者-年份条目取 `[Author, Year]` 里的
     “第一作者姓氏 + 年份”（如 `Chen 2019`）—— 比原先统一的 `•` 好认，
     也避免长标签把卡片正文挤窄。 */
  function entryLabel(e) {
    if (!e) return "•";
    if (e.n) return "[" + e.n + "]";
    let label = e.label || "";
    if (!label) {
      const m = /^\s*(\[[^\[\]\n]{1,120}?,\s*(?:19|20)\d{2}[a-z]?\])/.exec(e.text || "");
      if (m) label = m[1];
    }
    if (!label) return "•";
    const m = /^\[\s*([^\[\],\n]{1,60}?)\s*,\s*((?:19|20)\d{2}[a-z]?)\s*\]$/.exec(label);
    if (m) {
      const first = m[1].split(/\s+/)[0].replace(/[^A-Za-z\-']/g, "");
      if (first) return first + " " + m[2];
    }
    return label;
  }

  function openFor(chip) {
    if (!data) return;
    const idxs = String(chip.dataset.refs || "").split(",")
      .map(s => parseInt(s, 10)).filter(v => !isNaN(v));
    const list = [];
    for (const i of idxs) {
      const e = data.entries && data.entries[i];
      if (e) list.push([i, e]);
    }
    if (!list.length) return;
    const el = pop();
    el.textContent = "";
    const head = mk("div", "rp-head");
    head.appendChild(mk("b", "", "参考文献"));
    head.appendChild(mk("span", "rp-nums",
      list.map(([, e]) => entryLabel(e)).join(" ")));
    const close = mk("button", "rp-x", "✕");
    close.title = "关闭";
    head.appendChild(close);
    el.appendChild(head);

    const box = mk("div", "rp-list");
    for (const [i, e] of list) {
      const item = mk("div", "rp-item");
      item.appendChild(mk("span", "rp-badge", entryLabel(e)));
      const main = mk("div", "rp-main");
      main.appendChild(mk("div", "rp-text",
        e.text || "（这一条没有可显示的文本）"));
      const acts = mk("div", "rp-acts");
      const jb = mk("button", "", "定位原文 ↧");
      jb.dataset.jump = String(i);
      acts.appendChild(jb);
      const cb = mk("button", "", "复制");
      cb.dataset.copy = String(i);
      acts.appendChild(cb);
      if (e.uri) {
        const a = mk("a", "", "打开链接 ↗");
        a.href = e.uri;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        acts.appendChild(a);
      }
      main.appendChild(acts);
      item.appendChild(main);
      box.appendChild(item);
    }
    el.appendChild(box);
    place(chip);
  }

  function place(chip) {
    const el = pop();
    const r = chip.getBoundingClientRect();
    el.hidden = false;
    el.style.visibility = "hidden";
    el.style.left = "0px";
    el.style.top = "0px";
    const pw = el.offsetWidth, ph = el.offsetHeight;
    let left = Math.max(8, Math.min(r.left, window.innerWidth - pw - 8));
    let top = r.bottom + 6;
    if (top + ph > window.innerHeight - 8) {
      const above = r.top - ph - 6;
      top = above >= 56 ? above : Math.max(56, window.innerHeight - ph - 8);
    }
    el.style.left = left + "px";
    el.style.top = top + "px";
    el.style.visibility = "";
  }

  /* ================= 事件 =================
     ⚠️ click 用**捕获**：抢在阅读器（pagesEl 的冒泡监听）之前接管并拦住，
     否则点引用会变成“选中该句 / 弹工具条”。 */
  function closestCite(t) {
    return t && t.closest ? t.closest(".cite") : null;
  }
  /* 事件是不是发生在卡片内部（含卡片自己的滚动事件：`scroll` 的 target 就是卡片） */
  function insidePop(t) {
    return !!(popEl && t && t.nodeType && popEl.contains(t));
  }
  function onPageDown(e) {
    if (closestCite(e.target)) e.preventDefault();   // 别从引用标记上拉出文本选择
  }
  function onPageClick(e) {
    const chip = closestCite(e.target);
    if (!chip || !(root === document || root.contains(chip))) return;
    e.preventDefault();
    e.stopPropagation();
    openFor(chip);
  }
  function onDocClick(e) {
    if (!popEl || popEl.hidden) return;
    if (insidePop(e.target)) return;
    if (closestCite(e.target)) return;
    hide();
  }
  function onScroll(e) {
    // 卡片自己在滚（条目多时拖卡片里的滚动条 / 卡片内容超出高度）→ **不收起**
    if (insidePop(e && e.target)) return;
    // 鼠标正停在卡片里（多半在滚卡片/选文字），页面滚动/重排也不收
    if (popHover) return;
    // 刚点过「定位原文」：平滑滚动本身要几百毫秒，余波也别把卡片收掉
    if (Date.now() - lastJumpAt < 1500) return;
    hide();
  }
  function onWheel(e) {
    // 在卡片里滚轮/触控板滚动 → 交给卡片自己滚（用户要求：鼠标在卡片内操作要能持久存在）
    if (insidePop(e && e.target) || popHover) return;
    // 页面上主动滚轮 → 立即收掉（“定位原文”平滑滚动的余波不算用户滚动）
    if (Date.now() - lastJumpAt > 300) hide();
  }
  function onKey(e) { if (e.key === "Escape") hide(); }

  root.addEventListener("mousedown", onPageDown, true);
  root.addEventListener("click", onPageClick, true);
  document.addEventListener("click", onDocClick, true);
  window.addEventListener("scroll", onScroll, { passive: true, capture: true });
  window.addEventListener("wheel", onWheel, { passive: true });
  window.addEventListener("resize", hide);
  document.addEventListener("keydown", onKey);

  return { set, markAll, markItem, hide };
};
