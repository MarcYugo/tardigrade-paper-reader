/* ============ 段落级翻译 + 译文卡：从 reader2.js 拆出来的模块 ============
   负责：单段/本页/全文翻译（后端按文本哈希缓存）、译文卡的展开收起、译文里的公式渲染，
   以及两个全局开关（“显示译文”勾选、设置里换翻译目标语言）。

   跨模块共享（装配见 reader2.js）：
     · zh / noZh         同一份容器（只做增删，不整体替换）
     · showZhOn          会在勾选框切换时改写 → getter + setter
     · selTransList      “所选句译文”卡列表，会整体替换 → getter + setter
     · sel / itemById / pages / docId  稳定引用
     · updateBar / placeBar / relayoutAll / renderSelTrans / enterParaTransMode  其它段
     · loadSelTrans / savePrefs / findItem  主作用域
   `busy` / `cancelAll` 只在本模块用，直接内部持有。
   ============================================================================ */
window.R2Trans = function (env) {
  "use strict";
  const $ = env.$;                               // #id 查询（reader2 的局部简写）
  let busy = false, cancelAll = false;           // “翻译全文”的进行中/取消标记

  /* ================= 翻译(段落级) ================= */
  async function onTranslateClick(id) {
    const it = env.findItem(id); if (!it) return;
    env.enterParaTransMode();                       // 与“所选句译文”互斥
    if (env.noZh.has(id)) { toast("该段无需翻译(中文/公式/符号)"); return; }
    if (env.zh[id]) { toggleTransOpen(id); return; }
    try {
      const { done, failed } = await translateItems([it], { open: true });
      if (done) { env.updateBar(); env.placeBar(); }
      else if (failed) showNotice("这一段的译文没有拿到：翻译服务没有返回结果，可以再点一次重试。", true);
      else if (env.noZh.has(id)) toast("该段无需翻译(中文/公式/符号)");
    } catch (err) { showNotice("翻译失败：" + err.message, true); }
  }
  function toggleTransOpen(id) {
    const it = env.findItem(id); if (!it) return;
    env.enterParaTransMode();                       // 与“所选句译文”互斥
    it.tsec.hidden = !it.tsec.hidden;
    // “翻译全文”在“显示译文”未勾选时只把译文存进 zh、不渲染卡片，
    // 这里展开必须补写内容，否则弹出来的是**空卡片**（踩过）
    if (!it.tsec.hidden) fillTrans(it);
    syncExp(it);
    env.relayoutAll();
    env.updateBar(); if (env.sel) env.placeBar();
  }
  async function translateItems(items, opt) {
    const pend = items.filter(it => it.kind === "blk" && !env.zh[it.id] && !env.noZh.has(it.id) && it.text.trim().length > 1);
    if (!pend.length) {
      if (opt && opt.open) for (const it of items) if (it.kind === "blk" && env.zh[it.id]) openTrans(it);
      return { done: 0, failed: 0 };
    }
    const res = await api("/api/translate", {
      method: "POST",
      json: { doc_id: env.docId, blocks: pend.map(it => ({ id: it.id, text: env.itemCopyText(it) })) },
    });
    // 服务端把“无需翻译(skipped)”和“这次没拿到(failed)”分开返回：
    // 只有 skipped 才能记进 noZh 永久跳过，failed 必须留着让用户重试。
    const skipped = new Set(res.skipped || []);
    let done = 0, failed = 0;
    for (const it of pend) {
      const z = (res.results || {})[it.id];
      if (z) { env.zh[it.id] = z; done++; if (opt && opt.open) openTrans(it); }
      else if (skipped.has(it.id)) env.noZh.add(it.id);
      else failed++;
    }
    env.relayoutAll();
    return { done, failed };
  }
  // 把译文写进卡片（幂等）。译文统一从 zh[] 取，避免出现“zh 里有、卡片是空的”
  // ⚠️ 不能用 `textContent` 比对是否已写入：译文里的 `$latex$` 会被渲染成 KaTeX，
  // textContent 变成 KaTeX 的内部文字，每次都判成“不一样” → 反复重写/闪烁。
  function fillTrans(item) {
    if (!item || item.kind !== "blk") return;
    const z = env.normTrans(env.zh[item.id] || "");
    if (item._transShown === z) return;
    item._transShown = z;
    item.tsec.textContent = z;
    // 译文里的公式（$latex$）直接出图，阅读体验与正文一致；渲染失败就保留 LaTeX 原文。
    // loose：译文卡的 `$…$` 都是模型按约定输出的，不再用“必须像数学”的启发式筛。
    try { env.renderInlineMath(item.tsec, { loose: true }); } catch (e) { /* ignore */ }
    // 模型常把公式写成**裸 LaTeX**（不带 `$` 定界符，如 `\Delta > 0`）—— 按本块
    // 解析侧给出的公式清单做白名单匹配，命中就地换 KaTeX（不猜、不误伤正文）。
    try { env.renderKnownLatex(item.tsec, itemMathLatex(item)); } catch (e) { /* ignore */ }
  }
  /* 本块的行内公式 latex（Surya / Unlimited-OCR 由解析侧下发在 item.inlineMath） */
  function itemMathLatex(item) {
    const out = [];
    for (const m of ((item && item.inlineMath) || []))
      if (m && m.latex) out.push(m.latex);
    // 本地解析方案：行内公式是页面级 `.fbox` 覆盖层，latex 挂在框上（`item.formulaBoxes`）
    for (const box of ((item && item.formulaBoxes) || []))
      if (box && box.dataset && box.dataset.latex) out.push(box.dataset.latex);
    return out;
  }
  /* “所选句译文”卡的公式清单：`t.key` 里记着全部片段（`<块id>#<起>-<止>` 用 `|` 相连），
     逐个块的公式都要算上 —— 否则跨块选择时，非宿主块的公式不会渲染。 */
  function selTransMathLatex(t, hostItem) {
    const out = [];
    const seen = new Set();
    for (const seg of String(t && t.key || "").split("|")) {
      const id = seg.split("#")[0];
      if (!id || seen.has(id)) continue;
      seen.add(id);
      const it = env.findItem(id);
      if (it) out.push(...itemMathLatex(it));
    }
    if (!out.length) out.push(...itemMathLatex(hostItem));
    return out;
  }
  function openTrans(item) {
    fillTrans(item);
    item.tsec.hidden = false;
    syncExp(item);
  }
  function openAllTrans() {
    let c = 0;
    for (const id in env.zh) { const it = env.findItem(id); if (it) { openTrans(it); c++; } }
    env.relayoutAll();
    return c;
  }
  function closeAllTrans(doLayout) {
    for (const id in env.itemById) { const it = env.itemById[id]; it.tsec.hidden = true; syncExp(it); }
    if (doLayout !== false) env.relayoutAll();
  }
  // 展开区(译文/笔记)的显隐：任一卡片可见就展开 .exp
  function syncExp(item) {
    if (!item || !item.exp) return;
    item.exp.classList.toggle("open",
      !item.tsec.hidden || !item.stbox.hidden || !item.nbox.hidden);
  }
  $("chkShowZh").addEventListener("change", e => {
    env.showZhOn = e.target.checked;
    env.savePrefs({ show_zh: env.showZhOn });      // 这个开关也跟账号走
    if (env.showZhOn) { env.enterParaTransMode(); openAllTrans(); }
    else closeAllTrans();
  });
  /* 设置里换了“翻译目标语言”：服务端的译文缓存与“所选句译文”卡都按语言分开存，
     这里把上一个语言的译文清掉、按新语言重新拉一次，页面上不留旧语言的译文 */
  document.addEventListener("paper-target-lang", async (e) => {
    for (const id in env.zh) delete env.zh[id];
    env.noZh.clear();
    for (const id in env.itemById) {
      const it = env.itemById[id];
      if (it && it.tsec) { it.tsec.textContent = ""; it._transShown = null; }
    }
    try {
      Object.assign(env.zh, await api("/api/doc/" + env.docId + "/zh_all").catch(() => ({})));
      env.selTransList = await env.loadSelTrans();
    } catch (err) { /* 拉不到就当还没翻译，不影响阅读 */ }
    env.renderSelTrans();
    if (env.showZhOn) openAllTrans(); else closeAllTrans();
    env.relayoutAll();
    env.updateBar();
    const name = (e.detail && e.detail.name) || "";
    toast(name ? ("翻译目标语言已改为 " + name + "，本页译文已按新语言刷新")
               : "翻译目标语言已切换，本页译文已刷新");
  });
  $("btnPageTrans").addEventListener("click", async () => {
    const pi = env.currentPageIndex();
    // 逐页加载：当前页可能只有页壳 —— 先把这一页取回来，否则会误报“没有可翻译段落”
    if (env.ensurePage && env.pages[pi] && !env.pages[pi].loaded) await env.ensurePage(pi);
    const items = env.pages[pi] ? env.pages[pi].items.filter(i => i.kind === "blk") : [];
    env.enterParaTransMode();                       // 与“所选句译文”互斥
    try {
      const { done, failed } = await translateItems(items, { open: true });
      if (done && failed) toast(`本页翻译完成 ${done} 段，另有 ${failed} 段没有拿到译文（可再点一次重试）`);
      else if (done) toast(`本页翻译完成 ${done} 段`);
      else if (failed) showNotice(`本页有 ${failed} 段没有拿到译文：翻译服务没有返回结果，可稍后再点「翻译本页」重试（这些段落仍然可以单独翻译）。`, true);
      else toast("本页没有需要翻译的英文段落");
    } catch (err) { showNotice("翻译失败：" + err.message, true); }
  });
  $("btnAllTrans").addEventListener("click", runAllTrans);
  async function runAllTrans() {
    if (busy) return;
    busy = true; cancelAll = false;
    env.enterParaTransMode();                       // 与“所选句译文”互斥
    const btn = $("btnAllTrans");
    btn.disabled = true;
    const prog = $("transProgress"), bar = $("tpBar"), txt = $("tpText");
    prog.hidden = false;
    // 逐页加载：“全文翻译”要按**整篇**收集段落，所以先把还没建出来的页补齐
    // （已加载的页不重复取；每补一页都会顺手把已缓存的译文渲染出来）。
    if (env.ensurePage) {
      for (let pi = 0; pi < env.pages.length; pi++) {
        if (cancelAll) break;
        if (!env.pages[pi].loaded) {
          txt.textContent = "正在加载全文… 第 " + (pi + 1) + " / " + env.pages.length + " 页";
          await env.ensurePage(pi);
        }
      }
    }
    const all = [];
    // 表格/纯公式块没有可翻译正文（text 为空）：别把空串送去翻译
    env.pages.forEach(p => p.items.forEach(it => {
      if (it.kind === "blk" && !env.zh[it.id] && !env.noZh.has(it.id) && it.text.trim().length > 1)
        all.push(it);
    }));
    if (!all.length) { toast("全部段落均已翻译"); prog.hidden = true; btn.disabled = false; busy = false; return; }
    const total = all.length;
    let doneN = 0, failedN = 0;
    try {
      for (let k = 0; k < all.length && !cancelAll; k += 20) {
        const chunk = all.slice(k, k + 20);
        const res = await api("/api/translate", {
          method: "POST", json: { doc_id: env.docId, blocks: chunk.map(i => ({ id: i.id, text: env.itemCopyText(i) })) },
        });
        const skipped = new Set(res.skipped || []);
        chunk.forEach(i => {
          const z = (res.results || {})[i.id];
          if (z) { env.zh[i.id] = z; if (env.showZhOn) openTrans(i); }
          else if (skipped.has(i.id)) env.noZh.add(i.id);   // 真·无需翻译才跳过
          else failedN++;                               // 没拿到 → 留着可重试
          doneN++;
        });
        bar.style.width = Math.round(doneN / total * 100) + "%";
        txt.textContent = doneN + " / " + total;
      }
      env.relayoutAll();
      if (failedN && !cancelAll) {
        showNotice(`全文翻译完成，但有 ${failedN} 段没有拿到译文（翻译服务没返回结果）。再点一次「翻译全文」即可只重试这些段落。`, true);
      } else if (cancelAll) {
        toast("已暂停翻译");
      } else if (!env.showZhOn) {
        // 没勾“显示译文”时卡片都是收着的，提示一下怎么看，别让人以为没翻译
        showNotice("全文翻译完成。勾选顶部的「显示译文」可展开全篇译文，或点任意句子后选「显示本段译文」逐段查看。", false);
      } else {
        toast("全文翻译完成 🎉");
      }
    } catch (err) {
      showNotice("翻译失败：" + err.message + "（可能未配置翻译服务或余额不足）", true);
    } finally {
      prog.hidden = true; btn.disabled = false; busy = false;
    }
  }
  $("tpCancel").addEventListener("click", () => { cancelAll = true; });

  /* ============ 对外接口 ============ */
  return {
    onTranslateClick, toggleTransOpen, translateItems, fillTrans, itemMathLatex,
    selTransMathLatex, openTrans, openAllTrans, closeAllTrans, syncExp, runAllTrans,
  };
};
