/* ============ 高亮调色盘 + 浮动工具条 + “所选句译文”卡：从 reader2.js 拆出来的模块 ============
   三块放在一起，是因为它们共用同一批状态与 DOM：
     · 调色盘（hlPalette / hlColor / swatches）既给工具条上的取色块用，也给
       `hlPaint`（正文高亮的着色，仍在 reader2）和笔记抽屉的换主题重绘用；
     · 工具条（#fbar）的按钮既触发高亮，也触发“翻译所选句”卡片（.stbox）；
     · 卡片展开后都要调 `relayoutAll` 重排。

   跨模块共享（装配见 reader2.js）：
     · sel / anno / selTransList  跨模块共享且会被**整体替换** → getter + setter
     · zh / noZh / itemById / docId  稳定引用（zh、noZh 是同一份容器）
     · 选中与文本工具：selCount / selAnchor / selItems / selKeys / clearSelection /
       refreshSelectionVisual / sentKey / sentRange / normTrans / copy*Text
     · 翻译与布局（reader2 里更靠后的段，函数声明会提升）：onTranslateClick /
       relayoutAll / syncExp / selTransMathLatex / closeAllTrans
   ============================================================================ */
window.R2Bar = function (env) {
  "use strict";
  const $ = env.$;          // #id 查询（common.js 里没有，是 reader2 的局部简写）

  /* ============ 高亮调色盘：3 个默认色 + 3 个自定义色 ============
     自定义色**随账号存在服务端**（/api/prefs）—— 换账号会重新加载，不会互相串；
     文档里存的是色位（c1/c2/c3），颜色本身只是当前账号的偏好。 */
  const HL_STORE_OLD = "paperReader.hlColors";   // 旧版本的浏览器本地存储（升级后不再使用）
  const hlPalette = [
    { key: "yellow", color: "#ffd64a", alpha: 0.55, custom: false },
    { key: "green",  color: "#60c994", alpha: 0.50, custom: false },
    { key: "pink",   color: "#ff8a9e", alpha: 0.55, custom: false },
    { key: "c1", color: "#7ec8f2", alpha: 0.50, custom: true },
    { key: "c2", color: "#c4a3f0", alpha: 0.50, custom: true },
    { key: "c3", color: "#ffb26b", alpha: 0.50, custom: true },
  ];
  const HL_CUSTOM_AT = 3;                       // 从第几个开始是自定义色
  function hlColor(key) {                       // 色键 -> 调色盘项(未知键回退黄色)
    return hlPalette.find(c => c.key === key) || hlPalette[0];
  }
  function applyHlColors(list) {
    if (!Array.isArray(list)) return;
    list.slice(0, 3).forEach((hex, i) => {
      if (/^#[0-9a-f]{6}$/i.test(hex || "")) hlPalette[HL_CUSTOM_AT + i].color = hex;
    });
  }
  // 偏好（自定义高亮色 / 显示译文开关）随账号存在服务端：进页时拉一次

  let hlSaveTimer = null;
  function saveHlColors() {
    // 存到服务端（跟着账号走）。取色器拖动会连续触发 input，这里防抖一下。
    clearTimeout(hlSaveTimer);
    hlSaveTimer = setTimeout(() => {
      env.savePrefs({ hl_colors: hlPalette.slice(HL_CUSTOM_AT).map(c => c.color) });
    }, 400);
  }
  // 渲染色块 + 同步 3 个取色器（自定义色改完立即生效并记住）
  function renderHmenuSwatches() {
    const wrap = $("hmenu").querySelector(".swatches");
    wrap.textContent = "";
    for (const c of hlPalette) {
      const s = el("span", "sw" + (c.custom ? " custom" : ""));
      s.dataset.c = c.key;
      s.style.background = c.color;
      if (isDark()) s.style.boxShadow = "0 0 9px " + c.color;   // 暗夜下色块也发点荧光，方便选
      s.title = c.custom ? "自定义高亮色（用下方颜色块修改）" : c.key;
      wrap.appendChild(s);
    }
    document.querySelectorAll("#hmenu .swedit input").forEach((inp, i) => {
      inp.value = hlPalette[HL_CUSTOM_AT + i].color;
    });
    markCurSwatch();
  }
  function bindHlColorInputs() {
    document.querySelectorAll("#hmenu .swedit input").forEach((inp, i) => {
      inp.addEventListener("input", () => {
        hlPalette[HL_CUSTOM_AT + i].color = inp.value;
        saveHlColors();
        renderHmenuSwatches();
      });
    });
  }

  /* ================= 浮动工具条 ================= */
  const fbar = $("fbar");
  function hideBar() { if (fbar) fbar.hidden = true; hideHmenu(); }
  // keepMenu=true 时只挪位置、不动调色盘(调色盘就挂在工具条里，会跟着走)；
  // 换选中句子时用默认的 false，顺手收起调色盘。
  function placeBar(keepMenu) {
    if (!env.sel) return;
    const anchor = env.selAnchor();
    if (!anchor) return;
    fbar.hidden = false;
    if (!keepMenu) hideHmenu();
    const formula = env.sel.formulaBox && env.sel.formulaBox.isConnected ? env.sel.formulaBox : null;
    const r = formula ? formula.getBoundingClientRect() : anchor.item.el.getBoundingClientRect();
    let top = r.top - 48;
    if (top < 60) top = r.bottom + 8;
    fbar.style.top = Math.max(60, top) + "px";
    fbar.style.left = clamp(r.left, 8, Math.max(8, env.vpW() - fbar.offsetWidth - 8)) + "px";
  }
  // 工具条只放操作按钮（翻译/标注/笔记/复制/关闭）——
  // 原来最左侧那个显示选中内容的预览（#fbT）已去掉，不再回填。
  function updateBar() {
    const bt = $("fbTrans");
    if (!env.sel) return;
    const anchor = env.selAnchor();
    const n = env.selCount();
    const blocks = env.selItems();
    if (n > 1) {
      bt.textContent = env.selTransList.some(t => t.key === selFragKey())
        ? "收起所选句译文" : "翻译所选句子";
    } else {
      const id = blocks[0].id;
      if (env.noZh.has(id)) bt.textContent = "本段无译文";
      else if (env.zh[id]) bt.textContent = isTransOpen(id) ? "收起本段译文" : "显示本段译文";
      else bt.textContent = "译本段";
    }
    const nb = $("fbNote");
    if (nb) nb.textContent = env.noteOf(env.sentKey(anchor.item, anchor.si)) ? "改笔记" : "笔记";
  }
  function isTransOpen(id) {
    const it = env.findItem(id);
    return !!(it && !it.tsec.hidden);
  }

  fbar.addEventListener("click", e => {
    const sw = e.target.closest(".sw");
    if (sw) { if (env.sel) setHighlight(sw.dataset.c); hideHmenu(); return; }
    const a = e.target.closest("[data-a]") && e.target.closest("[data-a]").dataset.a;
    if (!a || !env.sel) return;
    if (a === "close") env.clearSelection();
    else if (a === "copy") {
      const n = env.selCount();
      const formula = env.sel.formulaBox;
      const formulaOnly = formula && !formula.matches(".mathbox.inline") &&
        formula.dataset.cls !== "InlineFormula";
      const mathItem = formulaOnly && formula.closest(".blk")
        ? env.findItem(formula.closest(".blk").dataset.id) : null;
      const s = formulaOnly
        ? (formula.dataset.latex || env.mathTexOf(mathItem))
        : env.sel.sents.map(r => env.mathTexOf(r.item) || env.copySentenceText(r.item, r.si)).join(" ");
      navigator.clipboard.writeText(s).then(() => toast(n > 1 ? "已复制 " + n + " 句" : "已复制该句"));
    } else if (a === "trans") {
      if (env.selCount() > 1) translateSelected();
      else env.onTranslateClick(env.selItems()[0].id);
    }
    else if (a === "hl") toggleHmenu();
    else if (a === "note") env.openNoteEditor(true);
    else if (a === "hlclear") { setHighlight(null); hideHmenu(); }
  });

  /* ---- 多选句子 → 只翻译所选句 ----
     把选区按文字块切成若干“连续句片段”，各自送翻译(后端按文本哈希缓存，和整段译文互不干扰)，
     结果只显示所选句的译文，不影响原来的整段翻译功能。 */
  function selGroups() {
    const groups = [];
    if (!env.sel) return groups;
    for (const r of env.sel.sents) {
      const last = groups[groups.length - 1];
      if (last && last.item === r.item) last.siTo = r.si;
      else groups.push({ item: r.item, siFrom: r.si, siTo: r.si });
    }
    return groups;
  }
  function selFragId(g) { return g.item.id + "#" + g.siFrom + "-" + g.siTo; }
  function selFragKey() { return selGroups().map(selFragId).join("|"); }
  function selFragText(g) {
    const a = env.sentRange(g.item, g.siFrom).a;
    const b = env.sentRange(g.item, g.siTo).b;
    // 与点「复制」拿到的一致：公式保留成 `$latex$`，空白压成单空格
    return env.copyRangeText(g.item, a, b);
  }

  async function translateSelected() {
    const groups = selGroups();
    const frags = groups.map(g => ({ id: selFragId(g), text: selFragText(g), item: g.item, si: g.siFrom }))
                       .filter(f => f.text.length > 1);
    if (!frags.length) { toast("所选句子没有可翻译的内容"); return; }
    const key = frags.map(f => f.id).join("|");
    const idx = env.selTransList.findIndex(t => t.key === key);
    if (idx >= 0) {                              // 同一选区再点一次 = 收起这张
      env.selTransList.splice(idx, 1);
      renderSelTrans(); env.relayoutAll(); updateBar();
      return;
    }
    try {
      const res = await api("/api/translate", {
        method: "POST",
        json: { doc_id: env.docId, blocks: frags.map(f => ({ id: f.id, text: f.text })) },
      });
      const parts = frags.map(f => (res.results || {})[f.id]).filter(z => z);
      if (!parts.length) {
        const skipped = new Set(res.skipped || []);
        toast(skipped.size === frags.length
          ? "所选句子无需翻译(中文/公式/符号)"
          : "没有拿到译文：翻译服务没有返回结果，请再点一次重试");
        return;
      }
      // 与整段译文互斥：切换成“所选句译文”模式，收起所有整段译文卡（译文仍在缓存里，可随时再开）
      env.closeAllTrans(false);
      // 卡片挂到**最后一个被选句子**的块下（不是第一句）：连选多句时若挂第一句，
      // 卡片会插在所选句中间，后面的正文看起来像被错了位。
      const last = frags[frags.length - 1];
      const card = { key, itemId: last.item.id, siFrom: last.si,
                     text: env.normTrans(parts.join(" ")) };
      env.selTransList.push(card);
      renderSelTrans();
      env.relayoutAll();
      updateBar(); placeBar(true);
      toast("已翻译所选 " + env.selCount() + " 句");
      // 存到服务端，重登/刷新后这张卡还在（失败只提示，不影响阅读）
      api("/api/doc/" + env.docId + "/seltrans", {
        method: "POST",
        json: { key, item_id: card.itemId, si_from: card.siFrom, text: card.text },
      }).catch(err => showNotice("所选句译文未能保存到服务端：" + err.message, true));
    } catch (err) { showNotice("翻译失败：" + err.message, true); }
  }

  /* 所选句译文卡：可以同时留多张（不同选区）；与“整段译文”互斥，二者只保留一种 */
  function clearSelTrans() {
    if (!env.selTransList.length) return;
    env.selTransList = [];
    renderSelTrans();
    updateBar();          // 卡片没了，工具条上“收起所选句译文”要跟着变回“翻译所选句子”
    api("/api/doc/" + env.docId + "/seltrans", { method: "DELETE" }).catch(() => {});
  }
  // 切回“整段译文”模式：把所选句译文卡全部收掉（它们在缓存里，可随时重新翻译出来）
  function enterParaTransMode() {
    if (!env.selTransList.length) return;
    clearSelTrans();
    env.relayoutAll();
  }
  function renderSelTrans() {
    for (const id in env.itemById) {
      const it = env.itemById[id];
      if (!it.stbox.hidden || it.stbox.childElementCount) {
        it.stbox.textContent = "";
        it.stbox.hidden = true;
        env.syncExp(it);
      }
    }
    const byItem = new Map();
    for (const t of env.selTransList) {
      const it = selTransHost(t);
      if (!it) continue;
      if (!byItem.has(it)) byItem.set(it, []);
      byItem.get(it).push(t);
    }
    for (const [it, list] of byItem) {
      list.sort((a, b) => (a.siFrom || 0) - (b.siFrom || 0));
      for (const t of list) it.stbox.appendChild(selTransCard(it, t));
      it.stbox.hidden = false;
      env.syncExp(it);
    }
  }
  function selTransHost(t) {
    /* 卡片挂在哪个块下：**最后一个被选句子**的块。
       `key` 里记着全部片段（`<块id>#<起>-<止>` 用 `|` 相连），所以连早期存的
       卡片（item_id 还是第一句的块）也能落到正确位置；解析不出来再退回 item_id。 */
    const frag = String(t.key || "").split("|").pop() || "";
    const id = frag.split("#")[0];
    return (id && env.findItem(id)) || env.findItem(t.itemId);
  }
  function selTransCard(item, t) {
    const card = el("div", "card-in stsec");
    const head = el("div", "sthead");
    head.appendChild(el("span", "", "所选句译文"));
    const bDel = el("button", "stclear", "清除");
    bDel.addEventListener("click", () => {
      env.selTransList = env.selTransList.filter(x => x.key !== t.key);
      renderSelTrans(); env.relayoutAll(); updateBar();
      api("/api/doc/" + env.docId + "/seltrans?key=" + encodeURIComponent(t.key),
          { method: "DELETE" }).catch(() => {});
    });
    head.appendChild(bDel);
    card.appendChild(head);
    const body = el("div", "stbody", env.normTrans(t.text));
    card.appendChild(body);
    // 译文里的 `$latex$` 与整段译文卡同口径：就地换 KaTeX（失败就保留 LaTeX 原文）
    try { env.renderInlineMath(body, { loose: true }); } catch (e) { /* ignore */ }
    // 同上：模型写成裸 LaTeX 时，按宿主块已知的公式清单兜底渲染
    try { env.renderKnownLatex(body, env.selTransMathLatex(t, item)); } catch (e) { /* ignore */ }
    return card;
  }
  function hideHmenu() { const m = $("hmenu"); if (m) m.classList.remove("show"); }
  function toggleHmenu() {
    const m = $("hmenu");
    m.classList.toggle("show");
    // 先定位(keepMenu=true，否则刚展开又被 hideHmenu 收掉)，再标出当前高亮色
    placeBar(true);
    if (m.classList.contains("show")) markCurSwatch();
  }
  function curHighlightColor() {
    if (!env.sel) return null;
    // 选中的句子全部同色才把该色块标出来
    const cur = env.anno.highlights || {};
    const colors = env.selKeys().map(k => (cur[k] && cur[k].color) || null);
    return colors.length && colors.every(c => c && c === colors[0]) ? colors[0] : null;
  }
  function markCurSwatch() {
    const cur = curHighlightColor();
    document.querySelectorAll("#hmenu .sw").forEach(s =>
      s.classList.toggle("cur", s.dataset.c === cur));
  }
  async function setHighlight(color) {
    if (!env.sel) return;
    if (!env.needWrite()) return;
    const keys = env.selKeys();
    try {
      const cur = env.anno.highlights || {};
      // 选中的句子全是这个颜色 → 再点一次 = 整段取消
      if (color && keys.every(k => cur[k] && cur[k].color === color)) color = null;
      for (const k of keys) {
        await api(`/api/doc/${env.docId}/anno`, {
          method: "POST",
          json: color
            ? { action: "add_highlight", block_id: k, color }
            : { action: "remove_highlight", block_id: k },
        });
      }
      env.anno = await api(`/api/doc/${env.docId}/anno`);   // 批量写入后取一次权威状态
      env.refreshSelectionVisual();
      const n = keys.length;
      toast(color ? (n > 1 ? `已标注 ${n} 句` : "已标注该句")
                  : (n > 1 ? `已清除 ${n} 句标注` : "已清除标注"));
    } catch (err) { toast(err.message); }
  }

  /* ============ 对外接口 ============ */
  return {
    hlColor, applyHlColors, renderHmenuSwatches, bindHlColorInputs, saveHlColors,
    hideBar, placeBar, updateBar, isTransOpen, hideHmenu,
    renderSelTrans, clearSelTrans, enterParaTransMode,
    selGroups, selFragId, selFragKey, selFragText, translateSelected,
    setHighlight, curHighlightColor, markCurSwatch,
  };
};
